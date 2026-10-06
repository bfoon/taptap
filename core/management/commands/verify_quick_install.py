"""Code/config verification for the TapTap MikroTik Quick Install."""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import agent
from core.link_trust import root_pems
from core.models import Router


def balanced(script):
    braces = 0
    quoted = False
    escaped = False
    for char in script:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
            if braces < 0:
                return False, "closing brace before opening brace"
    if quoted:
        return False, "unterminated RouterOS string"
    if braces:
        return False, f"{braces} unclosed RouterOS brace(s)"
    return True, ""


class Command(BaseCommand):
    help = "Verify TapTap Link Quick Install code/config without changing a router."

    def handle(self, *args, **options):
        failures = []
        warnings = []

        def check(name, condition, detail=""):
            if condition:
                self.stdout.write(self.style.SUCCESS(f"PASS  {name}"))
            else:
                failures.append(name)
                self.stdout.write(self.style.ERROR(f"FAIL  {name}" + (f" — {detail}" if detail else "")))

        site = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
        check("SITE_URL configured", bool(site), "Set SITE_URL=https://your-domain")
        check("SITE_URL uses HTTPS", site.startswith("https://"), site or "empty")
        check(
            "TLS verification enabled",
            bool(getattr(settings, "AGENT_VERIFY_TLS", True)),
            "AGENT_VERIFY_TLS should stay True in production",
        )

        pems = root_pems()
        check("Bundled root certificates available", len(pems) >= 1)
        check("RouterOS 6 certificate file size safe", all(len(p) < 4000 for p in pems))

        # Unsaved Router is sufficient: enrollment_script catches the missing
        # reverse RouterAgent relation and uses the default poll interval.
        router = Router(name="Quick Install Verifier")
        token = "ttl_" + ("A" * 43)
        try:
            script = agent.enrollment_script(router, token, None)
        except Exception as exc:
            raise CommandError(f"Could not generate Quick Install script: {exc}")

        ok, reason = balanced(script)
        check("Generated RouterOS syntax balance", ok, reason)

        markers = [
            "# === 1) Clock and certificates",
            "# === 2) TapTap Link",
        ]
        positions = [script.find(marker) for marker in markers]
        check("Clock/certificate stage exists", positions[0] >= 0)
        check("TapTap Link stage exists", positions[1] >= 0)
        check(
            "HTTPS trust happens before Link installation",
            positions[0] >= 0 and positions[1] > positions[0],
        )
        check(
            "TapTap Link script is installed",
            '/system script add name="taptap-link"' in script,
        )
        check(
            "TapTap Link scheduler is installed",
            '/system scheduler add name="taptap-link"' in script
            and "on-event=taptap-link" in script,
        )
        check(
            "Installer immediately tests Link",
            "/system script run taptap-link" in script,
        )
        check(
            "NTP clock repair included",
            "/system ntp client set enabled=yes" in script,
        )
        check(
            "Certificate trust store/import fallback included",
            "builtin-trust-anchors=trusted" in script
            and "/certificate import" in script,
        )

        update_pos = script.find("# === 4) Update RouterOS")
        if getattr(settings, "LINK_INSTALL_UPDATE", True):
            check("RouterOS update is last", update_pos > positions[1])
        else:
            self.stdout.write(self.style.WARNING("INFO  RouterOS auto-update disabled by LINK_INSTALL_UPDATE=False"))

        required = {"hotspot_user_mac", "hotspot_kick", "hotspot_sticky"}
        missing = required - set(agent.SAFE_KINDS)
        check(
            "Post-install device/sticky commands allowed",
            not missing,
            "Missing: " + ", ".join(sorted(missing)) if missing else "",
        )

        # This verifier deliberately does not create/rotate a real token and
        # never changes RouterOS. A real-device check-in is still the final test.
        self.stdout.write("")
        if failures:
            raise CommandError(
                f"Quick Install verification failed: {', '.join(failures)}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Quick Install code/config verification PASSED. "
                "Final verification is one DEV MikroTik check-in after pasting the generated block."
            )
        )
