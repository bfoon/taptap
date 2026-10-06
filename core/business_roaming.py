"""Business collaboration roaming: shared vouchers, measured usage and clearing.

A voucher always belongs to its issuing business. Partner routers receive a
managed mirror of eligible vouchers; TapTap records the visited router's actual
HotSpot session uptime and bills the issuer at the host business's agreed hourly
rate. Two-way debts can be explicitly offset while real payments stay separate.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import AgentCommand, Business, PAYMENT_METHODS, Voucher
from .models_collaboration_buy import BusinessCollaboration
from .models_roaming import RoamingAgreement, RoamingMirror, RoamingOffset, RoamingPayment, RoamingSession

logger = logging.getLogger("taptap.roaming")
ROAM_PREFIX = "TapTap-roam:"
MAX_MIRRORS_PER_PARTNER = 500
INCREMENTS = {1, 5, 15, 30, 60}
_INSTALLED = False


def _business(request):
    return getattr(request, "tt_business", None) or request.user.business


def _actual_owner(request, business=None):
    business = business or _business(request)
    return bool(business and request.user.is_authenticated and business.user_id == request.user.pk and not getattr(request, "tt_view_as", None))


def _party(agreement, business):
    c = agreement.collaboration
    if business.pk == c.source_business_id:
        return c.source_business, c.target_business, True
    if business.pk == c.target_business_id:
        return c.target_business, c.source_business, False
    raise ValueError("Business is not part of this roaming agreement.")


def _money(value):
    try:
        x = Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Enter a valid amount.")
    if x < 0:
        raise ValueError("Amount cannot be negative.")
    return x


def _comment(agreement, voucher):
    return f"{ROAM_PREFIX}{agreement.pk}:{voucher.pk}"


def _profile(agreement, voucher):
    key = hashlib.sha1(f"{voucher.plan_name}|{voucher.max_devices}".encode()).hexdigest()[:8]
    return f"roam-{agreement.pk}-{key}"[:64]


def _remaining_seconds(voucher, now=None):
    now = now or timezone.now()
    expiry = voucher.expires_at
    if not expiry and voucher.used_at and voucher.duration_minutes:
        expiry = voucher.used_at + timedelta(minutes=voucher.duration_minutes)
    if expiry:
        return max(0, int((expiry - now).total_seconds()))
    if voucher.duration_minutes:
        return max(60, int(voucher.duration_minutes) * 60)
    return None  # unlimited


def _eligible(voucher, now=None):
    now = now or timezone.now()
    if voucher.status != "active" or voucher.deleted_at or voucher.frozen_at:
        return False
    if getattr(voucher, "is_member", False):
        return False
    if not (voucher.sold_at or voucher.used_at):
        return False
    remaining = _remaining_seconds(voucher, now)
    return remaining is None or remaining > 0


def _desired_hash(agreement, voucher):
    expiry = voucher.expires_at.isoformat() if voucher.expires_at else ""
    raw = f"{voucher.code}|{voucher.status}|{voucher.plan_name}|{voucher.max_devices}|{voucher.duration_minutes}|{expiry}|{agreement.status}|{agreement.enabled}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _issuer_for(agreement, visited_business):
    c = agreement.collaboration
    if visited_business.pk == c.source_business_id:
        return c.target_business
    if visited_business.pk == c.target_business_id:
        return c.source_business
    return None


def _host_rate(agreement, visited_business):
    c = agreement.collaboration
    return agreement.source_host_rate_per_hour if visited_business.pk == c.source_business_id else agreement.target_host_rate_per_hour


def _bill(seconds_used, rate, increment):
    increment = max(1, int(increment or 1))
    block = increment * 60
    units = (max(0, int(seconds_used)) + block - 1) // block
    minutes = units * increment
    amount = (Decimal(rate) * Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return minutes, amount


def balances(agreement):
    """Directional gross, payments, offsets and current due for one agreement."""
    c = agreement.collaboration
    active = agreement.sessions.exclude(status="void")
    s_to_t_gross = active.filter(issuer_business=c.source_business, visited_business=c.target_business).aggregate(x=Sum("amount"))["x"] or Decimal("0")
    t_to_s_gross = active.filter(issuer_business=c.target_business, visited_business=c.source_business).aggregate(x=Sum("amount"))["x"] or Decimal("0")
    s_to_t_paid = agreement.payments.filter(payer_business=c.source_business, payee_business=c.target_business).aggregate(x=Sum("amount"))["x"] or Decimal("0")
    t_to_s_paid = agreement.payments.filter(payer_business=c.target_business, payee_business=c.source_business).aggregate(x=Sum("amount"))["x"] or Decimal("0")
    offset = agreement.offsets.aggregate(x=Sum("amount"))["x"] or Decimal("0")
    s_due = max(Decimal("0"), s_to_t_gross - s_to_t_paid - offset)
    t_due = max(Decimal("0"), t_to_s_gross - t_to_s_paid - offset)
    available = min(s_due, t_due)
    net = s_due - t_due
    return {
        "source_to_target_gross": s_to_t_gross, "target_to_source_gross": t_to_s_gross,
        "source_to_target_paid": s_to_t_paid, "target_to_source_paid": t_to_s_paid,
        "offset_total": offset, "source_to_target_due": s_due, "target_to_source_due": t_due,
        "available_offset": available, "net": net,
    }


def _direct_profile(svc, profile, shared):
    res = svc.resource("/ip/hotspot/user/profile")
    rows = res.get(name=profile)
    values = {"shared_users": str(max(1, int(shared or 1)))}
    if rows:
        rid = rows[0].get("id") or rows[0].get(".id")
        if rid: res.set(id=rid, **values)
    else:
        res.add(name=profile, **values)


def _direct_upsert(svc, mirror, now):
    from .sync import _routeros_seconds
    v, a = mirror.voucher, mirror.agreement
    profile = _profile(a, v); _direct_profile(svc, profile, v.max_devices)
    users = svc.resource("/ip/hotspot/user")
    rows = users.get(name=v.code)
    remaining = _remaining_seconds(v, now)
    values = {"password": v.code, "profile": profile, "comment": _comment(a, v), "disabled": "no"}
    if rows:
        old_comment = str(rows[0].get("comment") or "")
        if not old_comment.startswith(ROAM_PREFIX):
            raise RuntimeError(f"{v.code} already exists on {mirror.router.name} and is not a TapTap roaming user.")
        used = _routeros_seconds(rows[0].get("uptime"))
        values["limit_uptime"] = "0s" if remaining is None else f"{used + remaining}s"
        rid = rows[0].get("id") or rows[0].get(".id")
        users.set(id=rid, **values)
    else:
        values["name"] = v.code
        values["limit_uptime"] = "0s" if remaining is None else f"{remaining}s"
        users.add(**values)
    mirror.status = "synced"; mirror.last_error = ""; mirror.last_synced_at = now
    mirror.save(update_fields=["status", "last_error", "last_synced_at", "updated_at"])


def _direct_remove(svc, mirror):
    users = svc.resource("/ip/hotspot/user"); rows = users.get(name=mirror.voucher.code)
    if rows:
        comment = str(rows[0].get("comment") or "")
        if comment == _comment(mirror.agreement, mirror.voucher):
            try: svc.reset_active_by_name(mirror.voucher.code)
            except Exception: pass
            rid = rows[0].get("id") or rows[0].get(".id")
            if rid: users.remove(id=rid)
    mirror.delete()


def _queue_upsert(mirror, user=None):
    from .linkops import send
    v, a = mirror.voucher, mirror.agreement
    if AgentCommand.objects.filter(router=mirror.router, kind="roaming_user_upsert", status__in=["queued","sent"], params__mirror_id=mirror.pk).exists():
        return
    send(mirror.router, "roaming_user_upsert", {
        "name": v.code, "profile": _profile(a, v), "shared": max(1, int(v.max_devices or 1)),
        "seconds": _remaining_seconds(v), "comment": _comment(a, v), "mirror_id": mirror.pk,
    }, label=f"Roaming voucher {v.code}", user=user, minutes=60*24)
    mirror.status = "queued"; mirror.last_error = ""
    mirror.save(update_fields=["status", "last_error", "updated_at"])


def _queue_remove(mirror, user=None):
    from .linkops import send
    if AgentCommand.objects.filter(router=mirror.router, kind="roaming_user_remove", status__in=["queued","sent"], params__mirror_id=mirror.pk).exists():
        return
    send(mirror.router, "roaming_user_remove", {"name": mirror.voucher.code, "comment": _comment(mirror.agreement, mirror.voucher), "mirror_id": mirror.pk},
         label=f"Remove roaming voucher {mirror.voucher.code}", user=user, minutes=60*24)
    mirror.status = "removing"; mirror.save(update_fields=["status", "updated_at"])


def sync_roaming_router(router, svc=None, user=None):
    """Reconcile every collaboration mirror that belongs on one visited router."""
    from .linkops import uses_link
    now = timezone.now(); business = router.business
    agreements = list(RoamingAgreement.objects.select_related("collaboration__source_business", "collaboration__target_business")
                      .filter(Q(collaboration__source_business=business) | Q(collaboration__target_business=business)))
    link = uses_link(router) if svc is None else False
    own = False
    if svc is None and not link:
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect(); own = True
    changed = 0
    try:
        for agreement in agreements:
            issuer = _issuer_for(agreement, business)
            desired_ids = set()
            if agreement.enabled and agreement.status == "active" and agreement.collaboration.status == "active" and issuer:
                candidates = (Voucher.objects.filter(business=issuer, status="active", deleted_at__isnull=True, frozen_at__isnull=True)
                              .filter(Q(sold_at__isnull=False) | Q(used_at__isnull=False)).order_by("expires_at", "pk")[:MAX_MIRRORS_PER_PARTNER])
                for v in candidates:
                    if not _eligible(v, now): continue
                    desired_ids.add(v.pk)
                    mirror, _ = RoamingMirror.objects.get_or_create(agreement=agreement, voucher=v, router=router,
                        defaults={"issuer_business": issuer, "visited_business": business})
                    wanted_hash = _desired_hash(agreement, v)
                    if mirror.desired_hash != wanted_hash:
                        mirror.desired_hash = wanted_hash; mirror.status = "pending"
                        mirror.save(update_fields=["desired_hash", "status", "updated_at"])
                    if link:
                        if mirror.status in {"pending", "error"}: _queue_upsert(mirror, user); changed += 1
                    else:
                        if mirror.status != "synced" or mirror.last_synced_at is None:
                            try: _direct_upsert(svc, mirror, now); changed += 1
                            except Exception as exc:
                                mirror.status="error"; mirror.last_error=str(exc)[:255]; mirror.save(update_fields=["status","last_error","updated_at"])
            stale = list(RoamingMirror.objects.filter(agreement=agreement, router=router).exclude(voucher_id__in=desired_ids))
            for mirror in stale:
                if link: _queue_remove(mirror, user)
                else: _direct_remove(svc, mirror)
                changed += 1
    finally:
        if own and svc: svc.close()
    return changed


def sync_agreement(agreement, user=None):
    total = 0
    businesses = [agreement.collaboration.source_business, agreement.collaboration.target_business]
    for b in businesses:
        for router in b.routers.all():
            try: total += sync_roaming_router(router, user=user)
            except Exception: logger.exception("Roaming sync failed on %s", router)
    return total


def _block_remote(router, mirror, svc=None):
    name = mirror.voucher.code
    if svc is not None:
        try: svc.reset_active_by_name(name)
        except Exception: pass
        try:
            users = svc.resource("/ip/hotspot/user"); rows = users.get(name=name)
            if rows and str(rows[0].get("comment") or "").startswith(ROAM_PREFIX):
                rid = rows[0].get("id") or rows[0].get(".id"); users.set(id=rid, disabled="yes")
        except Exception: pass
    else:
        from .agent import queue
        try: queue(router, "disconnect", {"user": name}, label=f"Stop roaming {name}", minutes=10)
        except Exception: pass
        try: queue(router, "hotspot_user_set", {"name": name, "disabled": True}, label=f"Disable roaming {name}", minutes=60)
        except Exception: pass


def ingest_roaming_sessions(router, rows, now=None, svc=None):
    """Measure foreign voucher session uptime and continuously accrue the host's receivable."""
    from .finance import mark_activated
    from .live import voucher_problem
    from .sync import _routeros_seconds
    now = now or timezone.now()
    codes = {str(r.get("user") or "").upper() for r in rows if r.get("user")}
    if not codes: 
        RoamingSession.objects.filter(router=router, status="open").update(status="ended", ended_at=now)
        return 0
    mirrors = list(RoamingMirror.objects.select_related("agreement__collaboration__source_business", "agreement__collaboration__target_business", "voucher")
                   .filter(router=router, voucher__code__in=list(codes)))
    by_code = {m.voucher.code.upper(): m for m in mirrors}; seen = set(); count = 0
    for s in rows:
        mirror = by_code.get(str(s.get("user") or "").upper())
        if not mirror: continue
        a, v = mirror.agreement, mirror.voucher
        if not a.enabled or a.status != "active" or a.collaboration.status != "active":
            _block_remote(router, mirror, svc); continue
        uptime = max(0, _routeros_seconds(s.get("uptime")))
        if not v.used_at:
            when = max(v.created_at, now - timedelta(seconds=uptime))
            mark_activated(v, when); v.refresh_from_db(fields=["used_at","expires_at","status","sold_at"])
        if v.used_at and not v.expires_at and v.duration_minutes:
            v.expires_at = v.used_at + timedelta(minutes=v.duration_minutes)
            Voucher.objects.filter(pk=v.pk, expires_at__isnull=True).update(expires_at=v.expires_at)
        if voucher_problem(v, now):
            _block_remote(router, mirror, svc); continue
        sid = str(s.get("id") or "").strip()
        mac = str(s.get("mac-address") or "").upper()
        if sid: key = f"r{router.pk}:v{v.pk}:s{sid}"
        else:
            start_bucket = int((now - timedelta(seconds=uptime)).timestamp() // 60)
            key = f"r{router.pk}:v{v.pk}:m{mac}:{start_bucket}"
        seen.add(key)
        started = now - timedelta(seconds=uptime)
        rate = _host_rate(a, router.business); minutes, amount = _bill(uptime, rate, a.billing_increment_minutes)
        session, created = RoamingSession.objects.get_or_create(session_key=key, defaults={
            "agreement": a, "voucher": v, "issuer_business": v.business, "visited_business": router.business, "router": router,
            "router_session_id": sid, "username": v.code, "mac_address": mac, "ip_address": str(s.get("address") or ""),
            "started_at": started, "last_seen_at": now, "seconds_used": uptime, "billable_minutes": minutes,
            "rate_per_hour": rate, "amount": amount, "status": "open"})
        if not created:
            new_seconds = max(session.seconds_used, uptime)
            minutes, amount = _bill(new_seconds, session.rate_per_hour, a.billing_increment_minutes)
            session.last_seen_at = now; session.seconds_used = new_seconds; session.billable_minutes = minutes; session.amount = amount; session.status = "open"; session.ended_at = None
            session.ip_address = str(s.get("address") or session.ip_address); session.mac_address = mac or session.mac_address
            session.save(update_fields=["last_seen_at","seconds_used","billable_minutes","amount","status","ended_at","ip_address","mac_address","updated_at"])
        count += 1
    open_qs = RoamingSession.objects.filter(router=router, status="open")
    if seen: open_qs.exclude(session_key__in=seen).update(status="ended", ended_at=now)
    else: open_qs.update(status="ended", ended_at=now)
    return count


def _link_upsert_body(params):
    from .agent import rs, NAME_RE
    name = str(params.get("name") or ""); profile = str(params.get("profile") or ""); comment = str(params.get("comment") or "")
    if not NAME_RE.match(name) or not re.match(r"^[\w .@:+/-]{1,64}$", profile): raise ValueError("Invalid roaming user/profile.")
    if not comment.startswith(ROAM_PREFIX) or any(ch in comment for ch in '\r\n"$;{}[]'): raise ValueError("Invalid roaming marker.")
    shared = max(1, min(100, int(params.get("shared") or 1))); seconds = params.get("seconds")
    if seconds is not None: seconds = max(0, min(366*86400, int(seconds)))
    lim_new = "0s" if seconds is None else f"{seconds}s"
    lim_old = "0s" if seconds is None else f"($used + {seconds}s)"
    return (f':local n {rs(name)}; :local p {rs(profile)}; :local c {rs(comment)}; '
            f':if ([:len [/ip hotspot user profile find name=$p]] = 0) do={{ /ip hotspot user profile add name=$p shared-users={shared} }} else={{ /ip hotspot user profile set [find name=$p] shared-users={shared} }}; '
            f':local id [/ip hotspot user find name=$n]; :if ([:len $id] = 0) do={{ /ip hotspot user add name=$n password=$n profile=$p limit-uptime={lim_new} disabled=no comment=$c }} else={{ '
            f':local old [/ip hotspot user get $id comment]; :if (!($old~"^TapTap-roam:")) do={{ :error "Existing username is not a TapTap roaming user" }}; '
            f':local used [/ip hotspot user get $id uptime]; /ip hotspot user set $id password=$n profile=$p limit-uptime={lim_old} disabled=no comment=$c }}')


def _link_remove_body(params):
    from .agent import rs, NAME_RE
    name = str(params.get("name") or ""); comment = str(params.get("comment") or "")
    if not NAME_RE.match(name) or not comment.startswith(ROAM_PREFIX): raise ValueError("Invalid roaming removal.")
    return (f':local id [/ip hotspot user find name={rs(name)}]; :if ([:len $id] > 0) do={{ :local c [/ip hotspot user get $id comment]; '
            f':if ($c={rs(comment)}) do={{ :do {{ /ip hotspot active remove [find user={rs(name)}] }} on-error={{}}; :do {{ /ip hotspot cookie remove [find user={rs(name)}] }} on-error={{}}; /ip hotspot user remove $id }} }}')


def _install_agent_hooks():
    from . import agent
    if getattr(agent, "_business_roaming_installed", False): return
    agent.SAFE_KINDS.update({"roaming_user_upsert", "roaming_user_remove"})
    agent.DEFAULT_EXPIRY.setdefault("roaming_user_upsert", 60*24); agent.DEFAULT_EXPIRY.setdefault("roaming_user_remove", 60*24)
    original_body = agent.command_body
    @functools.wraps(original_body)
    def body(cmd):
        if cmd.kind == "roaming_user_upsert": return _link_upsert_body(cmd.params or {})
        if cmd.kind == "roaming_user_remove": return _link_remove_body(cmd.params or {})
        return original_body(cmd)
    agent.command_body = body

    original_ack = agent.handle_ack
    @functools.wraps(original_ack)
    def ack(cmd_id, given_nonce, status, result=""):
        cmd = AgentCommand.objects.filter(pk=cmd_id).first()
        ok = original_ack(cmd_id, given_nonce, status, result)
        if ok and cmd and cmd.kind in {"roaming_user_upsert","roaming_user_remove"}:
            mid = (cmd.params or {}).get("mirror_id")
            mirror = RoamingMirror.objects.filter(pk=mid).first() if mid else None
            if mirror:
                if cmd.kind == "roaming_user_remove" and status == "ok": mirror.delete()
                else:
                    mirror.status = "synced" if status == "ok" else "error"; mirror.last_error = "" if status == "ok" else (result or "Router rejected roaming command")[:255]; mirror.last_synced_at = timezone.now() if status == "ok" else mirror.last_synced_at
                    mirror.save(update_fields=["status","last_error","last_synced_at","updated_at"])
        return ok
    agent.handle_ack = ack

    original_ingest = agent.ingest_sessions
    @functools.wraps(original_ingest)
    def ingest(router, rows, now):
        result = original_ingest(router, rows, now)
        try: ingest_roaming_sessions(router, rows, now)
        except Exception: logger.exception("Roaming Link session ingest failed on %s", router)
        return result
    agent.ingest_sessions = ingest

    original_push = agent.push_pending_vouchers
    @functools.wraps(original_push)
    def push(router, limit=25):
        count = original_push(router, limit)
        try:
            from .linkops import uses_link
            if uses_link(router): sync_roaming_router(router)
        except Exception: logger.exception("Roaming Link mirror sync failed on %s", router)
        return count
    agent.push_pending_vouchers = push
    agent._business_roaming_installed = True


def _install_direct_hooks():
    from . import traffic
    if getattr(traffic, "_business_roaming_installed", False): return
    original = traffic.collect
    @functools.wraps(original)
    def collect(router, svc, active, now=None):
        result = original(router, svc, active, now)
        stamp = now or timezone.now()
        try: ingest_roaming_sessions(router, active, stamp, svc=svc)
        except Exception: logger.exception("Roaming API session ingest failed on %s", router)
        try: sync_roaming_router(router, svc=svc)
        except Exception: logger.exception("Roaming API mirror sync failed on %s", router)
        return result
    traffic.collect = collect
    traffic._business_roaming_installed = True

    # Native live/full sync must not try to import TapTap's foreign mirror as a
    # local voucher. Active sessions stay visible, so the roaming hook above can bill them.
    from .mikrotik import MikroTikService
    original_users = MikroTikService.hotspot_users
    if not getattr(original_users, "_business_roaming_filter", False):
        @functools.wraps(original_users)
        def users(self, *args, **kwargs):
            rows = original_users(self, *args, **kwargs)
            return [r for r in rows if not str(r.get("comment") or "").startswith(ROAM_PREFIX)]
        users._business_roaming_filter = True
        MikroTikService.hotspot_users = users


def _active_collaborations(business):
    return BusinessCollaboration.objects.select_related("source_business","target_business").filter(status="active").filter(Q(source_business=business)|Q(target_business=business))

@login_required
def roaming_page(request):
    business = _business(request)
    if not _actual_owner(request, business):
        messages.error(request, "Only the actual business owner can manage roaming and settlements."); return redirect("dashboard")
    cards = []
    for c in _active_collaborations(business):
        a = getattr(c, "roaming_agreement", None)
        if not a:
            a = RoamingAgreement.objects.create(collaboration=c, currency=c.source_business.currency, proposed_by_business=business, proposed_by=request.user)
        cards.append({"collab": c, "agreement": a, "other": c.target_business if business.pk==c.source_business_id else c.source_business,
                      "balance": balances(a), "is_source": business.pk==c.source_business_id,
                      "can_accept": a.status=="pending" and a.proposed_by_business_id and a.proposed_by_business_id != business.pk,
                      "sessions": a.sessions.select_related("voucher","issuer_business","visited_business","router")[:20],
                      "payments": a.payments.select_related("payer_business","payee_business")[:10], "offsets": a.offsets.all()[:10]})
    return render(request, "core/business_roaming.html", {"business": business, "cards": cards, "payment_methods": PAYMENT_METHODS, "increments": sorted(INCREMENTS)})

@login_required
@require_POST
def roaming_action(request):
    business = _business(request)
    if not _actual_owner(request, business): messages.error(request, "Only the actual business owner can manage roaming."); return redirect("dashboard")
    action = str(request.POST.get("action") or "")
    try:
        agreement = RoamingAgreement.objects.select_related("collaboration__source_business","collaboration__target_business").get(pk=request.POST.get("agreement"))
        me, other, _ = _party(agreement, business)
        if action == "propose":
            if agreement.collaboration.status != "active": raise ValueError("The business collaboration must be active first.")
            if agreement.collaboration.source_business.currency != agreement.collaboration.target_business.currency: raise ValueError("Both businesses must use the same settlement currency before roaming can be enabled.")
            source_rate = _money(request.POST.get("source_host_rate")); target_rate = _money(request.POST.get("target_host_rate"))
            inc = int(request.POST.get("increment") or 1)
            if inc not in INCREMENTS: raise ValueError("Choose a valid billing increment.")
            agreement.source_host_rate_per_hour=source_rate; agreement.target_host_rate_per_hour=target_rate; agreement.billing_increment_minutes=inc
            agreement.currency=agreement.collaboration.source_business.currency; agreement.status="pending"; agreement.enabled=False
            agreement.proposed_by_business=business; agreement.proposed_by=request.user; agreement.proposed_at=timezone.now(); agreement.accepted_by=None; agreement.accepted_at=None
            agreement.save()
            sync_agreement(agreement, request.user)
            messages.success(request, f"Roaming terms sent to {other.business_name} for acceptance. Roaming stays off until they accept.")
        elif action == "accept":
            if agreement.status != "pending" or not agreement.proposed_by_business_id or agreement.proposed_by_business_id == business.pk: raise ValueError("There are no partner-proposed terms waiting for your acceptance.")
            agreement.status="active"; agreement.enabled=True; agreement.accepted_by=request.user; agreement.accepted_at=timezone.now(); agreement.save(update_fields=["status","enabled","accepted_by","accepted_at","updated_at"])
            sync_agreement(agreement, request.user); messages.success(request, f"Voucher roaming with {other.business_name} is active.")
        elif action == "pause":
            agreement.status="paused"; agreement.enabled=False; agreement.save(update_fields=["status","enabled","updated_at"])
            sync_agreement(agreement, request.user); messages.success(request, "Roaming paused and partner voucher mirrors are being removed.")
        elif action == "sync":
            n=sync_agreement(agreement, request.user); messages.success(request, f"Roaming reconciliation started ({n} router change(s)).")
        elif action == "offset":
            b=balances(agreement); maximum=b["available_offset"]
            amount=_money(request.POST.get("amount") or maximum)
            if amount <= 0 or amount > maximum: raise ValueError(f"Offset must be above zero and no more than {agreement.currency}{maximum:,.2f}.")
            RoamingOffset.objects.create(agreement=agreement, amount=amount, note=(request.POST.get("note") or "Mutual balance offset")[:255], created_by=request.user)
            messages.success(request, f"Cancelled {agreement.currency}{amount:,.2f} from both businesses' balances.")
        elif action == "payment":
            b=balances(agreement); c=agreement.collaboration
            payee = other; due = b["source_to_target_due"] if business.pk==c.source_business_id else b["target_to_source_due"]
            amount=_money(request.POST.get("amount"))
            if amount <= 0 or amount > due: raise ValueError(f"Payment must be above zero and no more than the current amount due ({agreement.currency}{due:,.2f}).")
            method=str(request.POST.get("method") or "cash")
            if method not in {x[0] for x in PAYMENT_METHODS}: method="other"
            RoamingPayment.objects.create(agreement=agreement, payer_business=business, payee_business=payee, amount=amount, payment_method=method,
                reference=(request.POST.get("reference") or "")[:120], note=(request.POST.get("note") or "")[:255], recorded_by=request.user)
            messages.success(request, f"Recorded {agreement.currency}{amount:,.2f} paid to {payee.business_name}.")
        else: raise ValueError("Unsupported roaming action.")
    except (RoamingAgreement.DoesNotExist, ValueError) as exc: messages.error(request, str(exc))
    return redirect("business_roaming")

NAV_DESKTOP='<a href="/collaboration/roaming/"><i class="bi bi-signpost-split"></i> Roaming & clearing</a>'
NAV_MOBILE='<a href="/collaboration/roaming/">Roaming & clearing</a>'

def _install_nav():
    from .team import TeamAccessMiddleware
    original=TeamAccessMiddleware.__call__
    if getattr(original,"_taptap_roaming_nav",False): return
    @functools.wraps(original)
    def wrapped(self,request):
        response=original(self,request); business=getattr(request,"tt_business",None)
        if not business or not _actual_owner(request,business) or getattr(response,"streaming",False) or response.status_code!=200 or "text/html" not in response.get("Content-Type","").lower(): return response
        body=response.content.decode(response.charset or "utf-8")
        if 'href="/collaboration/roaming/"' in body: return response
        anchor='<a href="/collaboration/"><i class="bi bi-buildings"></i> Collaboration</a>'
        if anchor in body: body=body.replace(anchor,anchor+NAV_DESKTOP,1)
        manchor='<a href="/collaboration/">Collaboration</a>'
        if manchor in body: body=body.replace(manchor,manchor+NAV_MOBILE,1)
        output=body.encode(response.charset or "utf-8"); response.content=output
        if response.has_header("Content-Length"): response["Content-Length"]=str(len(output))
        return response
    wrapped._taptap_roaming_nav=True; TeamAccessMiddleware.__call__=wrapped

def _install_urls():
    from . import urls, permissions
    names={getattr(p,"name",None) for p in urls.urlpatterns}
    if "business_roaming" not in names: urls.urlpatterns.append(path("collaboration/roaming/",roaming_page,name="business_roaming"))
    if "business_roaming_action" not in names: urls.urlpatterns.append(path("collaboration/roaming/action/",roaming_action,name="business_roaming_action"))
    permissions.URL_PERMS["business_roaming"]="team.manage"; permissions.URL_PERMS["business_roaming_action"]="team.manage"

def install():
    global _INSTALLED
    if _INSTALLED: return
    _install_agent_hooks(); _install_direct_hooks(); _install_urls(); _install_nav(); _INSTALLED=True
