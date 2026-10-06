from django.test import TestCase

from core.control_designer import CATALOG, build_plan, link_script


class ControlDesignerPlanTests(TestCase):
    def test_catalog_has_router_and_port_recipes(self):
        self.assertTrue(any(x["scope"] == "router" for x in CATALOG.values()))
        self.assertTrue(any(x["scope"] == "port" for x in CATALOG.values()))

    def test_port_state_plan_is_narrow(self):
        plan = build_plan("port_enabled", "ether3", {"enabled": "no"})
        self.assertEqual(plan[0]["resource"], "/interface")
        self.assertEqual(plan[0]["find"], {"name": "ether3"})
        self.assertEqual(plan[0]["values"]["disabled"], "yes")

    def test_wan_dhcp_plan(self):
        plan = build_plan("wan_dhcp", "ether1", {})
        resources = [x["resource"] for x in plan]
        self.assertIn("/ip/dhcp-client", resources)
        self.assertIn("/ip/firewall/nat", resources)
        body = link_script(plan)
        self.assertIn("/ip/dhcp-client", body)
        self.assertIn("ether1", body)

    def test_access_vlan_validates_vlan_range(self):
        with self.assertRaises(ValueError):
            build_plan("access_vlan", "ether2", {"bridge": "bridge1", "vlan": "5000"})

    def test_trunk_accepts_multiple_vlans(self):
        plan = build_plan("trunk_vlan", "ether4", {"bridge": "bridge1", "vlans": "10,20,30"})
        vlan_ops = [x for x in plan if x["resource"] == "/interface/bridge/vlan"]
        self.assertEqual(len(vlan_ops), 3)

    def test_static_wan_is_specific_to_port(self):
        plan = build_plan("static_wan", "ether1", {"address": "192.0.2.2/24", "gateway": "192.0.2.1"})
        self.assertEqual(plan[1]["find"]["interface"], "ether1")
        self.assertEqual(plan[2]["find"]["gateway"], "192.0.2.1")

    def test_bad_interface_rejected(self):
        with self.assertRaises(ValueError):
            build_plan("port_enabled", 'ether1"; /system reset-configuration', {"enabled": "yes"})
