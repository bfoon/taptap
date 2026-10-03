import importlib
import logging

from django.apps import AppConfig


logger = logging.getLogger("taptap.core")


def _install_optional(module_name, install_name="install"):
    """
    Install a TapTap feature module if it exists.

    This keeps CoreConfig resilient while allowing locally installed TapTap
    feature modules to coexist. A missing optional module will not bring the
    whole Django app down.
    """
    try:
        module = importlib.import_module(f"core.{module_name}")
    except ModuleNotFoundError as exc:
        # Only suppress the error when THIS optional module is missing.
        # If one of its dependencies is missing, re-raise so the real problem
        # is visible instead of being hidden.
        if exc.name in {f"core.{module_name}", module_name}:
            logger.info("Optional TapTap module not installed: %s", module_name)
            return False
        raise

    installer = getattr(module, install_name, None)
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
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Existing TapTap sign-in / business-selection registration.
        from . import team  # noqa: F401

        # Existing expired-voucher captive-portal protection.
        _install_optional("expired_portal")

        # Owner-authorized support access and support notification.
        _install_optional("support_access")

        # Sticky voucher / captive portal recovery.
        _install_optional("hotspot_recovery")

        # Sticky voucher MAC roaming and device-binding protection.
        _install_optional("mac_roaming")

        # Extended TapTap Link RouterOS health:
        # CPU, memory, storage, temperature/sensors, architecture, uptime.
        _install_optional("link_system_health")

        # RESTORE THIS FEATURE:
        # Security -> Voucher entry protection.
        #
        # Provides:
        # - configurable wrong voucher/member attempt count
        # - warning before threshold
        # - temporary device lockout
        # - blocked-until message on captive portal
        # - support phone message
        # - manual Unblock now / Unblock all in Security
        _install_optional("voucher_entry_security")

        # TapTap Link sync reliability:
        # binding ACK reconciliation, deduplication, bounded retries,
        # missing-bypass recovery and earlier failed-sync retry.
        _install_optional("link_sync_reliability")

        # Security DDoS / IDS-IPS state reconciliation:
        # queued/sent/done/failed Link commands override stale inventory until
        # a newer firewall snapshot verifies the real RouterOS state.
        _install_optional("protection_state_fix")

        # Traffic base-speed control.
        # Device > Agent > Plan > All plans; Fair Usage can still reduce more.
        _install_optional("traffic_speed")

        # Keep Account Management navigation installation LAST so its menu and
        # back-navigation hooks coexist with the protections above.
        _install_optional("account_navigation")
