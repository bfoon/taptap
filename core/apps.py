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

    def import_models(self):
        """Load TapTap's split-out model modules during Django's model phase."""
        super().import_models()
        importlib.import_module("core.models_cash")
        importlib.import_module("core.models_free_access")
        importlib.import_module("core.models_member_plans")

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

        # Shared vouchers must keep each family's/device group's slots stable.
        # In particular, a random private MAC must never silently take an
        # offline device's slot on a multi-device plan.
        _install_optional(
            "shared_voucher_stability",
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

        # Business-managed HotSpot walled garden / free-access websites.
        _install_optional(
            "free_access",
        )

        # Navigation stays last.
        _install_optional(
            "account_navigation",
        )
