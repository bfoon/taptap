from unittest import mock

from django.test import SimpleTestCase

from .control_designer import (
    advanced_link_impact,
    build_plan,
    link_impact,
)


class LinkSafetyGuardTests(SimpleTestCase):
    class Router:
        connection_mode = "agent"

    def setUp(self):
        self.router = self.Router()

    def test_dns_change_warns_for_link(self):
        plan = build_plan(
            "dns",
            "",
            {"servers": "1.1.1.1,8.8.8.8", "allow_remote": "yes"},
        )
        impact = link_impact(
            self.router,
            "dns",
            "",
            {"servers": "1.1.1.1,8.8.8.8", "allow_remote": "yes"},
            plan,
        )
        self.assertTrue(impact["affects"])
        self.assertEqual(impact["severity"], "critical")
        self.assertTrue(any("DNS" in reason for reason in impact["reasons"]))

    def test_identity_change_does_not_claim_link_risk(self):
        plan = build_plan("identity", "", {"identity": "Branch-Router"})
        impact = link_impact(
            self.router,
            "identity",
            "",
            {"identity": "Branch-Router"},
            plan,
        )
        self.assertFalse(impact["affects"])

    @mock.patch(
        "core.control_designer._port_link_risk",
        return_value=("", ""),
    )
    def test_ordinary_lan_port_state_has_no_false_link_warning(self, _risk):
        plan = build_plan("port_enabled", "ether5", {"enabled": "yes"})
        impact = link_impact(
            self.router,
            "port_enabled",
            "ether5",
            {"enabled": "yes"},
            plan,
        )
        self.assertFalse(impact["affects"])

    @mock.patch(
        "core.control_designer._port_link_risk",
        return_value=(
            "wan",
            "This port carries Internet traffic (a WAN link).",
        ),
    )
    def test_protected_wan_port_change_warns(self, _risk):
        plan = build_plan("access_vlan", "ether1", {"bridge": "bridge1", "vlan": "20"})
        impact = link_impact(
            self.router,
            "access_vlan",
            "ether1",
            {"bridge": "bridge1", "vlan": "20"},
            plan,
        )
        self.assertTrue(impact["affects"])
        self.assertEqual(impact["severity"], "critical")

    def test_raw_script_and_scheduler_are_link_sensitive(self):
        for path in ("/system/script", "/system/scheduler"):
            impact = advanced_link_impact(self.router, path)
            self.assertTrue(impact["affects"])
            self.assertEqual(impact["severity"], "critical")

    def test_unrelated_queue_resource_does_not_raise_link_warning(self):
        impact = advanced_link_impact(self.router, "/queue/simple")
        self.assertFalse(impact["affects"])


class InspectorRecipeDefaultsTests(SimpleTestCase):
    def test_access_vlan_plan_accepts_adjusted_parameters(self):
        plan = build_plan(
            "access_vlan",
            "ether3",
            {"bridge": "bridge-lan", "vlan": "40"},
        )
        self.assertEqual(plan[0]["values"]["pvid"], "40")
        self.assertEqual(plan[1]["values"]["untagged"], "ether3")

    def test_static_wan_remains_high_impact_recipe_shape(self):
        plan = build_plan(
            "static_wan",
            "ether1",
            {"address": "192.0.2.20/24", "gateway": "192.0.2.1"},
        )
        self.assertTrue(any(op["resource"] == "/ip/route" for op in plan))
        self.assertTrue(any(op["resource"] == "/ip/firewall/nat" for op in plan))
