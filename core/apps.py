import logging

from django.apps import AppConfig

logger = logging.getLogger("taptap.tunnel")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        """Install the optional TapTap Tunnel transport."""
        try:
            from .tunnel import install_runtime_patches
            install_runtime_patches()
        except Exception:
            # Never prevent Django from starting because the optional tunnel layer
            # is unavailable or has not been migrated yet.
            logger.exception("TapTap Tunnel runtime patch could not be installed")
