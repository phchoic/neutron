import mock
import testtools

from neutron.common import exceptions as n_exc
from neutron.openstack.common import log as logging
from neutron.tests import base

from neutron.plugins.cisco.l3.plugging_drivers.ml2_ovs_plugging_driver import(
    ML2OVSPluggingDriver)


LOG = logging.getLogger(__name__)


class TestMl2OvsPluggingDriver(base.BaseTestCase):

    def setUp(self):
        super(TestMl2OvsPluggingDriver, self).setUp()
        # patcher = mock.patch('neutron.manager.NeutronManager')
        # self.manager_cls = patcher.start()
        # self.addCleanup(patcher.stop)
        # self.manager = mock.MagicMock()
        # self.manager_cls.return_value = self.manager
        # self.core_plugin = mock.MagicMock
        # self.manager.get_plugin = self.core_plugin
        # self.ml2_ovs_plugging_driver = ML2OVSPluggingDriver()


    # def test_delete_resource_port_fail_always(self):
    #     self.skipTest("")
    #     mgmt_port_id = 'fake_port_id'
    #
    #     self.core_plugin.delete_port = mock.MagicMock(
    #         side_effect=n_exc.NeutronException)
    #     kwargs = {}
    #     self.assertRaises(
    #         n_exc.NeutronException,
    #         self.ml2_ovs_plugging_driver._delete_resource_port,
    #         mgmt_port_id)

    def test_delete_resource_port_fail_always(self):
        mgmt_port_id = 'fake_port_id'
        mocked_plugin = mock.MagicMock()
        mocked_plugin.delete_port = mock.MagicMock(
            side_effect=n_exc.NeutronException)

        with mock.patch.object(ML2OVSPluggingDriver, '_core_plugin') as \
                plugin:
            plugin.__get__ = mock.MagicMock(return_value=mocked_plugin)
            ml2_ovs_plugging_driver = ML2OVSPluggingDriver()
            self.assertRaises(
                n_exc.NeutronException,
                ml2_ovs_plugging_driver._delete_resource_port,
                mgmt_port_id)

    def test_delete_resource_port_fail_only_twice(self):
        mgmt_port_id = 'fake_port_id'
        mocked_plugin = mock.MagicMock()
        mocked_plugin.delete_port = mock.MagicMock(
            side_effect=[n_exc.NeutronException, n_exc.NeutronException,
                         mock.Mock])
        with mock.patch.object(ML2OVSPluggingDriver, '_core_plugin') as plugin:
            plugin.__get__ = mock.MagicMock(return_value=mocked_plugin)
            ml2_ovs_plugging_driver = ML2OVSPluggingDriver()
            ml2_ovs_plugging_driver._delete_resource_port(mgmt_port_id)
            self.assertEquals(3, mocked_plugin.delete_port.call_count)

    def test_delete_resource_port_handle_port_not_found(self):
        mgmt_port_id = 'fake_port_id'
        mocked_plugin = mock.MagicMock()
        mocked_plugin.delete_port = mock.MagicMock(
            side_effect=n_exc.PortNotFound(port_id=mgmt_port_id))
        with mock.patch.object(ML2OVSPluggingDriver, '_core_plugin') as plugin:
            plugin.__get__ = mock.MagicMock(return_value=mocked_plugin)
            ml2_ovs_plugging_driver = ML2OVSPluggingDriver()
            ml2_ovs_plugging_driver._delete_resource_port(mgmt_port_id)
            self.assertEquals(1, mocked_plugin.delete_port.call_count)
