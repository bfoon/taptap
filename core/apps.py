from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    # TapTap Tunnel is wired in explicitly (mikrotik.connect, agent.enrollment_script,
    # live.watch_business, linkops.uses_link); no runtime monkey-patching is needed
    # except the customer voucher-state guard, support-access authorization guard,
    # and HotSpot recovery guard below.

    def ready(self):
        from . import team  # noqa: F401  (registers the sign-in counter)

        # Customer-facing expired-voucher protection.
        from .expired_portal import install as install_expired_portal
        install_expired_portal()

        # Platform support may only "view as owner" after the actual business
        # owner explicitly approves a time-limited support-access request.
        from .support_access import install as install_support_access
        install_support_access()

        # Repair sticky-voucher / captive-portal edge cases. In particular:
        # - allow the three implemented TapTap Link sticky commands;
        # - clear RouterOS HotSpot cookies whenever a customer is deliberately
        #   disconnected, disabled or expires, so the next voucher can show the
        #   login page instead of a stale automatic-login state.
        from .hotspot_recovery import install as install_hotspot_recovery
        install_hotspot_recovery()

        # Allow a recognized phone to move its binding when iOS rotates its MAC.
        from .mac_roaming import install as install_mac_roaming
        install_mac_roaming()
