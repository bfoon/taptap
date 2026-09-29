
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    # TapTap Tunnel is wired in explicitly (mikrotik.connect, agent.enrollment_script,
    # live.watch_business, linkops.uses_link); no runtime monkey-patching is needed
    # except the customer voucher-state guard installed below.

    def ready(self):
        from . import team  # noqa: F401  (registers the sign-in counter)

        # Install the customer-facing expired-voucher guard before Django loads
        # core.urls. This keeps the normal portal code intact while ensuring both
        # hosted and MikroTik-served login pages show a proper expired page.
        from .expired_portal import install
        install()

