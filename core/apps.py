import importlib
import logging

from django.apps import AppConfig


logger = logging.getLogger("taptap.core")


def _install_optional(module_name, install_name="install"):
    try:
        module = importlib.import_module(
            f"core.{module_name}"
        )
    except ModuleNotFoundError as exc:
        if exc.name in {
            f"core.{module_name}",
            module_name,
        }:
            logger.info(
                "Optional TapTap module not installed: %s",
                module_name,
            )
            return False
        raise

    installer = getattr(
        module,
        install_name,
        None,
    )

    if not callable(installer):
        logger.warning(
            "TapTap module core.%s has no callable %s()",
            module_name,
            install_name,
        )
        return False

    installer()
    return True


class CoreConfig(AppConfig):
    default_auto_field = (
        "django.db.models.BigAutoField"
    )
    name = "core"

    def ready(self):
        from . import team  # noqa: F401

        _install_optional(
            "expired_portal",
        )

        _install_optional(
            "support_access",
        )

        _install_optional(
            "hotspot_recovery",
        )

        _install_optional(
            "mac_roaming",
        )

        _install_optional(
            "link_system_health",
        )

        # Voucher try-count, warning and exact temporary lockout.
        _install_optional(
            "voucher_entry_security",
        )

        _install_optional(
            "link_sync_reliability",
        )

        _install_optional(
            "protection_state_fix",
        )

        _install_optional(
            "traffic_speed",
        )

        # Navigation stays last.
        _install_optional(
            "account_navigation",
        )
