"""Keep TapTap Member Plans and MikroTik HotSpot users exactly aligned.

Problem fixed
-------------
The Members page correctly compares a member against ``member_time.expected_limit``
and its assigned MemberPlan profile.  But the old "Fix on router" button called
the generic ``views_agents.push_one`` path.

That generic path has two important weaknesses for members:

1. TapTap Link calls ``push_pending_vouchers()``.  That function deliberately
   returns 0 when ANY other ``hotspot_users`` batch is already queued/sent.
   ``push_one`` then returns True anyway, so the UI can say the member was sent
   even though no new repair command was queued.

2. Direct/Tunnel sends the generic voucher row but never reads the user back
   from MikroTik.  A failed/ignored limit write can therefore leave the member
   at ``limit-uptime=0s`` while TapTap correctly says the plan is finite.

This module replaces only the *member* push path. Normal vouchers keep the
existing sender unchanged.

Member alignment rules
----------------------
* Assigned MemberPlan is the source of profile/devices/speed.
* ``member_time.expected_limit(member)`` is the source of RouterOS
  ``limit-uptime``.  It includes already-used time / paid renewal extensions.
* Direct API / TapTap Tunnel: write, read back, retry one precise set, then
  refuse to report success unless MikroTik actually matches.
* TapTap Link: queue a one-member upsert followed by ``hotspot_users_limit``.
  The second command is the final authoritative time/profile write and is not
  blocked by the generic bulk-voucher queue.
* While that Link repair is queued/sent, Members shows it as waiting instead of
  continuing to offer a misleading red "Fix on router" button.
* On Link ACK, TapTap's RouterHotspotUser mirror is updated immediately so the
  Members page and MikroTik do not remain visually out of alignment until the
  next full inventory sync.

No schema/migration changes are required.
"""
from __future__ import annotations

from functools import wraps
import logging

from django.utils import timezone

logger = logging.getLogger("taptap.member_router_alignment")

_ORIGINAL_PUSH = None
_ORIGINAL_ROUTER_VIEW = None
_ORIGINAL_HANDLE_ACK = None


def _member_plan(voucher, supplied=None):
    if supplied is not None:
        return supplied
    try:
        from .members import plan_for_member
        return plan_for_member(voucher)
    except Exception:
        return None


def desired(voucher, plan=None):
    """Return the exact router facts TapTap wants for one member."""
    from .member_time import expected_limit
    from .utils import voucher_profile

    plan = _member_plan(voucher, plan)
    profile, shared, rate = voucher_profile(voucher, plan)
    return {
        "plan": plan,
        "profile": profile,
        "shared": max(1, int(shared or 1)),
        "rate": rate or "",
        "limit": expected_limit(voucher),
        "password": voucher.login_password,
        "disabled": voucher.status != "active",
    }


def _mirror_success(voucher, *, profile, limit, mikrotik_id="", raw=None):
    """Make the local router mirror reflect a write that MikroTik confirmed."""
    from .models import RouterHotspotUser

    if not voucher.router_id:
        return

    now = timezone.now()
    values = {
        "business": voucher.business,
        "profile": profile or "",
        "limit_uptime": limit or "",
        "disabled": voucher.status != "active",
        "source": "taptap",
        "is_present": True,
        "last_seen_at": now,
    }
    if mikrotik_id:
        values["mikrotik_id"] = str(mikrotik_id)
    if raw is not None:
        values["raw_data"] = raw

    row = RouterHotspotUser.objects.filter(
        router_id=voucher.router_id,
        username__iexact=voucher.code,
    ).first()
    if row:
        RouterHotspotUser.objects.filter(pk=row.pk).update(**values)
    else:
        RouterHotspotUser.objects.create(
            router_id=voucher.router_id,
            username=voucher.code,
            **values,
        )


def _direct_push(voucher, wanted):
    """Write + verify one member over Direct API or TapTap Tunnel."""
    from .member_time import router_matches
    from .mikrotik import MikroTikService
    from .models import Voucher

    svc = MikroTikService(voucher.router).connect()
    try:
        svc.ensure_hotspot_profile(
            wanted["profile"],
            wanted["shared"],
            wanted["rate"],
        )
        _, item_id = svc.upsert_voucher(
            voucher.code,
            wanted["profile"],
            limit_uptime=wanted["limit"],
            password=wanted["password"],
            disabled=wanted["disabled"],
            comment=(
                f"TapTap member {voucher.code}"
                + (
                    f" · {voucher.customer_name}"
                    if voucher.customer_name
                    else ""
                )
            ),
        )

        users = svc.resource("/ip/hotspot/user")
        rows = users.get(name=voucher.code)
        if not rows:
            raise RuntimeError(
                f"MikroTik did not return member {voucher.code} after the update."
            )

        row = rows[0]
        item_id = row.get("id") or item_id or ""
        have_limit = str(
            row.get(
                "limit-uptime",
                row.get("limit_uptime", ""),
            )
            or ""
        )
        have_profile = str(row.get("profile", "") or "")

        # Retry once with the smallest possible RouterOS write. This catches
        # devices where an upsert/profile update succeeded but limit-uptime was
        # left behind.
        if (
            not router_matches(voucher, have_limit)
            or have_profile != wanted["profile"]
        ):
            users.set(
                id=item_id,
                limit_uptime=wanted["limit"],
                profile=wanted["profile"],
                disabled=(
                    "yes"
                    if wanted["disabled"]
                    else "no"
                ),
            )
            rows = users.get(name=voucher.code)
            if not rows:
                raise RuntimeError(
                    f"MikroTik lost member {voucher.code} during verification."
                )
            row = rows[0]
            have_limit = str(
                row.get(
                    "limit-uptime",
                    row.get("limit_uptime", ""),
                )
                or ""
            )
            have_profile = str(row.get("profile", "") or "")

        if not router_matches(voucher, have_limit):
            raise RuntimeError(
                "MikroTik still reports "
                f"limit-uptime={have_limit or '0s'}; "
                f"TapTap requires {wanted['limit']}."
            )
        if have_profile != wanted["profile"]:
            raise RuntimeError(
                "MikroTik still reports profile "
                f"{have_profile or 'default'}; "
                f"TapTap requires {wanted['profile']}."
            )

        _mirror_success(
            voucher,
            profile=have_profile,
            limit=have_limit,
            mikrotik_id=item_id,
            raw=dict(row),
        )
        Voucher.objects.filter(pk=voucher.pk).update(
            mikrotik_id=str(item_id),
            mikrotik_sync_status="Synced",
            mikrotik_sync_error="",
        )
        return True
    finally:
        svc.close()


def _cancel_older_alignment(router, voucher_id):
    """Cancel only stale *queued* alignment commands created by this module."""
    from .models import AgentCommand

    for cmd in AgentCommand.objects.filter(
        router=router,
        status="queued",
        kind="hotspot_users_limit",
    ):
        marker = (cmd.params or {}).get("member_alignment") or {}
        if int(marker.get("voucher_id") or 0) == int(voucher_id):
            AgentCommand.objects.filter(pk=cmd.pk).update(
                status="cancelled",
                done_at=timezone.now(),
                result="Superseded by a newer Member Plan alignment",
            )


def _link_push(voucher, wanted):
    """Queue an exact one-member repair through TapTap Link."""
    from .agent import queue
    from .models import Voucher
    from .sticky import profile_values

    _cancel_older_alignment(voucher.router, voucher.pk)

    profile = {
        "name": wanted["profile"],
        "shared": wanted["shared"],
        "rate": wanted["rate"],
    }
    user = {
        "n": voucher.code,
        "prof": wanted["profile"],
        "lim": wanted["limit"],
        "dis": wanted["disabled"],
        "pw": wanted["password"],
        "c": (
            f"TapTap member {voucher.code}"
            + (
                f" · {voucher.customer_name}"
                if voucher.customer_name
                else ""
            )
        ),
    }

    # First ensure the user exists and its password/profile are current.
    # Unlike the old push_pending_vouchers() route, this is never skipped just
    # because another voucher batch is already waiting.
    queue(
        voucher.router,
        "hotspot_users",
        {
            "users": [user],
            "profiles": [profile],
            "ids": [voucher.pk],
            "sticky": profile_values(voucher.business),
            "member_upsert": voucher.pk,
        },
        label=f"Update member {voucher.code}",
        minutes=60,
    )

    # Final authoritative time/profile write. command_body('hotspot_users_limit')
    # does not use the generic voucher-pending gate, and a RouterOS error makes
    # the command ACK fail instead of silently claiming this member is aligned.
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
            },
        },
        label=(
            f"Align member {voucher.code}: "
            f"{wanted['profile']} / {wanted['limit']}"
        ),
        minutes=60,
    )

    Voucher.objects.filter(pk=voucher.pk).update(
        mikrotik_sync_status="Queued",
        mikrotik_sync_error="",
    )
    return True


def aligned_push_one(voucher, plan=None):
    """Replacement for views_agents.push_one for members only."""
    global _ORIGINAL_PUSH

    if not getattr(voucher, "is_member", False):
        return _ORIGINAL_PUSH(voucher, plan)

    if not voucher.router:
        return "No router assigned."

    # Pull current time/status from the database. Member renewal / plan edit code
    # can update those fields immediately before this function is called.
    try:
        voucher.refresh_from_db()
    except Exception:
        pass

    plan = _member_plan(voucher, plan)
    wanted = desired(voucher, plan)

    try:
        from .linkops import uses_link

        if uses_link(voucher.router):
            return _link_push(voucher, wanted)

        return _direct_push(voucher, wanted)

    except Exception as exc:
        from .models import Voucher

        message = str(exc)[:500]
        Voucher.objects.filter(pk=voucher.pk).update(
            mikrotik_sync_status="Error",
            mikrotik_sync_error=message,
        )
        logger.exception(
            "Member router alignment failed for %s",
            voucher.code,
        )
        return message


def _pending_alignment_ids(business, vouchers):
    """Voucher ids whose exact Link repair is still queued or being applied."""
    from .models import AgentCommand

    router_ids = {
        v.router_id
        for v in vouchers
        if v.router_id
    }
    wanted_ids = {v.pk for v in vouchers}

    pending = set()
    if not router_ids:
        return pending

    for cmd in AgentCommand.objects.filter(
        router_id__in=router_ids,
        kind="hotspot_users_limit",
        status__in=["queued", "sent"],
    ):
        marker = (cmd.params or {}).get("member_alignment") or {}
        try:
            vid = int(marker.get("voucher_id") or 0)
        except (TypeError, ValueError):
            vid = 0
        if vid in wanted_ids:
            pending.add(vid)
    return pending


def _aligned_router_view(original):
    @wraps(original)
    def wrapped(business, vouchers):
        out = original(business, vouchers)
        pending = _pending_alignment_ids(
            business,
            vouchers,
        )
        for voucher in vouchers:
            if voucher.pk not in pending:
                continue
            row = out.get(voucher.pk)
            if row is None:
                row = {}
                out[voucher.pk] = row
            row.update({
                "text": (
                    "Correction queued — waiting for MikroTik "
                    "to apply the Member Plan"
                ),
                "mismatch": False,
                "time_mismatch": False,
                "profile_mismatch": False,
                "pending_fix": True,
                "missing": False,
            })
        return out

    wrapped._taptap_member_alignment = True
    return wrapped


def _finish_link_alignment(cmd):
    """Apply a successful/failed exact Link alignment to TapTap's mirror."""
    from .models import Voucher

    marker = (cmd.params or {}).get("member_alignment") or {}
    try:
        voucher_id = int(marker.get("voucher_id") or 0)
    except (TypeError, ValueError):
        voucher_id = 0
    if not voucher_id:
        return

    voucher = (
        Voucher.objects
        .select_related("router", "business")
        .filter(pk=voucher_id)
        .first()
    )
    if not voucher:
        return

    if cmd.status == "done":
        _mirror_success(
            voucher,
            profile=str(marker.get("profile") or ""),
            limit=str(marker.get("limit") or ""),
        )
        Voucher.objects.filter(pk=voucher.pk).update(
            mikrotik_sync_status="Synced",
            mikrotik_sync_error="",
        )
    elif cmd.status == "failed":
        Voucher.objects.filter(pk=voucher.pk).update(
            mikrotik_sync_status="Error",
            mikrotik_sync_error=(
                cmd.result
                or "MikroTik rejected the Member Plan alignment"
            )[:500],
        )


def _aligned_handle_ack(original):
    @wraps(original)
    def wrapped(cmd_id, given_nonce, status, result=""):
        from .models import AgentCommand

        cmd = AgentCommand.objects.filter(pk=cmd_id).first()
        handled = original(
            cmd_id,
            given_nonce,
            status,
            result,
        )
        if (
            handled
            and cmd
            and cmd.kind == "hotspot_users_limit"
            and (cmd.params or {}).get("member_alignment")
        ):
            cmd.refresh_from_db()
            _finish_link_alignment(cmd)
        return handled

    wrapped._taptap_member_alignment = True
    return wrapped


def install():
    """Install the member-only sender and Link ACK/mismatch integration once."""
    global _ORIGINAL_PUSH
    global _ORIGINAL_ROUTER_VIEW
    global _ORIGINAL_HANDLE_ACK

    from . import views_agents

    if not getattr(
        views_agents.push_one,
        "_taptap_member_alignment",
        False,
    ):
        _ORIGINAL_PUSH = views_agents.push_one
        aligned_push_one._taptap_member_alignment = True
        views_agents.push_one = aligned_push_one

    # views_members imported push_one by value. Patch its module-global name as
    # well so the existing URL callbacks use the aligned sender at runtime.
    from . import views_members

    views_members.push_one = aligned_push_one

    if not getattr(
        views_members.router_view,
        "_taptap_member_alignment",
        False,
    ):
        _ORIGINAL_ROUTER_VIEW = views_members.router_view
        views_members.router_view = _aligned_router_view(
            _ORIGINAL_ROUTER_VIEW
        )

    from . import agent

    if not getattr(
        agent.handle_ack,
        "_taptap_member_alignment",
        False,
    ):
        _ORIGINAL_HANDLE_ACK = agent.handle_ack
        agent.handle_ack = _aligned_handle_ack(
            _ORIGINAL_HANDLE_ACK
        )

    logger.info(
        "Member router alignment enabled: exact time/profile repair + verification."
    )
