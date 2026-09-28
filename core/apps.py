from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    # TapTap Tunnel is wired in explicitly (mikrotik.connect, agent.enrollment_script,
    # live.watch_business, linkops.uses_link); no runtime monkey-patching is needed.

    def ready(self):
        from . import team  # noqa: F401  (registers the sign-in counter)
