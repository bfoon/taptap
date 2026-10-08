"""Member payment balances, receipt reprints and remaining-time UI.

This feature deliberately keeps two ideas separate:

* MemberRenewal = adds one membership period and records what was paid then.
* MemberBalancePayment = later money collected against that renewal's arrears.

Collecting a balance never calls the router time-extension code, so paying an
old balance cannot accidentally give another month/week/day.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import wraps
import json
import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Voucher, VoucherSale
from .models_member_arrears import MemberBalancePayment
from .models_member_plans import MemberRenewal

logger = logging.getLogger("taptap.member_arrears")

_ORIGINAL_MEMBERS = None
_ORIGINAL_MEMBER_RENEW = None
_INSTALLED = False


class BalanceError(ValueError):
    pass


def _money(value, label="Amount"):
    try:
        amount = Decimal(
            str(value or "").replace(",", "").strip()
        )
    except (InvalidOperation, ValueError):
        raise BalanceError(f"{label} must be a number.")
    if amount <= 0:
        raise BalanceError(f"{label} must be more than zero.")
    if amount > Decimal("99999999.99"):
        raise BalanceError(f"{label} is too large.")
    return amount.quantize(Decimal("0.01"))


def _allocations_paid(payments):
    totals = defaultdict(lambda: Decimal("0.00"))
    for payment in payments:
        for item in payment.allocations or []:
            try:
                rid = int(item.get("renewal_id") or 0)
                amount = Decimal(str(item.get("amount") or "0"))
            except (TypeError, ValueError, InvalidOperation):
                continue
            if rid:
                totals[rid] += amount
    return totals


def _charge_paid(payments):
    """Money already applied to each MemberCharge (allocations with charge_id)."""
    totals = defaultdict(lambda: Decimal("0.00"))
    for payment in payments:
        for item in payment.allocations or []:
            try:
                cid = int(item.get("charge_id") or 0)
                amount = Decimal(str(item.get("amount") or "0"))
            except (TypeError, ValueError, InvalidOperation):
                continue
            if cid:
                totals[cid] += amount
    return totals


def _charge_due(charge, paid):
    if charge.waived_at:
        return Decimal("0.00")
    return max(Decimal("0.00"), charge.amount - paid[charge.pk]).quantize(Decimal("0.01"))


def _charges_for(member_ids, lock=False):
    from .models_member_charges import MemberCharge
    qs = MemberCharge.objects.filter(member_id__in=list(member_ids)).order_by("charged_at", "pk")
    return list(qs.select_for_update() if lock else qs)


def balance_rows(member):
    """Outstanding balance per renewal and per charge (late fee…), oldest first.

    Each row: {"renewal": MemberRenewal or None, "charge": MemberCharge or None, "due": Decimal, "at": datetime}."""
    renewals = list(
        MemberRenewal.objects
        .filter(member=member)
        .order_by("renewed_at", "pk")
    )
    payments = list(
        MemberBalancePayment.objects
        .filter(member=member)
        .order_by("paid_at", "pk")
    )
    extra = _allocations_paid(payments)
    rows = []
    for renewal in renewals:
        due = max(
            Decimal("0.00"),
            renewal.plan_price
            - renewal.amount_collected
            - extra[renewal.pk],
        )
        rows.append({
            "renewal": renewal,
            "charge": None,
            "due": due.quantize(Decimal("0.01")),
            "at": renewal.renewed_at,
        })
    cpaid = _charge_paid(payments)
    for charge in _charges_for([member.pk]):
        rows.append({"renewal": None, "charge": charge, "due": _charge_due(charge, cpaid), "at": charge.charged_at})
    rows.sort(key=lambda r: (r["at"], 0 if r["renewal"] else 1))
    return rows


def total_arrears(member):
    return sum(
        (row["due"] for row in balance_rows(member)),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))


def balance_for_renewal(renewal):
    if not renewal.member_id:
        return max(
            Decimal("0.00"),
            renewal.plan_price - renewal.amount_collected,
        ).quantize(Decimal("0.01"))
    for row in balance_rows(renewal.member):
        if row["renewal"] is not None and row["renewal"].pk == renewal.pk:
            return row["due"]
    return Decimal("0.00")


def summaries_for_members(members, now=None):
    """One compact UI payload per member, with only two payment queries."""
    now = now or timezone.now()
    members = list(members)
    ids = [v.pk for v in members]
    renewals = list(
        MemberRenewal.objects
        .filter(member_id__in=ids)
        .order_by("renewed_at", "pk")
    )
    payments = list(
        MemberBalancePayment.objects
        .filter(member_id__in=ids)
        .order_by("paid_at", "pk")
    )

    charges_by_member = defaultdict(list)
    for charge in _charges_for(ids):
        charges_by_member[charge.member_id].append(charge)
    renew_by_member = defaultdict(list)
    pay_by_member = defaultdict(list)
    for renewal in renewals:
        renew_by_member[renewal.member_id].append(renewal)
    for payment in payments:
        pay_by_member[payment.member_id].append(payment)

    out = {}
    for member in members:
        rlist = renew_by_member[member.pk]
        plist = pay_by_member[member.pk]
        paid = _allocations_paid(plist)

        arrears = Decimal("0.00")
        for renewal in rlist:
            arrears += max(
                Decimal("0.00"),
                renewal.plan_price
                - renewal.amount_collected
                - paid[renewal.pk],
            )
        cpaid = _charge_paid(plist)
        for charge in charges_by_member[member.pk]:
            arrears += _charge_due(charge, cpaid)

        # The small bar represents one normal plan period. If an early renewal
        # leaves more than a full period remaining it stays full until the final
        # period begins, then drains smoothly to zero.
        progress = None
        end = member.expires_at
        plan_minutes = int(member.duration_minutes or 0)
        try:
            assignment = member.member_plan_assignment
            if assignment and assignment.plan:
                plan_minutes = int(assignment.plan.duration_minutes or 0)
        except Exception:
            pass
        if end and plan_minutes > 0:
            remaining = max(0.0, (end - now).total_seconds())
            full = max(60.0, plan_minutes * 60.0)
            progress = int(
                round(max(0.0, min(1.0, remaining / full)) * 100)
            )

        latest_kind = ""
        latest_id = None
        latest_at = None
        if rlist:
            latest_kind = "renewal"
            latest_id = rlist[-1].pk
            latest_at = rlist[-1].renewed_at
        if plist and (latest_at is None or plist[-1].paid_at >= latest_at):
            latest_kind = "balance"
            latest_id = plist[-1].pk
            latest_at = plist[-1].paid_at

        out[member.pk] = {
            "progress": progress,
            "arrears": arrears.quantize(Decimal("0.01")),
            "latest_kind": latest_kind,
            "latest_id": latest_id,
        }
    return out


@transaction.atomic
def collect_balance(
    member,
    *,
    amount,
    method="cash",
    reference="",
    agent=None,
    user=None,
    first_charge=None,
):
    """Collect money against arrears (old renewals and charges such as late fees) WITHOUT adding membership time.

    Oldest first — except ``first_charge`` (a MemberCharge id), which is paid before anything else: the fee the
    member is paying for at the counter."""
    from .finance import record_sale
    from .voucher_history import record

    member = (
        Voucher.objects
        .select_for_update()
        .select_related("business")
        .get(pk=member.pk)
    )
    renewals = list(
        MemberRenewal.objects
        .select_for_update()
        .filter(member=member)
        .order_by("renewed_at", "pk")
    )
    payments = list(
        MemberBalancePayment.objects
        .select_for_update()
        .filter(member=member)
        .order_by("paid_at", "pk")
    )
    extra = _allocations_paid(payments)

    open_rows = []
    balance_before = Decimal("0.00")
    for renewal in renewals:
        due = max(
            Decimal("0.00"),
            renewal.plan_price
            - renewal.amount_collected
            - extra[renewal.pk],
        ).quantize(Decimal("0.01"))
        if due:
            open_rows.append((renewal, due))
            balance_before += due
    cpaid = _charge_paid(payments)
    for charge in _charges_for([member.pk], lock=True):
        due = _charge_due(charge, cpaid)
        if due:
            open_rows.append((charge, due))
            balance_before += due
    open_rows.sort(key=lambda item: (getattr(item[0], "renewed_at", None) or item[0].charged_at,
                                     0 if isinstance(item[0], MemberRenewal) else 1))
    if first_charge:
        open_rows.sort(key=lambda item: 0 if (not isinstance(item[0], MemberRenewal) and item[0].pk == int(first_charge)) else 1)

    balance_before = balance_before.quantize(Decimal("0.01"))
    if balance_before <= 0:
        raise BalanceError(
            "This member owes nothing — add a charge (late fee…) first, or renew."
        )

    collected = _money(amount, "Amount collected")
    if collected > balance_before:
        raise BalanceError(
            f"Amount collected cannot be more than the outstanding balance "
            f"({member.business.currency}{balance_before:,.2f})."
        )

    left = collected
    allocations = []
    for item, due in open_rows:
        if left <= 0:
            break
        applied = min(left, due).quantize(Decimal("0.01"))
        if isinstance(item, MemberRenewal):
            allocations.append({
                "renewal_id": item.pk,
                "receipt": item.receipt_number,
                "plan": item.plan_name,
                "amount": str(applied),
            })
        else:
            allocations.append({
                "charge_id": item.pk,
                "receipt": item.receipt_number,
                "plan": item.label,
                "amount": str(applied),
            })
        left -= applied

    balance_after = (
        balance_before - collected
    ).quantize(Decimal("0.01"))

    sale = record_sale(
        member.business,
        None,
        plan_name=f"{member.plan_name} — balance payment",
        amount=collected,
        method=method,
        agent=agent,
        customer_name=member.customer_name,
        customer_phone=member.customer_phone,
        reference=(reference or "")[:120],
        user=user,
        notes=(
            f"Member {member.code}: arrears/balance collection · "
            f"amount collected {member.business.currency}{collected:,.2f} · "
            f"balance after {member.business.currency}{balance_after:,.2f}"
        ),
    )
    if sale:
        VoucherSale.objects.filter(pk=sale.pk).update(
            voucher_code=member.code
        )
        sale.voucher_code = member.code

    payment = MemberBalancePayment.objects.create(
        business=member.business,
        member=member,
        sale=sale,
        amount_collected=collected,
        balance_before=balance_before,
        balance_after=balance_after,
        currency=member.business.currency,
        payment_method=method,
        reference=(reference or "")[:120],
        allocations=allocations,
        recorded_by=(
            user
            if getattr(user, "is_authenticated", False)
            else None
        ),
    )

    record(
        member,
        "note",
        user=user,
        source="user",
        reason="Member balance payment",
        text=(
            f"{payment.receipt_number}: collected "
            f"{member.business.currency}{collected:,.2f}; "
            f"arrears now {member.business.currency}{balance_after:,.2f}. "
            "No membership time was added."
        ),
    )
    return payment


@transaction.atomic
def add_charge(member, *, kind="late_fee", amount, description="", user=None, collect=None):
    """Put a charge (late fee, reconnection fee…) on a member's account. Adds no time.

    ``collect``: {"amount", "method", "reference", "agent"} when the member pays now — the payment goes to this
    charge first. Returns (charge, payment_or_None)."""
    from .models_member_charges import MemberCharge
    from .voucher_history import record
    kinds = dict(MemberCharge.KINDS)
    if kind not in kinds:
        raise BalanceError("Choose what the charge is for.")
    value = _money(amount, "Charge amount")
    description = (description or "").strip()[:200]
    if kind == "other" and not description:
        raise BalanceError("Say what the charge is for.")
    paying = None
    if collect and str(collect.get("amount") or "").strip():
        paying = _money(collect.get("amount"), "Amount paid now")
        if paying > value:
            raise BalanceError("The amount paid now cannot be more than the charge. Use Record payment for older arrears.")
    charge = MemberCharge.objects.create(
        business=member.business, member=member, member_username=member.code, kind=kind, description=description,
        amount=value, currency=member.business.currency,
        created_by=user if getattr(user, "is_authenticated", False) else None)
    record(member, "note", user=user, source="user", reason=f"Charge added: {charge.label}",
           text=f"{charge.receipt_number}: {member.business.currency}{value:,.2f}. No membership time was added.")
    payment = None
    if paying:
        payment = collect_balance(member, amount=paying, method=collect.get("method") or "cash",
                                  reference=collect.get("reference") or "", agent=collect.get("agent"), user=user,
                                  first_charge=charge.pk)
    return charge, payment


def waive_charge(member, charge_id, *, reason="", user=None):
    """Cancel what is still unpaid on a charge (money already paid on it stays paid)."""
    from .models_member_charges import MemberCharge
    from .voucher_history import record
    reason = (reason or "").strip()[:200]
    if not reason:
        raise BalanceError("Give a reason for waiving the charge.")
    charge = MemberCharge.objects.filter(member=member, pk=charge_id).first()
    if not charge or charge.waived_at:
        raise BalanceError("That charge is not open.")
    payments = MemberBalancePayment.objects.filter(member=member)
    due = _charge_due(charge, _charge_paid(payments))
    if due <= 0:
        raise BalanceError("That charge is already fully paid.")
    charge.waived_at, charge.waive_reason = timezone.now(), reason
    charge.waived_by = user if getattr(user, "is_authenticated", False) else None
    charge.save(update_fields=["waived_at", "waive_reason", "waived_by"])
    record(member, "note", user=user, source="user", reason=f"Charge waived: {charge.label}",
           text=f"{charge.receipt_number}: {member.business.currency}{due:,.2f} no longer owed — {reason}")
    return charge, due


def _safe_next(value, fallback="/members/"):
    value = str(value or "")
    return (
        value
        if value.startswith("/") and not value.startswith("//")
        else fallback
    )


def _receipt_context(request, renewal):
    at_issue = max(
        Decimal("0.00"),
        renewal.plan_price - renewal.amount_collected,
    ).quantize(Decimal("0.01"))
    current = balance_for_renewal(renewal)
    total = (
        total_arrears(renewal.member)
        if renewal.member_id
        else current
    )
    return {
        "renewal": renewal,
        "arrears_at_issue": at_issue,
        "current_renewal_arrears": current,
        "total_arrears": total,
        "next_url": _safe_next(
            request.GET.get("next"),
            reverse("members"),
        ),
        "autoprint": request.GET.get("autoprint") == "1",
        "fmt": (
            "thermal"
            if request.GET.get("format") == "thermal"
            else "a4"
        ),
    }


def _balance_receipt_context(request, payment):
    return {
        "payment": payment,
        "next_url": _safe_next(
            request.GET.get("next"),
            reverse("members"),
        ),
        "autoprint": request.GET.get("autoprint") == "1",
        "fmt": (
            "thermal"
            if request.GET.get("format") == "thermal"
            else "a4"
        ),
    }


def _visible_members(request, business):
    q = (request.GET.get("q") or "").strip()
    kind = request.GET.get("kind", "")
    qs = (
        business.vouchers
        .filter(login_type="member")
        .select_related(
            "member_plan_assignment__plan",
        )
        .order_by("-created_at")
    )
    from django.db.models import Q
    if q:
        qs = qs.filter(
            Q(code__icontains=q)
            | Q(customer_name__icontains=q)
            | Q(customer_phone__icontains=q)
            | Q(plan_name__icontains=q)
            | Q(member_notification_settings__email__icontains=q)
        )
    if kind == "free":
        qs = qs.filter(price=0)
    elif kind == "paid":
        qs = qs.filter(price__gt=0)
    elif kind == "unassigned":
        qs = qs.filter(member_plan_assignment__isnull=True)

    return list(
        Paginator(qs, 50).get_page(
            request.GET.get("p")
        )
    )


def _inject_members_ui(response, request, business):
    if (
        response.status_code != 200
        or "text/html" not in response.get(
            "Content-Type",
            "",
        )
    ):
        return response

    try:
        html = response.content.decode(
            response.charset or "utf-8"
        )
    except Exception:
        return response

    if "</body>" not in html.lower():
        return response

    members = _visible_members(request, business)
    summary = summaries_for_members(members)
    next_url = request.get_full_path()

    data = {}
    for member in members:
        row = summary.get(member.pk) or {}
        arrears = row.get("arrears") or Decimal("0.00")
        receipt_url = ""
        if row.get("latest_kind") == "renewal":
            receipt_url = (
                reverse("members")
                + "?"
                + urlencode({
                    "receipt": row["latest_id"],
                    "autoprint": "1",
                    "next": next_url,
                })
            )
        elif row.get("latest_kind") == "balance":
            receipt_url = (
                reverse("members")
                + "?"
                + urlencode({
                    "balance_receipt": row["latest_id"],
                    "autoprint": "1",
                    "next": next_url,
                })
            )
        data[member.code.lower()] = {
            "progress": row.get("progress"),
            "arrears": f"{arrears:.2f}",
            "arrears_display": (
                f"{business.currency}{arrears:,.2f}"
            ),
            "receipt_url": receipt_url,
        }

    payload = (
        json.dumps(
            {
                "currency": business.currency,
                "members": data,
            },
            separators=(",", ":"),
        )
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
    )

    head = (
        f'<link rel="stylesheet" href="{static("css/member-arrears.css")}?v=1">'
    )
    tail = (
        '<script id="memberArrearsData" type="application/json">'
        + payload
        + "</script>"
        + f'<script src="{static("js/member-arrears.js")}?v=1"></script>'
    )

    lower = html.lower()
    hi = lower.rfind("</head>")
    if hi >= 0:
        html = html[:hi] + head + html[hi:]
    bi = html.lower().rfind("</body>")
    html = html[:bi] + tail + html[bi:]

    response.content = html.encode(
        response.charset or "utf-8"
    )
    response["Content-Length"] = str(
        len(response.content)
    )
    return response


@login_required
def members_view(request, *args, **kwargs):
    business = request.user.business

    receipt_id = str(
        request.GET.get("receipt") or ""
    )
    if (
        request.method == "GET"
        and receipt_id.isdigit()
    ):
        renewal = get_object_or_404(
            MemberRenewal.objects.select_related(
                "business",
                "member",
                "plan",
                "sale",
                "recorded_by",
            ),
            business=business,
            pk=int(receipt_id),
        )
        return render(
            request,
            "core/member_renewal_receipt_arrears.html",
            _receipt_context(
                request,
                renewal,
            ),
        )

    payment_id = str(
        request.GET.get("balance_receipt") or ""
    )
    if (
        request.method == "GET"
        and payment_id.isdigit()
    ):
        payment = get_object_or_404(
            MemberBalancePayment.objects.select_related(
                "business",
                "member",
                "sale",
                "recorded_by",
            ),
            business=business,
            pk=int(payment_id),
        )
        return render(
            request,
            "core/member_balance_receipt.html",
            _balance_receipt_context(
                request,
                payment,
            ),
        )

    response = _ORIGINAL_MEMBERS(
        request,
        *args,
        **kwargs,
    )
    if request.method == "GET":
        response = _inject_members_ui(
            response,
            request,
            business,
        )
    return response


@login_required
@require_POST
def member_renew_view(request, pk, *args, **kwargs):
    business = request.user.business
    member = get_object_or_404(
        business.vouchers.filter(
            login_type="member"
        ),
        pk=pk,
    )

    mode = str(
        request.POST.get("mode") or "renew"
    ).strip().lower()

    if mode == "balance":
        perms = getattr(
            request,
            "tt_perms",
            None,
        )
        if (
            perms is not None
            and "vouchers.create" not in perms
        ):
            messages.error(
                request,
                "Your role cannot collect member payments.",
            )
            return redirect(
                _safe_next(
                    request.POST.get("next")
                )
            )

        from .views_agents import MANUAL_METHODS
        method = (
            request.POST.get("method")
            if request.POST.get("method")
            in dict(MANUAL_METHODS)
            else "cash"
        )
        agent = business.agents.filter(
            pk=request.POST.get("agent") or 0
        ).first()

        try:
            payment = collect_balance(
                member,
                amount=request.POST.get(
                    "amount"
                ),
                method=method,
                reference=request.POST.get(
                    "reference",
                    "",
                ),
                agent=agent,
                user=request.user,
            )
        except BalanceError as exc:
            messages.error(
                request,
                str(exc),
            )
            return redirect(
                _safe_next(
                    request.POST.get("next")
                )
            )

        messages.success(
            request,
            f"{member.code}: collected "
            f"{payment.currency}"
            f"{payment.amount_collected:,.2f} "
            f"towards arrears. "
            f"Balance remaining: "
            f"{payment.currency}"
            f"{payment.balance_after:,.2f}. "
            "No additional membership time was added.",
        )

        from .utils import log
        log(
            business,
            "Member Balance Collected",
            f"{member.code}: "
            f"{payment.receipt_number} · "
            f"{payment.currency}"
            f"{payment.amount_collected:,.2f}",
        )

        nxt = _safe_next(
            request.POST.get("next"),
            reverse("members"),
        )
        query = urlencode({
            "balance_receipt": payment.pk,
            "next": nxt,
        })
        return redirect(
            f'{reverse("members")}?{query}'
        )

    due = total_arrears(member)
    if due > 0:
        messages.error(
            request,
            f"{member.code} has an outstanding "
            f"balance of {business.currency}"
            f"{due:,.2f}. Collect the balance "
            "before adding another renewal period.",
        )
        return redirect(
            _safe_next(
                request.POST.get("next")
            )
        )

    return _ORIGINAL_MEMBER_RENEW(
        request,
        pk,
        *args,
        **kwargs,
    )


def _patch_patterns(patterns):
    changed = 0
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            changed += _patch_patterns(
                pattern.url_patterns
            )
            continue
        if not isinstance(
            pattern,
            URLPattern,
        ):
            continue
        # Django 2+ resolves through URLPattern.callback (``_callback`` was the pre-2.0 name and setting it
        # changes nothing), so set both: otherwise the wrappers are skipped whenever the URLconf loaded before
        # install() ran, and a balance payment falls through to the plain renewal (adding a whole period).
        if pattern.name == "members":
            pattern.callback = pattern._callback = members_view
            changed += 1
        elif pattern.name == "member_renew":
            pattern.callback = pattern._callback = member_renew_view
            changed += 1
    return changed


def install():
    """Install without replacing the large existing Members view/template."""
    global _ORIGINAL_MEMBERS
    global _ORIGINAL_MEMBER_RENEW
    global _INSTALLED

    if _INSTALLED:
        return

    from . import views_members

    if getattr(
        views_members.members,
        "_taptap_arrears",
        False,
    ):
        _INSTALLED = True
        return

    _ORIGINAL_MEMBERS = views_members.members
    _ORIGINAL_MEMBER_RENEW = (
        views_members.member_renew
    )

    members_view._taptap_arrears = True
    member_renew_view._taptap_arrears = True

    # If core.urls is imported after ready(), it picks these wrappers up.
    views_members.members = members_view
    views_members.member_renew = (
        member_renew_view
    )

    # If the URLconf was already imported, update its stored callback too.
    try:
        changed = _patch_patterns(
            get_resolver().url_patterns
        )
        logger.info(
            "Member arrears installed; "
            "%s URL callback(s) aligned.",
            changed,
        )
    except Exception:
        # The module attributes above are still enough when URL loading happens
        # after AppConfig.ready().
        logger.exception(
            "Could not inspect URL callbacks while "
            "installing member arrears."
        )

    _INSTALLED = True
