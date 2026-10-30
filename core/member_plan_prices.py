"""Member plans and their router profiles.

Every MemberPlan has a HotSpot user profile on the router (``taptap-member-plan-<id>``, or
``taptap-unlimited-member-plan-<id>``). TapTap writes the plan's price into that profile's comment
(``... · price=1000`` or ``... · free``) so the router itself carries it.

Member plans only work with members. A member-plan profile is never a voucher plan:
  * router sync records the profile on the router but does NOT import it as a voucher plan
    (core/sync.py); migration 0107 moved the rows imported before this change to the Bin;
  * a voucher plan cannot be given a member-plan profile name (VoucherPlan.save / PlanForm);
  * a voucher cannot be created on a member-plan profile, or switched onto one by hand
    (Voucher.save, voucher_set_profile) — only members use them.
"""
import re
from decimal import Decimal

PROFILE_RE = re.compile(r'^taptap-(?:unlimited-)?member-plan-(\d+)$', re.I)
SOURCE = 'member'          # VoucherPlan.price_source for prices copied from a member plan


MEMBERS_ONLY = 'Member plans only work with members. Use a voucher plan to make vouchers.'


def plan_id(profile_name):
    m = PROFILE_RE.match(str(profile_name or '').strip())
    return int(m.group(1)) if m else None


def is_member_profile(name):
    """True for a member plan's router profile (taptap-member-plan-<id> / taptap-unlimited-member-plan-<id>)."""
    return plan_id(name) is not None


def _amount(price):
    price = Decimal(str(price or 0))
    return str(int(price)) if price == price.to_integral_value() else f'{price.normalize():f}'


def router_comment(plan):
    """The profile comment TapTap writes for a member plan, e.g. 'TapTap member plan · ANNA · price=1000'."""
    if plan is None:
        return ''
    name = re.sub(r'\s+', ' ', str(plan.name or '')).strip()[:60]
    tail = f'price={_amount(plan.price)}' if Decimal(str(plan.price or 0)) > 0 else 'free'
    return f'TapTap member plan · {name} · {tail}'


def member_plan_for(business, profile_name):
    pid = plan_id(profile_name)
    if not pid:
        return None
    from .models_member_plans import MemberPlan
    return MemberPlan.objects.filter(business=business, pk=pid).first()


def apply_to_plan(vplan, mplan, save=True):
    """Give a voucher-plan row (the imported profile) the member plan's price and validity. True if it changed.

    The profile itself has no time limit (members carry their own), so without this the row showed
    "1 day" for every member plan. A price you typed on the Plans page yourself ('manual') is kept."""
    if mplan is None:
        return False
    fields = []
    minutes = int(mplan.duration_minutes or 0)
    unit = 'unlimited' if not minutes else mplan.effective_duration_unit
    if (vplan.duration_minutes, vplan.duration_unit) != (minutes, unit):
        vplan.duration_minutes, vplan.duration_unit = minutes, unit
        fields += ['duration_minutes', 'duration_unit']
    if vplan.price_source != 'manual':
        price = Decimal(str(mplan.price or 0))
        free = price <= 0
        if (vplan.price, vplan.is_free, vplan.price_source) != (price, free, SOURCE):
            vplan.price, vplan.is_free, vplan.price_source = price, free, SOURCE
            fields += ['price', 'is_free', 'price_source']
    if fields and save:
        vplan.save(update_fields=fields)
    return bool(fields)


def sync_business(business):
    """Bring every member-plan profile row on the Plans page in line with its member plan. Returns rows changed."""
    from .models import VoucherPlan
    from .models_member_plans import MemberPlan
    rows = list(VoucherPlan.objects.filter(business=business, name__iregex=r'^taptap-(unlimited-)?member-plan-[0-9]+$'))
    if not rows:
        return 0
    plans = MemberPlan.objects.in_bulk([plan_id(r.name) for r in rows])
    changed = 0
    for r in rows:
        mp = plans.get(plan_id(r.name))
        if mp and mp.business_id == business.pk and apply_to_plan(r, mp):
            changed += 1
    return changed


def annotate(plan_list, business):
    """For the Plans page: which member plan each profile row belongs to (shown under its name)."""
    from .models_member_plans import MemberPlan
    ids = {plan_id(p.name) for p in plan_list} - {None}
    found = MemberPlan.objects.filter(business=business, pk__in=ids).in_bulk() if ids else {}
    for p in plan_list:
        p.member_plan = found.get(plan_id(p.name))
    return plan_list
