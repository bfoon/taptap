from django.test import SimpleTestCase, override_settings

from . import agent
from .link_trust import root_pems
from .models import Router
from .management.commands.verify_quick_install import balanced


class QuickInstallCompatibilityTests(SimpleTestCase):
    @override_settings(
        SITE_URL="https://taptapnetwork.com",
        AGENT_VERIFY_TLS=True,
        LINK_INSTALL_UPDATE=True,
    )
    def test_script_order_and_post_install_safe_commands(self):
        router = Router(name="Verifier")
        script = agent.enrollment_script(
            router,
            "ttl_" + ("A" * 43),
            None,
        )
        ok, reason = balanced(script)
        self.assertTrue(ok, reason)
        self.assertLess(
            script.index("# === 1) Clock and certificates"),
            script.index("# === 2) TapTap Link"),
        )
        self.assertLess(
            script.index("# === 2) TapTap Link"),
            script.index("# === 4) Update RouterOS"),
        )
        self.assertIn('/system script add name="taptap-link"', script)
        self.assertIn('/system scheduler add name="taptap-link"', script)
        self.assertIn("/system script run taptap-link", script)
        self.assertIn("builtin-trust-anchors=trusted", script)
        self.assertTrue(root_pems())
        self.assertTrue(all(len(p) < 4000 for p in root_pems()))
        self.assertTrue(
            {"hotspot_user_mac", "hotspot_kick", "hotspot_sticky"}
            <= agent.SAFE_KINDS
        )
