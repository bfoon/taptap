from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    # Keep voucher expiry protection and owner-authorized support access.

    def ready(self):
        from . import team  # noqa: F401  (registers the sign-in counter)

        from .expired_portal import install as install_expired_portal
        install_expired_portal()

        from .support_access import install as install_support_access
        install_support_access()

        # Base-menu integration without replacing your customized base.html.
        from .account_navigation import install as install_account_navigation
        install_account_navigation()
