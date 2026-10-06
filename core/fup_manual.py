"""Manual Fair Usage step rollback.

TapTap normally derives the Fair Usage step from measured bytes every live sync.
This module adds a staff override that is valid only for the current policy
period. It changes the EFFECTIVE router cap without erasing measured usage.

Examples:
    automatic step 3 -> manual step 0  (full speed)
    automatic step 3 -> manual step 1
    automatic step 3 -> manual step 2
    Automatic         -> give control back to the normal thresholds

Shared vouchers are controlled per device. IP-binding bypass FUP states are also
supported because they use the same FairUsageState model.
"""
from __future__ import annotations

import copy
import logging
import re
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models_fup import FairUsageState
from .models_fup_manual import FairUsageManualStep

logger = logging.getLogger("taptap.fup_manual")

_INSTALLED = False
_ORIGINAL = {}


def _business(request):
    return getattr(request, "tt_business", None) or request.user.business


def _allowed(request):
    business = _business(request)
    if request.user.is_superuser or getattr(business, "user_id", None) == request.user.id:
        return True
    perms = getattr(request, "tt_perms", frozenset())
    return bool({"network.manage", "vouchers.support"} & set(perms))


def _safe_next(request, default="/security/fair-usage/manual-steps/"):
    value = str(request.POST.get("next") or request.GET.get("next") or "")
    return value if value.startswith("/") and not value.startswith("//") else default


def _period_key(state, now=None):
    """Current period key for this state, independent of the last live sync."""
    from . import fair_usage as fu

    now = now or timezone.now()
    if state.voucher_id:
        return fu.window(state.policy, state.voucher, now)[1]
    if state.device.startswith("bypass:"):
        mac = state.device[len("bypass:"):]
        return fu.bypass_window(state.policy, state.policy.business, mac, now)[1]
    return state.period_key


def _valid_override(state, now=None, delete_stale=False):
    try:
        override = state.manual_override
    except FairUsageManualStep.DoesNotExist:
        return None

    current = _period_key(state, now)
    if override.period_key != current:
        if delete_stale:
            override.delete()
        return None

    if override.tier > len(state.policy.tiers or []):
        # A policy may have been edited and now contain fewer steps.
        # Do not silently map an old Step 4 override onto a different policy.
        if delete_stale:
            override.delete()
        return None
    return override


def _active_lift(state, now=None):
    now = now or timezone.now()
    if state.lifted_until and state.lifted_until > now:
        return True
    if state.voucher_id and state.device:
        whole = FairUsageState.objects.filter(
            voucher_id=state.voucher_id,
            policy_id=state.policy_id,
            device="",
            lifted_until__gt=now,
        ).exists()
        if whole:
            return True
    return False


def automatic_tier(state):
    from . import fair_usage as fu
    return fu.tier_for(int(state.used_bytes or 0), state.policy.tiers or [])


def effective_tier(state, now=None):
    """Effective step after lift/manual override, without changing usage."""
    now = now or timezone.now()
    if _active_lift(state, now):
        return 0
    override = _valid_override(state, now)
    if override is not None:
        return int(override.tier)
    return automatic_tier(state)


def set_manual_step(state, tier, user=None, now=None):
    """Roll one state back to a chosen step for the current policy period."""
    from . import fair_usage as fu
    from .utils import log

    now = now or timezone.now()
    tiers = state.policy.tiers or []
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        raise ValueError("Choose a valid fair-usage step.")

    auto = automatic_tier(state)
    max_allowed = min(auto, len(tiers))
    if tier < 0 or tier > max_allowed:
        raise ValueError(
            f"This customer has naturally reached Step {auto}. "
            f"Choose Step 0 to Step {max_allowed}, or Automatic."
        )

    current_key = _period_key(state, now)
    who = user if getattr(user, "is_authenticated", False) else None

    # A manual step is the source of truth now, so an older temporary lift must
    # not hide it.
    if state.lifted_until or state.lifted_by_id:
        state.lifted_until = None
        state.lifted_by = None
        state.save(update_fields=["lifted_until", "lifted_by", "updated_at"])

    # A whole-voucher lift would otherwise force a shared device to Step 0 even
    # when staff explicitly chose Step 1/2 for this device.
    if state.voucher_id and state.device and tier > 0:
        FairUsageState.objects.filter(
            voucher_id=state.voucher_id,
            policy_id=state.policy_id,
            device="",
        ).update(lifted_until=None, lifted_by=None)

    override, _ = FairUsageManualStep.objects.update_or_create(
        state=state,
        defaults={
            "period_key": current_key,
            "tier": tier,
            "set_by": who,
        },
    )

    if state.voucher_id:
        from .voucher_history import record

        subject = state.device_label or state.device
        prefix = f"{subject}: " if state.device else ""
        speed = (
            "full speed"
            if tier == 0
            else (
                f"{fu.speed_text(tiers[tier - 1]['down'])} down / "
                f"{fu.speed_text(tiers[tier - 1]['up'])} up"
            )
        )
        record(
            state.voucher,
            "fup_restored",
            user=user,
            reason="Manual fair-usage rollback",
            text=(
                f"{prefix}manually rolled back from automatic Step {auto} "
                f"to Step {tier} ({speed}) for the current {state.policy.get_period_display().lower()}. "
                "Usage counters were not reset."
            ),
        )
    else:
        label = state.device_label or state.device.replace("bypass:", "")
        log(
            state.policy.business,
            "Fair Usage",
            (
                f"{label}: manual rollback automatic Step {auto} → Step {tier} "
                f"({state.policy.name}); usage counters unchanged"
            ),
        )

    fu.refresh(state.policy.business)
    return override, auto


def clear_manual_step(state, user=None):
    """Return a state to automatic threshold calculation immediately."""
    from . import fair_usage as fu
    from .utils import log

    deleted, _ = FairUsageManualStep.objects.filter(state=state).delete()

    # "Automatic" also cancels an old temporary Full speed lift for this exact
    # state so the policy really does resume.
    changed = False
    if state.lifted_until or state.lifted_by_id:
        state.lifted_until = None
        state.lifted_by = None
        state.save(update_fields=["lifted_until", "lifted_by", "updated_at"])
        changed = True

    if state.voucher_id:
        from .voucher_history import record

        if deleted or changed:
            subject = state.device_label or state.device
            prefix = f"{subject}: " if state.device else ""
            record(
                state.voucher,
                "fup_restored",
                user=user,
                reason="Automatic fair usage restored",
                text=(
                    f"{prefix}manual speed-step override removed. "
                    "Fair usage now follows measured data and policy thresholds again."
                ),
            )
    elif deleted or changed:
        label = state.device_label or state.device.replace("bypass:", "")
        log(
            state.policy.business,
            "Fair Usage",
            f"{label}: manual step override removed; automatic thresholds restored",
        )

    fu.refresh(state.policy.business)
    return bool(deleted or changed)


def _voucher_sessions(router, active):
    """Resolve active voucher/alias codes to {voucher_id: [(ip, mac), ...]}."""
    from .models import Voucher, VoucherCodeAlias
    from . import fair_usage as fu

    by_code = {}
    bypass = {}
    for session in active or []:
        code = str(session.get("user", "")).strip()
        ip = str(session.get("address", "")).strip()
        mac = str(session.get("mac-address", "")).upper()
        if not code or not fu.IP_RE.match(ip):
            continue
        if code.upper().startswith(fu.BYPASS):
            bypass.setdefault(code[len(fu.BYPASS):].upper(), set()).add(ip)
        else:
            by_code.setdefault(code.upper(), []).append((ip, mac))

    if not by_code:
        return {}, bypass

    variants = list(by_code) + [c.lower() for c in by_code]
    vouchers = {
        v.code.upper(): v
        for v in Voucher.objects.filter(
            business=router.business,
            code__in=variants,
        )
    }
    for alias in VoucherCodeAlias.objects.filter(
        business=router.business,
        code__in=list(by_code),
    ).select_related("voucher"):
        vouchers.setdefault(alias.code.upper(), alias.voucher)

    out = {}
    for code, sessions in by_code.items():
        voucher = vouchers.get(code)
        if voucher and not voucher.deleted_at:
            out.setdefault(voucher.pk, {"voucher": voucher, "sessions": []})
            out[voucher.pk]["sessions"].extend(sessions)
    return out, bypass


def _apply_manual_caps(router, active, caps, now=None):
    """Modify automatic desired caps with period-valid manual overrides."""
    from . import fair_usage as fu

    now = now or timezone.now()
    voucher_sessions, bypass = _voucher_sessions(router, active)

    overrides = list(
        FairUsageManualStep.objects.filter(
            state__policy__business=router.business,
            state__policy__active=True,
        ).select_related(
            "state__policy",
            "state__voucher",
        )
    )
    if not overrides:
        return caps

    for override in overrides:
        state = override.state
        if override.period_key != _period_key(state, now):
            override.delete()
            continue

        tiers = state.policy.tiers or []
        tier = int(override.tier)
        if tier > len(tiers):
            override.delete()
            continue

        if state.voucher_id:
            item = voucher_sessions.get(state.voucher_id)
            if not item:
                continue
            voucher = item["voucher"]
            sessions = item["sessions"]

            # Shared vouchers are controlled per device. A legacy whole-voucher
            # state is intentionally ignored for a shared voucher.
            if fu.shared(voucher):
                if not state.device:
                    continue
                bindings = list(voucher.device_bindings.all())
                ips = set()
                for ip, mac in sessions:
                    key, _, _ = fu.device_of(voucher, mac, bindings)
                    if key == state.device:
                        ips.add(ip)
            else:
                if state.device:
                    continue
                ips = {ip for ip, _ in sessions}

            for ip in ips:
                name = fu._qname(voucher.code, ip)
                caps.pop(name, None)
                if tier:
                    speed = tiers[tier - 1]
                    caps[name] = (
                        ip,
                        f"{fu.kbps(speed['up'])}k/{fu.kbps(speed['down'])}k",
                        voucher.code,
                        tier,
                    )
        else:
            if not state.device.startswith("bypass:"):
                continue
            mac = state.device[len("bypass:"):].upper()
            ips = bypass.get(mac, set())
            tag = "BP" + mac.replace(":", "")
            for ip in ips:
                name = fu._qname(tag, ip)
                caps.pop(name, None)
                if tier:
                    speed = tiers[tier - 1]
                    caps[name] = (
                        ip,
                        f"{fu.kbps(speed['up'])}k/{fu.kbps(speed['down'])}k",
                        state.device_label or mac,
                        tier,
                    )
    return caps


def _decorate_status(info, voucher, now=None):
    """Make voucher-detail FUP status reflect the effective manual step."""
    from . import fair_usage as fu

    if not info:
        return info
    now = now or timezone.now()
    policy = info["policy"]
    tiers = policy.tiers or []

    if info.get("per_device"):
        states = {
            state.device: state
            for state in FairUsageState.objects.filter(
                voucher=voucher,
                policy=policy,
            ).exclude(device="")
        }
        for device in info.get("devices", []):
            state = states.get(device.get("key"))
            if not state:
                continue
            auto = fu.tier_for(int(device.get("used") or 0), tiers)
            override = _valid_override(state, now)
            if override is not None and not _active_lift(state, now):
                tier = int(override.tier)
                device["tier"] = tier
                device["speed"] = fu.speed_text(tiers[tier - 1]["down"]) if tier else ""
                device["next_gb"] = tiers[tier]["gb"] if tier < len(tiers) else None
                device["manual_tier"] = tier
                device["automatic_tier"] = auto

        effective = [int(d.get("tier") or 0) for d in info.get("devices", [])]
        tier = max(effective or [0])
        info["tier"] = tier
        info["speed"] = (
            {
                "down": fu.speed_text(tiers[tier - 1]["down"]),
                "up": fu.speed_text(tiers[tier - 1]["up"]),
            }
            if tier
            else None
        )
        info["next"] = (
            {"gb": tiers[tier]["gb"], "down": fu.speed_text(tiers[tier]["down"])}
            if tier < len(tiers)
            else None
        )
        return info

    state = FairUsageState.objects.filter(
        voucher=voucher,
        policy=policy,
        device="",
    ).first()
    if not state:
        return info
    override = _valid_override(state, now)
    if override is None or _active_lift(state, now):
        return info

    auto = fu.tier_for(int(info.get("used") or 0), tiers)
    tier = int(override.tier)
    info["automatic_tier"] = auto
    info["manual_tier"] = tier
    info["tier"] = tier
    info["speed"] = (
        {
            "down": fu.speed_text(tiers[tier - 1]["down"]),
            "up": fu.speed_text(tiers[tier - 1]["up"]),
        }
        if tier
        else None
    )
    info["next"] = (
        {"gb": tiers[tier]["gb"], "down": fu.speed_text(tiers[tier]["down"])}
        if tier < len(tiers)
        else None
    )
    for index, step in enumerate(info.get("steps", []), start=1):
        step["current"] = index == tier
    return info


def _decorate_devices(rows, voucher, policy, now=None):
    from . import fair_usage as fu

    now = now or timezone.now()
    tiers = policy.tiers or []
    states = {
        state.device: state
        for state in FairUsageState.objects.filter(
            voucher=voucher,
            policy=policy,
        ).exclude(device="")
    }
    for row in rows:
        state = states.get(row.get("key"))
        if not state or row.get("exempt") or row.get("blocked"):
            continue
        override = _valid_override(state, now)
        if override is None or _active_lift(state, now):
            continue
        auto = fu.tier_for(int(row.get("used") or 0), tiers)
        tier = int(override.tier)
        row["automatic_tier"] = auto
        row["manual_tier"] = tier
        row["tier"] = tier
        row["cap"] = fu.speed_text(tiers[tier - 1]["down"]) if tier else ""
        row["cap_up"] = fu.speed_text(tiers[tier - 1]["up"]) if tier else ""
        row["next_gb"] = tiers[tier]["gb"] if tier < len(tiers) else None
    return rows


def _install_fair_usage_wrappers():
    from . import fair_usage as fu

    if getattr(fu.desired_caps, "_manual_fup_installed", False):
        return

    _ORIGINAL["desired_caps"] = fu.desired_caps
    _ORIGINAL["status"] = fu.status
    _ORIGINAL["card_devices"] = fu.card_devices
    _ORIGINAL["slowed"] = fu.slowed
    _ORIGINAL["slowed_bypass"] = fu.slowed_bypass
    _ORIGINAL["bypass_status"] = fu.bypass_status
    _ORIGINAL["bandwidth"] = fu.bandwidth
    _ORIGINAL["lift"] = fu.lift
    _ORIGINAL["unlift"] = fu.unlift

    @wraps(fu.desired_caps)
    def desired_caps(router, active, now=None):
        caps = _ORIGINAL["desired_caps"](router, active, now)
        return _apply_manual_caps(router, active, caps, now)

    desired_caps._manual_fup_installed = True
    fu.desired_caps = desired_caps

    @wraps(fu.status)
    def status(voucher, now=None):
        return _decorate_status(_ORIGINAL["status"](voucher, now), voucher, now)

    fu.status = status

    @wraps(fu.card_devices)
    def card_devices(voucher, policy, now=None):
        rows = _ORIGINAL["card_devices"](voucher, policy, now)
        return _decorate_devices(rows, voucher, policy, now)

    fu.card_devices = card_devices

    @wraps(fu.bandwidth)
    def bandwidth(voucher, state, now=None):
        override = _valid_override(state, now)
        if override is None or _active_lift(state, now):
            return _ORIGINAL["bandwidth"](voucher, state, now)
        proxy = copy.copy(state)
        proxy.tier = int(override.tier)
        return _ORIGINAL["bandwidth"](voucher, proxy, now)

    fu.bandwidth = bandwidth

    @wraps(fu.slowed)
    def slowed(business, now=None):
        rows = _ORIGINAL["slowed"](business, now)
        now2 = now or timezone.now()
        out = []
        for row in rows:
            state = row["state"]
            policy = row["policy"]
            tiers = policy.tiers or []

            if row.get("devices"):
                devices = row["devices"]  # already decorated by wrapped card_devices()
                row["slowed_devices"] = sum(1 for d in devices if int(d.get("tier") or 0) > 0)
                tier = max([int(d.get("tier") or 0) for d in devices] or [0])
                if tier == 0:
                    continue
                row["tier"] = tier
                row["next"] = (
                    {"gb": tiers[tier]["gb"], "down": fu.speed_text(tiers[tier]["down"])}
                    if tier < len(tiers)
                    else None
                )
                proxy = copy.copy(state)
                proxy.tier = tier
                row.update(_ORIGINAL["bandwidth"](row["voucher"], proxy, now2))
            else:
                override = _valid_override(state, now2)
                if override is not None and not _active_lift(state, now2):
                    tier = int(override.tier)
                    if tier == 0:
                        continue
                    row["automatic_tier"] = automatic_tier(state)
                    row["manual_tier"] = tier
                    row["tier"] = tier
                    row["next"] = (
                        {"gb": tiers[tier]["gb"], "down": fu.speed_text(tiers[tier]["down"])}
                        if tier < len(tiers)
                        else None
                    )
                    proxy = copy.copy(state)
                    proxy.tier = tier
                    row.update(_ORIGINAL["bandwidth"](row["voucher"], proxy, now2))
            out.append(row)
        return out

    fu.slowed = slowed

    @wraps(fu.slowed_bypass)
    def slowed_bypass(business, now=None):
        rows = _ORIGINAL["slowed_bypass"](business, now)
        now2 = now or timezone.now()
        out = []
        for row in rows:
            mac = str(row.get("mac") or "").upper()
            state = FairUsageState.objects.filter(
                voucher__isnull=True,
                policy=row["policy"],
                device="bypass:" + mac,
            ).first()
            if not state:
                out.append(row)
                continue
            override = _valid_override(state, now2)
            if override is None or _active_lift(state, now2):
                out.append(row)
                continue
            tier = int(override.tier)
            if tier == 0:
                continue
            tiers = state.policy.tiers or []
            row["automatic_tier"] = automatic_tier(state)
            row["manual_tier"] = tier
            row["tier"] = tier
            step = tiers[tier - 1]
            row["cap"] = {
                "down": fu.speed_text(step["down"]),
                "up": fu.speed_text(step["up"]),
            }
            out.append(row)
        return out

    fu.slowed_bypass = slowed_bypass

    @wraps(fu.bypass_status)
    def bypass_status(business, macs, now=None):
        data = _ORIGINAL["bypass_status"](business, macs, now)
        now2 = now or timezone.now()
        if not data:
            return data
        pol = fu.bypass_policy(fu.policies(business))
        if not pol:
            return data
        for mac in list(data):
            state = FairUsageState.objects.filter(
                voucher__isnull=True,
                policy=pol,
                device="bypass:" + str(mac).upper(),
            ).first()
            if not state:
                continue
            override = _valid_override(state, now2)
            if override is None or _active_lift(state, now2):
                continue
            tier = int(override.tier)
            tiers = pol.tiers or []
            data[mac]["automatic_tier"] = automatic_tier(state)
            data[mac]["manual_tier"] = tier
            data[mac]["tier"] = tier
            data[mac]["speed"] = fu.speed_text(tiers[tier - 1]["down"]) if tier else ""
            data[mac]["next_gb"] = tiers[tier]["gb"] if tier < len(tiers) else None
        return data

    fu.bypass_status = bypass_status

    @wraps(fu.lift)
    def lift(voucher, user=None, now=None):
        FairUsageManualStep.objects.filter(state__voucher=voucher).delete()
        result = _ORIGINAL["lift"](voucher, user, now)
        fu.refresh(voucher.business)
        return result

    fu.lift = lift

    @wraps(fu.unlift)
    def unlift(voucher, user=None):
        FairUsageManualStep.objects.filter(state__voucher=voucher).delete()
        result = _ORIGINAL["unlift"](voucher, user)
        fu.refresh(voucher.business)
        return result

    fu.unlift = unlift


def _row_from_state(state, now=None):
    from . import fair_usage as fu

    now = now or timezone.now()
    # Ignore stale rows from an old accounting period unless a stale override
    # exists, in which case remove the stale override too.
    current_key = _period_key(state, now)
    override = _valid_override(state, now, delete_stale=True)
    if state.period_key != current_key and override is None:
        return None

    auto = automatic_tier(state)
    if auto <= 0 and override is None:
        return None

    if state.voucher_id and fu.shared(state.voucher) and not state.device:
        # Shared vouchers are intentionally controlled per device.
        return None

    lifted = _active_lift(state, now)
    effective = 0 if lifted else (int(override.tier) if override is not None else auto)
    tiers = state.policy.tiers or []
    choices = [{"value": 0, "label": "Step 0 — Full speed"}]
    for i in range(1, min(auto, len(tiers)) + 1):
        tier = tiers[i - 1]
        choices.append(
            {
                "value": i,
                "label": (
                    f"Step {i} — {fu.speed_text(tier['down'])} down / "
                    f"{fu.speed_text(tier['up'])} up"
                ),
            }
        )

    if state.voucher_id:
        subject = state.voucher.code
        detail = state.device_label or state.device or state.voucher.plan_name
        router = state.voucher.router.name if state.voucher.router_id else "No router"
        url = f"/vouchers/{state.voucher_id}/"
        kind = "Shared device" if state.device else "Voucher"
    else:
        subject = state.device_label or state.device.replace("bypass:", "")
        detail = state.device.replace("bypass:", "")
        router = "IP-binding bypass"
        url = ""
        kind = "Bypass device"

    return {
        "state": state,
        "subject": subject,
        "detail": detail,
        "router": router,
        "url": url,
        "kind": kind,
        "policy": state.policy,
        "used_text": fu.size_text(state.used_bytes),
        "automatic": auto,
        "effective": effective,
        "manual": override is not None,
        "manual_tier": int(override.tier) if override is not None else None,
        "lifted": lifted,
        "choices": choices,
    }


@login_required
def manual_steps(request):
    if not _allowed(request):
        raise PermissionDenied

    business = _business(request)
    q = str(request.GET.get("q") or "").strip()
    policy_filter = str(request.GET.get("policy") or "").strip()

    qs = (
        FairUsageState.objects.filter(
            policy__business=business,
            policy__active=True,
        )
        .filter(Q(voucher__isnull=True) | Q(voucher__deleted_at__isnull=True))
        .select_related(
            "policy",
            "voucher",
            "voucher__router",
        )
        .order_by("-updated_at", "-tier", "-used_bytes")
    )

    if policy_filter.isdigit():
        qs = qs.filter(policy_id=int(policy_filter))

    rows = []
    for state in qs[:3000]:
        row = _row_from_state(state)
        if not row:
            continue
        if q:
            haystack = " ".join(
                [
                    row["subject"],
                    row["detail"],
                    row["policy"].name,
                    row["router"],
                ]
            ).lower()
            if q.lower() not in haystack:
                continue
        rows.append(row)

    # Manual overrides first, then naturally highest step / usage.
    rows.sort(
        key=lambda row: (
            0 if row["manual"] else 1,
            -row["automatic"],
            -int(row["state"].used_bytes or 0),
        )
    )

    return render(
        request,
        "core/fair_usage_manual.html",
        {
            "rows": rows[:500],
            "query": q,
            "policy_filter": policy_filter,
            "policies": business.fair_usage_policies.filter(active=True).order_by("name"),
            "manual_count": sum(1 for row in rows if row["manual"]),
        },
    )


@login_required
@require_POST
def manual_step_action(request, state_id):
    if not _allowed(request):
        raise PermissionDenied

    business = _business(request)
    state = get_object_or_404(
        FairUsageState.objects.select_related(
            "policy",
            "voucher",
        ),
        pk=state_id,
        policy__business=business,
    )

    action = str(request.POST.get("action") or "set")
    try:
        if action == "auto":
            changed = clear_manual_step(state, request.user)
            if changed:
                messages.success(
                    request,
                    "Automatic fair usage restored. The next live sync applies the normal threshold step.",
                )
            else:
                messages.info(request, "This customer is already using automatic fair usage.")
        else:
            override, auto = set_manual_step(
                state,
                request.POST.get("tier"),
                request.user,
            )
            label = state.device_label or (
                state.voucher.code if state.voucher_id else state.device.replace("bypass:", "")
            )
            if override.tier == 0:
                speed = "full speed"
            else:
                step = state.policy.tiers[override.tier - 1]
                from . import fair_usage as fu
                speed = (
                    f"{fu.speed_text(step['down'])} down / "
                    f"{fu.speed_text(step['up'])} up"
                )
            messages.success(
                request,
                (
                    f"{label}: rolled back from automatic Step {auto} "
                    f"to Step {override.tier} ({speed}). "
                    "Usage data was kept. The router updates on the next live sync."
                ),
            )
    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect(_safe_next(request))


def _install_urls():
    from . import urls

    names = {getattr(item, "name", None) for item in urls.urlpatterns}
    if "fup_manual_steps" not in names:
        urls.urlpatterns.append(
            path(
                "security/fair-usage/manual-steps/",
                manual_steps,
                name="fup_manual_steps",
            )
        )
    if "fup_manual_step_action" not in names:
        urls.urlpatterns.append(
            path(
                "security/fair-usage/manual-steps/<int:state_id>/",
                manual_step_action,
                name="fup_manual_step_action",
            )
        )


MANAGE_BUTTON = (
    '<a class="btn btn-sm btn-outline-primary" '
    'href="/security/fair-usage/manual-steps/">'
    '<i class="bi bi-arrow-counterclockwise"></i> Roll back steps</a>'
)


def _inject_manage_button(request, response):
    if request.path.rstrip("/") != "/security/fair-usage/slowed":
        return response
    if getattr(response, "streaming", False) or response.status_code != 200:
        return response
    if "text/html" not in response.get("Content-Type", "").lower():
        return response

    body = response.content.decode(response.charset or "utf-8")
    if "Roll back steps" in body:
        return response

    marker = '<form method="get" class="d-flex gap-2 align-items-center">'
    if marker in body:
        body = body.replace(marker, MANAGE_BUTTON + marker, 1)
    output = body.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _install_ui():
    from .team import TeamAccessMiddleware

    original = TeamAccessMiddleware.__call__
    if getattr(original, "_fup_manual_installed", False):
        return

    @wraps(original)
    def with_fup_manual(self, request):
        return _inject_manage_button(request, original(self, request))

    with_fup_manual._fup_manual_installed = True
    TeamAccessMiddleware.__call__ = with_fup_manual


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_fair_usage_wrappers()
    _install_urls()
    _install_ui()
    _INSTALLED = True
