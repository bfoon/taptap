"""Keep finite Member Plan time stable across every router sync path.

Why this exists
---------------
TapTap's member system has a more precise clock than ordinary vouchers:

    member_time.expected_limit(member)

It preserves the member's actual start/end period, renewals and plan changes.

Older generic router senders use ``durations.router_limit``.  More importantly,
``core.sync`` imports that function once when the module is loaded.  A member
can therefore be repaired correctly by ``member_router_alignment`` and later be
written again by a generic/background sync using the ordinary voucher rule.

This module makes the member clock authoritative everywhere without changing
normal voucher behaviour.  It also watches TapTap-Link inventory: if MikroTik
ever reports a member as ``limit-uptime=0s``/blank (or on the wrong member
profile), TapTap queues one exact correction automatically instead of requiring
the owner to press "Fix on router" again.

No schema changes are required.
"""
from __future__ import annotations

from functools import wraps
import logging

from django.utils import timezone

logger = logging.getLogger("taptap.member_time_stability")

_ORIGINAL_ROUTER_LIMIT = None
_ORIGINAL_LINK_USERS_IMPORT = None


def member_router_limit(voucher, plan=None):
    """RouterOS limit for a member; ordinary vouchers keep the original rule."""
    global _ORIGINAL_ROUTER_LIMIT

    if getattr(voucher, "is_member", False):
        from .member_time import expected_limit
        return expected_limit(voucher)

    return _ORIGINAL_ROUTER_LIMIT(voucher, plan)


def _install_member_aware_limit():
    """Make every generic/background sender use the member clock for members.

    ``core.agent`` and ``core.voucher_push`` import durations.router_limit inside
    their functions, so replacing durations.router_limit covers them.

    ``core.sync`` imported router_limit at module-import time, so its local alias
    must also be replaced explicitly.
    """
    global _ORIGINAL_ROUTER_LIMIT

    from . import durations
    from . import sync

    current = durations.router_limit
    if getattr(current, "_taptap_member_time_stability", False):
        # Another ready() call (tests/autoreload). Reuse the already installed
        # wrapper and make sure sync's early-bound alias points at it.
        sync.router_limit = current
        return

    _ORIGINAL_ROUTER_LIMIT = current
    member_router_limit._taptap_member_time_stability = True
    durations.router_limit = member_router_limit
    sync.router_limit = member_router_limit


def _pending_alignment(voucher):
    from .models import AgentCommand

    for cmd in AgentCommand.objects.filter(
        router_id=voucher.router_id,
        kind="hotspot_users_limit",
        status__in=["queued", "sent"],
    ):
        marker = (cmd.params or {}).get("member_alignment") or {}
        try:
            voucher_id = int(marker.get("voucher_id") or 0)
        except (TypeError, ValueError):
            voucher_id = 0
        if voucher_id == voucher.pk:
            return True
    return False


def _queue_exact_link_repair(voucher):
    """Inventory confirmed drift: queue only the authoritative limit/profile set.

    The user already exists because this runs after the HotSpot-user inventory
    table has been imported, so there is no reason to enqueue a generic upsert.
    """
    from . import member_router_alignment as alignment
    from .agent import queue
    from .models import Voucher

    if _pending_alignment(voucher):
        return False

    wanted = alignment.desired(voucher)
    profile = {
        "name": wanted["profile"],
        "shared": wanted["shared"],
        "rate": wanted["rate"],
    }

    queue(
        voucher.router,
        "hotspot_users_limit",
        {
            "users": [{
                "n": voucher.code,
                "lim": wanted["limit"],
                "prof": wanted["profile"],
            }],
            "profiles": [profile],
            "member_alignment": {
                "voucher_id": voucher.pk,
                "username": voucher.code,
                "profile": wanted["profile"],
                "limit": wanted["limit"],
                "automatic": True,
                "reason": "inventory_drift",
            },
        },
        label=(
            f"Auto-align member {voucher.code}: "
            f"{wanted['profile']} / {wanted['limit']}"
        ),
        minutes=60,
    )

    Voucher.objects.filter(pk=voucher.pk).update(
        mikrotik_sync_status="Queued",
        mikrotik_sync_error="",
    )
    return True


def repair_member_drift(router):
    """Repair members whose latest real RouterOS inventory differs from TapTap.

    Returns the number of exact repairs queued.  Missing router users are left to
    the existing voucher/member provisioning logic; this function only repairs a
    user that inventory actually saw.
    """
    from .member_time import router_matches
    from .models import RouterHotspotUser, Voucher
    from . import member_router_alignment as alignment

    members = list(
        Voucher.objects
        .filter(
            router=router,
            login_type="member",
            source="taptap",
            status="active",
        )
        .select_related("router", "business")
    )
    if not members:
        return 0

    mirrors = {
        row.username.upper(): row
        for row in RouterHotspotUser.objects.filter(
            router=router,
            is_present=True,
        )
    }

    repaired = 0
    for voucher in members:
        mirror = mirrors.get(str(voucher.code).upper())
        if mirror is None:
            continue

        wanted = alignment.desired(voucher)
        have_profile = str(mirror.profile or "")
        have_limit = str(mirror.limit_uptime or "")

        time_ok = router_matches(voucher, have_limit)
        profile_ok = have_profile == wanted["profile"]
        if time_ok and profile_ok:
            continue

        try:
            if _queue_exact_link_repair(voucher):
                repaired += 1
                logger.warning(
                    "Member %s drifted on %s: router profile=%s limit=%s; "
                    "TapTap profile=%s limit=%s. Exact repair queued.",
                    voucher.code,
                    router.name,
                    have_profile or "default",
                    have_limit or "0s",
                    wanted["profile"],
                    wanted["limit"],
                )
        except Exception:
            logger.exception(
                "Could not auto-repair member %s after Link inventory",
                voucher.code,
            )

    return repaired


def _wrap_link_users_import(original):
    @wraps(original)
    def wrapped(router, rows, now):
        summary = original(router, rows, now)

        # Only after the real /ip/hotspot/user inventory is stored do we decide
        # whether the router drifted. This prevents stale UI data from generating
        # a repair and makes RouterOS itself the evidence.
        try:
            fixed = repair_member_drift(router)
            if fixed:
                summary = dict(summary or {})
                summary["member_alignments_queued"] = (
                    int(summary.get("member_alignments_queued") or 0) + fixed
                )
        except Exception as exc:
            logger.exception(
                "Member drift check failed after inventory for %s",
                router,
            )
            summary = dict(summary or {})
            errors = list(summary.get("errors") or [])
            errors.append(f"Member Plan alignment check: {exc}")
            summary["errors"] = errors

        return summary

    wrapped._taptap_member_time_stability = True
    return wrapped


def _install_inventory_guard():
    global _ORIGINAL_LINK_USERS_IMPORT

    from . import agent_inventory

    current = agent_inventory._users
    if getattr(current, "_taptap_member_time_stability", False):
        return

    _ORIGINAL_LINK_USERS_IMPORT = current
    agent_inventory._users = _wrap_link_users_import(current)


def install():
    _install_member_aware_limit()
    _install_inventory_guard()

    logger.info(
        "Member time stability enabled: member clock is authoritative for "
        "generic sends/full syncs and Link inventory drift is auto-repaired."
    )
