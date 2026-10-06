import importlib
import logging

from django.apps import AppConfig

logger = logging.getLogger("taptap.core")

def _install_optional(module_name, install_name="install"):
    try:
        module = importlib.import_module(f"core.{module_name}")
    except ModuleNotFoundError as exc:
        if exc.name in {f"core.{module_name}", module_name}:
            logger.info("Optional TapTap module not installed: %s", module_name)
            return False
        raise
    installer = getattr(module, install_name, None)
    if not callable(installer):
        logger.warning("TapTap module core.%s has no callable %s()", module_name, install_name)
        return False
    installer()
    return True

class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def import_models(self):
        super().import_models()
        importlib.import_module("core.models_cash")
        importlib.import_module("core.models_free_access")
        importlib.import_module("core.models_member_plans")
        importlib.import_module("core.models_member_arrears")
        importlib.import_module("core.models_member_portal")
        importlib.import_module("core.models_sticky_exclusions")
        importlib.import_module("core.models_fup_manual")
        importlib.import_module("core.models_permanent_device")
        importlib.import_module("core.models_collaboration_buy")

    def ready(self):
        from . import team  # noqa: F401
        _install_optional("expired_portal")
        _install_optional("support_access")
        _install_optional("support_presence")
        _install_optional("hotspot_recovery")
        _install_optional("mac_roaming")
        _install_optional("shared_voucher_stability")
        _install_optional("shared_voucher_device_control")
        _install_optional("link_system_health")
        _install_optional("voucher_entry_security")
        _install_optional("link_sync_reliability")
        _install_optional("link_safe_command_compat")
        _install_optional("member_router_alignment")
        _install_optional("member_arrears")
        _install_optional("member_self_service")
        _install_optional("sticky_exclusions")
        _install_optional("voucher_qr")
        _install_optional("permanent_device")
        _install_optional("fup_manual")
        _install_optional("business_collaboration")
        _install_optional("online_buy")
        _install_optional("protection_state_fix")
        _install_optional("traffic_speed")
        _install_optional("free_access")
        _install_optional("account_navigation")
