# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Paul Michali, Cisco Systems, Inc.

import abc
from collections import namedtuple
import netaddr

import httplib
from oslo.config import cfg

from neutron.common import exceptions
from neutron.common import rpc as n_rpc
from neutron import context
from neutron.openstack.common import lockutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import rpc
from neutron.openstack.common.rpc import proxy
from neutron.plugins.common import constants
from neutron.plugins.common import utils as plugin_utils
from neutron.services.vpn.common import topics
from neutron.services.vpn import device_drivers
from neutron.services.vpn.device_drivers import (
    cisco_csr_rest_client as csr_client)


CONF = cfg.CONF
ipsec_opts = [
    cfg.IntOpt('ipsec_status_check_interval',
               default=60,
               help=_("Interval for checking ipsec status"))
]
CONF.register_opts(ipsec_opts, 'ipsec')

LOG = logging.getLogger(__name__)

RollbackStep = namedtuple('RollbackStep', ['action', 'resource_id', 'title'])


class CsrResourceCreateFailure(exceptions.NeutronException):
    message = _("Cisco CSR failed to create %(resource)s (%(which)s)")


class CsrDriverMismatchError(exceptions.NeutronException):
    message = _("Required %(resource)s attribute %(attr)s mapping for Cisco "
                "CSR is missing in device driver")


class CsrUnknownMappingError(exceptions.NeutronException):
    message = _("Device driver does not have a mapping of '%(value)s for "
                "attribute %(attr)s of %(resource)s")


class CsrDriverSyncError(exceptions.NeutronException):
    message = _("Device driver does not have info on connection %(conn)s")


def find_available_csrs_from_config(config_files):
    """Read INI for available Cisco CSRs that driver can use.

    Loads management port, user, and password, information for available
    CSRs from configuration file. Driver will use this info to configure
    VPN connections. The CSR is associated 1:1 with a Neutron router. To
    identify which CSR to use for a VPN service, the public (GW) IP of
    the Neutron router will be used as an index into the CSR config info.
    """
    multi_parser = cfg.MultiConfigParser()
    read_ok = multi_parser.read(config_files)

    if len(read_ok) != len(config_files):
        raise cfg.Error(_("Unable to parse config files for Cisco CSR "
                          "info"))
    csrs_found = {}
    for parsed_file in multi_parser.parsed:
        for parsed_item in parsed_file.keys():
            device_type, sep, for_router = parsed_item.partition(':')
            if device_type.lower() == 'cisco_csr':
                try:
                    netaddr.IPNetwork(for_router)
                except netaddr.core.AddrFormatError:
                    LOG.error(_("Ignoring Cisco CSR configuration entry - "
                                "subnet '%s' is not valid"), for_router)
                    continue
                entry = parsed_file[parsed_item]
                # Check for missing fields
                try:
                    rest_mgmt_ip = entry['rest_mgmt'][0]
                    username = entry['username'][0]
                    password = entry['password'][0]
                except KeyError as ke:
                    LOG.error(_("Ignoring Cisco CSR for subnet %(subnet)s "
                                "- missing %(field)s setting"),
                              {'subnet': for_router, 'field': str(ke)})
                    continue
                # Validate fields
                try:
                    timeout = entry['timeout'][0]
                    timeout = float(timeout)
                except ValueError:
                    LOG.error(_("Ignoring Cisco CSR for subnet %s - "
                                "timeout is not a floating point number"),
                              for_router)
                    continue
                except KeyError:
                    timeout = csr_client.TIMEOUT
                try:
                    netaddr.IPAddress(rest_mgmt_ip)
                except netaddr.core.AddrFormatError:
                    LOG.error(_("Ignoring Cisco CSR for subnet %s - "
                                "REST management is not an IP address"),
                              for_router)
                    continue
                # TODO(pcm) Consider tuple
                csrs_found[for_router] = {'rest_mgmt': rest_mgmt_ip,
                                          'username': username,
                                          'password': password,
                                          'timeout': timeout}

                LOG.debug(_("Found CSR for subnet %(subnet)s: %(info)s"),
                          {'subnet': for_router,
                           'info': csrs_found[for_router]})
    LOG.info(_("Loaded %d Cisco CSR configurations"), len(csrs_found))
    return csrs_found


class CiscoCsrIPsecVpnDriverApi(proxy.RpcProxy):
    """RPC API for agent to plugin messaging."""
    IPSEC_PLUGIN_VERSION = '1.0'

    def get_vpn_services_on_host(self, context, host):
        """Get list of vpnservices on this host.

        The vpnservices including related ipsec_site_connection,
        ikepolicy, ipsecpolicy, and Cisco info on this host.
        """
        return self.call(context,
                         self.make_msg('get_vpn_services_on_host',
                                       host=host),
                         version=self.IPSEC_PLUGIN_VERSION,
                         topic=self.topic)

    def update_status(self, context, status):
        """Update status for all VPN services and connections."""
        return self.cast(context,
                         self.make_msg('update_status',
                                       status=status),
                         version=self.IPSEC_PLUGIN_VERSION,
                         topic=self.topic)


class CiscoCsrIPsecDriver(device_drivers.DeviceDriver):
    """Cisco CSR VPN Device Driver for IPSec.

    This class is designed for use with L3-agent now.
    However this driver will be used with another agent in future.
    so the use of "Router" is kept minimul now.
    Insted of router_id,  we are using process_id in this code.
    """

    # history
    #   1.0 Initial version

    RPC_API_VERSION = '1.0'
    __metaclass__ = abc.ABCMeta

    def __init__(self, agent, host):
        self.agent = agent
        self.conf = self.agent.conf
        self.root_helper = self.agent.root_helper
        self.host = host
        self.conn = rpc.create_connection(new=True)
        self.context = context.get_admin_context_without_session()
        self.topic = topics.CISCO_IPSEC_AGENT_TOPIC
        node_topic = '%s.%s' % (self.topic, self.host)

        self.service_state = {}

        self.conn.create_consumer(
            node_topic,
            self.create_rpc_dispatcher(),
            fanout=False)
        self.conn.consume_in_thread()
        self.agent_rpc = (
            CiscoCsrIPsecVpnDriverApi(topics.CISCO_IPSEC_DRIVER_TOPIC, '1.0'))
        self.periodic_report = loopingcall.FixedIntervalLoopingCall(
            self.report_status, self.context)
        self.periodic_report.start(
            interval=self.conf.ipsec.ipsec_status_check_interval)

        csrs_found = find_available_csrs_from_config(CONF.config_file)
        self.csrs = dict([(k, csr_client.CsrRestClient(v['rest_mgmt'],
                                                       v['username'],
                                                       v['password'],
                                                       v['timeout']))
                          for k, v in csrs_found.items()])

    def create_rpc_dispatcher(self):
        return n_rpc.PluginRpcDispatcher([self])

    STATUS_MAP = {'ERROR': constants.ERROR,
                  'UP-ACTIVE': constants.ACTIVE,
                  'UP-IDLE': constants.ACTIVE,
                  'UP-NO-IKE': constants.ACTIVE,
                  'DOWN': constants.DOWN,
                  'DOWN-NEGOTIATING': constants.DOWN}

    def vpnservice_updated(self, context, **kwargs):
        """Handle VPNaaS service driver change notifications."""
        LOG.debug(_("Handling VPN service update notification"))
        self.sync(context, [])

    def snapshot_service_state(self, vpn_service):
        """Create/get VPN service state and save current status.

        Upon creation, the router's external IP will be used to refer to
        the associated Cisco CSR.
        """
        service_state = self.service_state.setdefault(
            vpn_service['id'],
            CiscoCsrVpnService(self.csrs.get(vpn_service['external_ip'])))
        service_state.last_status = vpn_service['status']
        return service_state

    def update_all_services_and_connections(self, context):
        """Update services and connections based on plugin info.

        Perform any create and update operations and then update status.
        Mark every visited ipsec_conn as no longer "dirty" so they will
        not be deleted at end of sync processing.
        """
        services_data = self.agent_rpc.get_vpn_services_on_host(context,
                                                                self.host)
        LOG.debug("Sync start for %d VPN services", len(services_data))
        for service_data in services_data:
            vpn_service_id = service_data['id']
            csr_id = service_data['external_ip']
            if csr_id not in self.csrs:
                LOG.error(_("Skipping VPN service %s as it is not "
                            "associated with a Cisco CSR"), vpn_service_id)
                continue
            LOG.debug(_("Processing service %s"), vpn_service_id)
            vpn_service = self.snapshot_service_state(service_data)
            for conn_data in service_data['ipsec_conns']:
                conn_id = conn_data['id']
                prev_status = vpn_service.conn_status(conn_id)
                ipsec_conn = vpn_service.snapshot_conn_state(conn_data)
                if conn_data['status'] == constants.PENDING_CREATE:
                    ipsec_conn.create_ipsec_site_connection(context,
                                                            conn_data)
                else:
                    if not prev_status:
                        raise CsrDriverSyncError(conn=conn_data['id'])
                    if prev_status != conn_data['status']:
                        # TODO(pcm) FUTURE - handle update. May need to save
                        # more state anc compare (peer_id, peer_cidrs, mtu,
                        # psk, dpd, and admin state).
                        pass

    def mark_existing_connections_as_dirty(self):
        """Mark all existing connections as "dirty" for sync."""
        for _, service_state in self.service_state.items():
            # TODO(pcm) Mark services too?
            for conn_id in service_state.conn_state:
                service_state.conn_state[conn_id].is_dirty = True

    def remove_unknown_connections(self, context):
        """Remove connections that are not known by service driver."""
        for service_state in self.service_state.values():
            dirty = [c_id for c_id, c in service_state.conn_state.items()
                     if c.is_dirty]
            for conn_id in dirty:
                conn_state = service_state.conn_state[conn_id]
                conn_state.delete_ipsec_site_connection(context, conn_id)
                del service_state.conn_state[conn_id]

    def report_status(self, context):
        """Get current status and report any changes to plugin."""
        # TODO(pcm) Handle VPN service deletion reporting
        # TODO(pcm) Refactor as this is long and messy
        LOG.debug(_("report_status for %d services"), len(self.service_state))
        service_report = []
        for vpn_service_id, vpn_service_state in self.service_state.items():
            LOG.debug(_("Collecting status for service %s"), vpn_service_id)
            any_connections = False
            conn_report = {}
            tunnels = vpn_service_state.get_ipsec_connections_status()
            for conn_id, conn_state in vpn_service_state.conn_state.items():
                tunnel_id = conn_state.tunnel
                if tunnel_id in tunnels:
                    conn_status = self.STATUS_MAP[tunnels[tunnel_id]]
                    any_connections = True
                elif conn_state.last_status != constants.PENDING_DELETE:
                    conn_status = constants.ERROR
                else:
                    conn_status = None
                if conn_status != conn_state.last_status:
                    LOG.debug(_("Reporting connection %(conn)s changing "
                                "status from %(prev)s to %(status)s"),
                              {'conn': conn_id,
                               'status': conn_status,
                               'prev': conn_state.last_status})
                    request_processed = plugin_utils.in_pending_status(
                        conn_state.last_status)
                    conn_report[conn_id] = {
                        'status': conn_status,
                        'updated_pending_status': request_processed
                    }
                    if conn_status == None:
                        del vpn_service_state.conn_state[conn_id]
                    else:
                        conn_state.last_status = conn_status
            service_status = (
                constants.ACTIVE if any_connections else constants.DOWN)
            # Report any status changes
            service_changed = service_status != vpn_service_state.last_status
            if conn_report or service_changed:
                if service_changed:
                    LOG.debug(_("VPN service %(service)s changed status from "
                                "%(prev)s to %(status)s"),
                              {'service': vpn_service_id,
                               'prev': vpn_service_state.last_status,
                               'status': service_status})
                request_processed = plugin_utils.in_pending_status(
                    vpn_service_state.last_status)
                service_report.append({
                    'id': vpn_service_id,
                    'status': service_status,
                    'updated_pending_status': request_processed,
                    'ipsec_site_connections': conn_report
                })
            if service_changed:
                vpn_service_state.last_status = service_status
        if service_report:
            self.agent_rpc.update_status(context, service_report)

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def sync(self, context, routers):
        """Synchronize with plugin and report current status.

        Mark all "known" connections as dirty, update them based on
        information from the plugin, remove any connections that are
        not updated (dirty), and report any updates back to plugin.
        Called when update/delete a service or create/update/delete a
        connection (vpnservice_updated message), or router change
        (_process_routers).

        TODO(pcm) Handle the following conditions:
            1) Agent class restarted
            2) Failure on process creation
            3) VpnService is deleted during agent down
            4) RPC failure
            5) Cisco CSR restart
        """
        self.mark_existing_connections_as_dirty()
        self.update_all_services_and_connections(context)
        self.remove_unknown_connections(context)
        self.report_status(context)

    def create_router(self, process_id):
        """Actions taken when router created."""
        # Note: Since Cisco CSR is running out-of-band, nothing to do here
        pass

    def destroy_router(self, process_id):
        """Actions taken when router deleted."""
        # Note: Since Cisco CSR is running out-of-band, nothing to do here
        pass


class CiscoCsrVpnService(object):

    """Maintains state/status information for a service and its connections."""

    def __init__(self, csr):
        self.last_status = None
        self.conn_state = {}
        self.csr = csr
        # TODO(pcm) FUTURE - handle sharing of policies

    def get_connection(self, conn_id):
        return self.conn_state.get(conn_id)

    def conn_status(self, conn_id):
        conn_state = self.get_connection(conn_id)
        if conn_state:
            return conn_state.last_status

    def snapshot_conn_state(self, ipsec_conn):
        """Create/obtain connection state and save current status."""
        conn_state = self.conn_state.setdefault(
            ipsec_conn['id'], CiscoCsrIPSecConnection(ipsec_conn, self.csr))
        conn_state.last_status = ipsec_conn['status']
        conn_state.is_dirty = False
        return conn_state

    def get_ipsec_connections_status(self):
        """Obtain current status of all tunnels on a Cisco CSR."""
        tunnels = self.csr.read_tunnel_statuses()
        for tunnel in tunnels:
            LOG.debug("CSR Reports %(tunnel)s status '%(status)s'",
                      {'tunnel': tunnel[0], 'status': tunnel[1]})
        return dict(tunnels)


class CiscoCsrIPSecConnection(object):

    """State and actions for IPSec site-to-site connections."""

    def __init__(self, conn_info, csr):
        self.csr = csr
        self.steps = []
        self.last_status = None
        self.tunnel = conn_info['cisco']['site_conn_id']

    DIALECT_MAP = {'ike_policy': {'name': 'IKE Policy',
                                  'v1': u'v1',
                                  # auth_algorithm -> hash
                                  'sha1': u'sha',
                                  # encryption_algorithm -> encryption
                                  '3des': u'3des',
                                  'aes-128': u'aes',
                                  # TODO(pcm) update these 2 once CSR updated
                                  'aes-192': u'aes',
                                  'aes-256': u'aes',
                                  # pfs -> dhGroup
                                  'group2': 2,
                                  'group5': 5,
                                  'group14': 14},
                   'ipsec_policy': {'name': 'IPSec Policy',
                                    # auth_algorithm -> esp-authentication
                                    'sha1': u'esp-sha-hmac',
                                    # transform_protocol -> ah
                                    'esp': None,
                                    'ah': u'ah-sha-hmac',
                                    'ah-esp': u'ah-sha-hmac',
                                    # encryption_algorithm -> esp-encryption
                                    '3des': u'esp-3des',
                                    'aes-128': u'esp-aes',
                                    # TODO(pcm) update these 2 once CSR updated
                                    'aes-192': u'esp-aes',
                                    'aes-256': u'esp-aes',
                                    # pfs -> pfs
                                    'group2': u'group2',
                                    'group5': u'group5',
                                    'group14': u'group14'}}

    def translate_dialect(self, resource, attribute, info):
        """Map VPNaaS attributes values to CSR values for a resource."""
        name = self.DIALECT_MAP[resource]['name']
        if attribute not in info:
            raise CsrDriverMismatchError(resource=name, attr=attribute)
        value = info[attribute].lower()
        if value in self.DIALECT_MAP[resource]:
            return self.DIALECT_MAP[resource][value]
        raise CsrUnknownMappingError(resource=name, attr=attribute,
                                     value=value)

    def create_psk_info(self, psk_id, conn_info):
        """Collect/create attributes needed for pre-shared key."""
        return {u'keyring-name': psk_id,
                u'pre-shared-key-list': [
                    {u'key': conn_info['psk'],
                     u'encrypted': False,
                     u'peer-address': conn_info['peer_address']}]}

    def create_ike_policy_info(self, ike_policy_id, conn_info):
        """Collect/create/map attributes needed for IKE policy."""
        for_ike = 'ike_policy'
        policy_info = conn_info[for_ike]
        version = self.translate_dialect(for_ike,
                                         'ike_version',
                                         policy_info)
        encrypt_algorithm = self.translate_dialect(for_ike,
                                                   'encryption_algorithm',
                                                   policy_info)
        auth_algorithm = self.translate_dialect(for_ike,
                                                'auth_algorithm',
                                                policy_info)
        group = self.translate_dialect(for_ike,
                                       'pfs',
                                       policy_info)
        lifetime = policy_info['lifetime_value']
        return {u'version': version,
                u'priority-id': ike_policy_id,
                u'encryption': encrypt_algorithm,
                u'hash': auth_algorithm,
                u'dhGroup': group,
                u'version': version,
                u'lifetime': lifetime}

    def create_ipsec_policy_info(self, ipsec_policy_id, info):
        """Collect/create attributes needed for IPSec policy.

        Note: OpenStack will provide a default encryption algorithm, if one is
        not provided, so a authentication only configuration of (ah, sha1),
        which maps to ah-sha-hmac transform protocol, cannot be selected.
        As a result, we'll always configure the encryption algorithm, and
        will select ah-sha-hmac for transform protocol.
        """

        for_ipsec = 'ipsec_policy'
        policy_info = info[for_ipsec]
        transform_protocol = self.translate_dialect(for_ipsec,
                                                    'transform_protocol',
                                                    policy_info)
        auth_algorithm = self.translate_dialect(for_ipsec,
                                                'auth_algorithm',
                                                policy_info)
        encrypt_algorithm = self.translate_dialect(for_ipsec,
                                                   'encryption_algorithm',
                                                   policy_info)
        group = self.translate_dialect(for_ipsec, 'pfs', policy_info)
        lifetime = policy_info['lifetime_value']
        settings = {u'policy-id': ipsec_policy_id,
                    u'protection-suite': {
                        u'esp-encryption': encrypt_algorithm,
                        u'esp-authentication': auth_algorithm},
                    u'lifetime-sec': lifetime,
                    u'pfs': group,
                    # TODO(pcm): Remove when CSR fixes 'Disable'
                    u'anti-replay-window-size': u'64'}
        if transform_protocol:
            settings[u'protection-suite'][u'ah'] = transform_protocol
        return settings

    def create_site_connection_info(self, site_conn_id, ipsec_policy_id,
                                    conn_info):
        """Collect/create attributes needed for the IPSec connection."""
        # gw_ip = vpnservice['external_ip'] (need to pass in)
        mtu = conn_info['mtu']
        return {
            u'vpn-interface-name': site_conn_id,
            u'ipsec-policy-id': ipsec_policy_id,
            u'local-device': {
                # TODO(pcm): FUTURE - Get CSR port of interface with
                # local subnet
                u'ip-address': u'GigabitEthernet3',
                # TODO(pcm): FUTURE - Get IP address of router's public
                # I/F, once CSR is used as embedded router.
                u'tunnel-ip-address': u'172.24.4.23'
                # u'tunnel-ip-address': u'%s' % gw_ip
            },
            u'remote-device': {
                u'tunnel-ip-address': conn_info['peer_address']
            },
            u'mtu': mtu
        }

    def create_routes_info(self, site_conn_id, conn_info):
        """Collect/create attributes for static routes."""
        routes_info = []
        for peer_cidr in conn_info.get('peer_cidrs', []):
            route = {u'destination-network': peer_cidr,
                     u'outgoing-interface': site_conn_id}
            route_id = csr_client.make_route_id(peer_cidr, site_conn_id)
            routes_info.append((route_id, route))
        return routes_info

    def _check_create(self, resource, which):
        """Determine if REST create request was successful."""
        if self.csr.status == httplib.CREATED:
            LOG.debug("%(resource)s %(which)s is configured",
                      {'resource': resource, 'which': which})
            return
        LOG.error(_("Unable to create %(resource)s %(which)s: "
                    "%(status)d"),
                  {'resource': resource, 'which': which,
                   'status': self.csr.status})
        # ToDO(pcm): Set state to error
        raise CsrResourceCreateFailure(resource=resource, which=which)

    def do_create_action(self, action_suffix, info, resource_id, title):
        """Perform a single REST step for IPSec site connection create."""
        create_action = 'create_%s' % action_suffix
        try:
            getattr(self.csr, create_action)(info)
        except AttributeError:
            LOG.exception(_("Internal error - '%s' is not defined"),
                          create_action)
            raise CsrResourceCreateFailure(resource=title,
                                           which=resource_id)
        self._check_create(title, resource_id)
        self.steps.append(RollbackStep(action_suffix, resource_id, title))

    def _verify_deleted(self, status, resource, which):
        """Determine if REST delete request was successful."""
        if status in (httplib.NO_CONTENT, httplib.NOT_FOUND):
            LOG.debug("%(resource)s configuration %(which)s was removed",
                      {'resource': resource, 'which': which})
        else:
            LOG.warning(_("Unable to delete %(resource)s %(which)s: "
                          "%(status)d"), {'resource': resource,
                                          'which': which,
                                          'status': status})

    def do_rollback(self):
        """Undo create steps that were completed successfully."""
        for step in reversed(self.steps):
            delete_action = 'delete_%s' % step.action
            LOG.debug(_("Performing rollback action %(action)s for "
                        "resource %(resource)s"), {'action': delete_action,
                                                   'resource': step.title})
            try:
                getattr(self.csr, delete_action)(step.resource_id)
            except AttributeError:
                LOG.exception(_("Internal error - '%s' is not defined"),
                              delete_action)
                raise CsrResourceCreateFailure(resource=step.title,
                                               which=step.resource_id)
            self._verify_deleted(self.csr.status, step.title, step.resource_id)
        self.steps = []

    def create_ipsec_site_connection(self, context, conn_info):
        """Creates an IPSec site-to-site connection on CSR.

        Create the PSK, IKE policy, IPSec policy, connection, static route,
        and (future) DPD.
        """
        # Get all the IDs
        conn_id = conn_info['id']
        psk_id = conn_id
        site_conn_id = conn_info['cisco']['site_conn_id']
        ike_policy_id = conn_info['cisco']['ike_policy_id']
        ipsec_policy_id = conn_info['cisco']['ipsec_policy_id']

        LOG.debug(_('create_ipsec_site_connection for %s'), conn_id)
        # Get all the attributes needed to create
        try:
            psk_info = self.create_psk_info(psk_id, conn_info)
            ike_policy_info = self.create_ike_policy_info(ike_policy_id,
                                                          conn_info)
            ipsec_policy_info = self.create_ipsec_policy_info(ipsec_policy_id,
                                                              conn_info)
            connection_info = self.create_site_connection_info(site_conn_id,
                                                               ipsec_policy_id,
                                                               conn_info)
            routes_info = self.create_routes_info(site_conn_id, conn_info)
        except (CsrUnknownMappingError, CsrDriverMismatchError) as e:
            LOG.exception(e)
            return

        try:
            self.do_create_action('pre_shared_key', psk_info,
                                  conn_id, 'Pre-Shared Key')
            self.do_create_action('ike_policy', ike_policy_info,
                                  ike_policy_id, 'IKE Policy')
            self.do_create_action('ipsec_policy', ipsec_policy_info,
                                  ipsec_policy_id, 'IPSec Policy')
            self.do_create_action('ipsec_connection', connection_info,
                                  site_conn_id, 'IPSec Connection')

            # TODO(pcm): FUTURE - Do DPD and handle if >1 connection
            # and different DPD settings
            for route_id, route_info in routes_info:
                self.do_create_action('static_route', route_info,
                                      route_id, 'Static Route')
        except CsrResourceCreateFailure:
            self.do_rollback()
            LOG.info(_("FAILED: Create of IPSec site-to-site connection %s"),
                     conn_id)
        else:
            LOG.info(_("SUCCESS: Created IPSec site-to-site connection %s"),
                     conn_id)

    def delete_ipsec_site_connection(self, context, conn_id):
        """Delete the site-to-site IPSec connection.

        This will be best effort and will continue, if there are any
        failures.
        """
        LOG.debug(_('delete_ipsec_site_connection for %s'), conn_id)
        if not self.steps:
            LOG.warning(_('Unable to find connection %s'), conn_id)
        else:
            self.do_rollback()

        LOG.info(_("COMPLETED: Deleted IPSec site-to-site connection %s"),
                 conn_id)
