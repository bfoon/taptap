from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from . import team  # noqa: F401: register the sign-in counter

        # Preserve the working expired-voucher portal protection.
        from .expired_portal import install as install_expired_portal
        install_expired_portal()

        # Preserve owner-authorized support access and the global notification.
        from .support_access import install as install_support_access
        install_support_access()

        # Preserve sticky-voucher Link commands and captive-portal recovery.
        from .hotspot_recovery import install as install_hotspot_recovery
        install_hotspot_recovery()

        # Preserve sticky-voucher MAC roaming and device-binding protection.
        from .mac_roaming import install as install_mac_roaming
        install_mac_roaming()

        # IMPORTANT: Account Management disappeared because the newer apps.py
        # omitted this existing navigation registration. Install it LAST so
        # the menu and its back buttons coexist with all guards above.
        from .account_navigation import install as install_account_navigation
        install_account_navigation()
