# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, Nachi Ueno, NTT I3, Inc.
# All Rights Reserved.
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

import mock

from neutron import context as n_ctx
from neutron.db import l3_db
from neutron.extensions import vpnaas
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron.services.vpn.service_drivers import ipsec as ipsec_driver
from neutron.tests import base

_uuid = uuidutils.generate_uuid

FAKE_VPN_CONNECTION = {
    'vpnservice_id': _uuid()
}
FAKE_VPN_SERVICE = {
    'router_id': _uuid()
}
FAKE_HOST = 'fake_host'
FAKE_ROUTER_ID = _uuid()
FAKE_ROUTER = {l3_db.EXTERNAL_GW_INFO: FAKE_ROUTER_ID}
FAKE_SUBNET_ID = _uuid()


class TestIPsecDriverValidation(base.BaseTestCase):

    def setUp(self):
        super(TestIPsecDriverValidation, self).setUp()
        mock.patch('neutron.openstack.common.rpc.create_connection').start()

        self.l3_plugin = mock.Mock()
        service_plugin_p = mock.patch(
            'neutron.manager.NeutronManager.get_service_plugins')
        get_service_plugin = service_plugin_p.start()
        get_service_plugin.return_value = {
            constants.L3_ROUTER_NAT: self.l3_plugin}

        self.core_plugin = mock.Mock()
        core_plugin_p = mock.patch('neutron.manager.NeutronManager.get_plugin')
        get_plugin = core_plugin_p.start()
        get_plugin.return_value = self.core_plugin

        self.service_plugin = mock.Mock()
        self.driver = ipsec_driver.IPsecVPNDriver(self.service_plugin)
        self.context = n_ctx.Context('some_user', 'some_tenant')
        self.vpn_service = mock.Mock()


    def test_non_public_router_for_vpn_service(self):
        """Failure test of service validate, when router missing ext. I/F."""
        self.l3_plugin.get_router.return_value = {}  # No external gateway
        vpnservice = {'vpnservice': {'router_id': 123,
                                     'subnet_id': 456}}
        self.assertRaises(vpnaas.RouterIsNotExternal,
                          self.driver.validate_create_vpnservice,
                          self.context, vpnservice)

    def test_subnet_not_connected_for_vpn_service(self):
        """Failure test of service validate, when subnet not on router."""
        self.l3_plugin.get_router.return_value = FAKE_ROUTER
        self.core_plugin.get_ports.return_value = None
        vpnservice = {'vpnservice': {'router_id': FAKE_ROUTER_ID,
                                     'subnet_id': FAKE_SUBNET_ID}}
        self.assertRaises(vpnaas.SubnetIsNotConnectedToRouter,
                          self.driver.validate_create_vpnservice,
                          self.context, vpnservice)


class TestIPsecDriver(base.BaseTestCase):
    def setUp(self):
        super(TestIPsecDriver, self).setUp()
        mock.patch('neutron.openstack.common.rpc.create_connection').start()

        l3_agent = mock.Mock()
        l3_agent.host = FAKE_HOST
        plugin = mock.Mock()
        plugin.get_l3_agents_hosting_routers.return_value = [l3_agent]
        plugin_p = mock.patch('neutron.manager.NeutronManager.get_plugin')
        get_plugin = plugin_p.start()
        get_plugin.return_value = plugin
        service_plugin_p = mock.patch(
            'neutron.manager.NeutronManager.get_service_plugins')
        get_service_plugin = service_plugin_p.start()
        get_service_plugin.return_value = {constants.L3_ROUTER_NAT: plugin}

        service_plugin = mock.Mock()
        service_plugin.get_l3_agents_hosting_routers.return_value = [l3_agent]
        service_plugin._get_vpnservice.return_value = {
            'router_id': _uuid()
        }
        self.driver = ipsec_driver.IPsecVPNDriver(service_plugin)

    def _test_update(self, func, args):
        ctxt = n_ctx.Context('', 'somebody')
        with mock.patch.object(self.driver.agent_rpc, 'cast') as cast:
            func(ctxt, *args)
            cast.assert_called_once_with(
                ctxt,
                {'args': {},
                 'namespace': None,
                 'method': 'vpnservice_updated'},
                version='1.0',
                topic='ipsec_agent.fake_host')

    def test_create_ipsec_site_connection(self):
        self._test_update(self.driver.create_ipsec_site_connection,
                          [FAKE_VPN_CONNECTION])

    def test_update_ipsec_site_connection(self):
        self._test_update(self.driver.update_ipsec_site_connection,
                          [FAKE_VPN_CONNECTION, FAKE_VPN_CONNECTION])

    def test_delete_ipsec_site_connection(self):
        self._test_update(self.driver.delete_ipsec_site_connection,
                          [FAKE_VPN_CONNECTION])

    def test_update_vpnservice(self):
        self._test_update(self.driver.update_vpnservice,
                          [FAKE_VPN_SERVICE, FAKE_VPN_SERVICE])

    def test_delete_vpnservice(self):
        self._test_update(self.driver.delete_vpnservice,
                          [FAKE_VPN_SERVICE])
