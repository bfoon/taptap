"""Router profiles, sorted into the groups people see: voucher plans, member plans, other profiles.

Member plans only work with members, so lists keep the two apart under bold headers, and a profile whose plan
was deleted (a member plan that no longer exists, or a voucher plan in the Bin) is left out of choice lists —
the profile may still sit on the router, but nobody should pick it.
"""
from .member_plan_prices import plan_id


def _binned_profile_names(business):
    """Lower-case profile names that belong only to voucher plans in the Bin (no live plan uses them)."""
    from .models import VoucherPlan
    live = set()
    for name, prof in business.plans.values_list('name', 'mikrotik_profile_name'):
        live.add((name or '').lower()); live.add((prof or '').lower())
    dead = set()
    for name, prof in VoucherPlan.all_objects.binned().filter(business=business).values_list('name', 'mikrotik_profile_name'):
        for n in (name, prof):
            if n and n.lower() not in live:
                dead.add(n.lower())
    return dead


def classify(business, names):
    """{profile name: (kind, member_plan_or_None)} with kind in 'member', 'member_off', 'deleted', 'other'."""
    member_plans = {p.pk: p for p in business.member_plans.all()}
    dead = _binned_profile_names(business)
    out = {}
    for n in names:
        pid = plan_id(n)
        if pid:
            mp = member_plans.get(pid)
            out[n] = ('deleted', None) if mp is None else (('member' if mp.active else 'member_off'), mp)
        else:
            out[n] = ('deleted', None) if n.lower() in dead else ('other', None)
    return out


def voucher_picker(business, voucher, current_profile=''):
    """What the "Router profile" picker on a voucher shows, already grouped:
    {'voucher_plans': [...], 'member_plans': [(row, member_plan)], 'others': [row], 'hidden': n}.
    Vouchers see voucher plans and other profiles; members see their member plans and other profiles."""
    is_member = voucher.login_type == 'member'
    rows = []
    if voucher.router_id:
        rows = list(voucher.router.hotspot_profiles.filter(is_present=True).order_by('name').values('name', 'shared_users', 'rate_limit'))
    kinds = classify(business, [r['name'] for r in rows])
    keep = {voucher.router_profile or '', current_profile or ''} - {''}
    member_rows, others, hidden = [], [], 0
    for r in rows:
        kind, mp = kinds[r['name']]
        if kind in ('member', 'member_off'):
            if not is_member:
                continue                                   # member plans only work with members
            if kind == 'member_off' and r['name'] not in keep:
                hidden += 1; continue                      # switched off: no new choices
            member_rows.append((r, mp))
        elif kind == 'deleted':
            if r['name'] in keep:
                others.append(r)                           # still on it now: keep it visible
            else:
                hidden += 1
        else:
            others.append(r)
    member_rows.sort(key=lambda x: (x[1].name.lower(), x[0]['name']))
    return {'voucher_plans': [] if is_member else list(business.plans.filter(active=True).order_by('name')),
            'member_plans': member_rows, 'others': others, 'hidden': hidden, 'is_member': is_member}
