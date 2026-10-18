"""Why does a voucher keep coming back with limit-uptime 0s? (core/router_time.py)

    docker compose exec web python manage.py router_time_trace ABC123

Shows, for one voucher code:
  * what the LAST inventory row from the router really contained (raw keys — a missing
    limit-uptime key means the router holds 0s; a missing uptime key means "not reported"),
  * TapTap's time for the voucher and the limit the rule wants now,
  * every recent "Match router time" command that included it: status, result, sent/done times.

Read it like this:
  commands "done" but the next inventory still 0s  → something ON the router clears it
                                                      (scheduler/script, on-login script, Mikhmon, restore)
  commands "failed" with "not applied for …"        → the router refused / user name not found
  commands "expired" / never "sent"                 → the router is not taking commands from TapTap Link
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Explain why a voucher keeps getting "Router time corrected (router had no time limit)".'

    def add_arguments(self, parser):
        parser.add_argument('code')

    def handle(self, code, **opts):
        from core import router_time as rt
        from core.models import AgentCommand, RouterHotspotUser, Voucher

        v = Voucher.all_objects.filter(code__iexact=code).select_related('router', 'business').first()
        if not v:
            self.stderr.write(f'No voucher {code}')
            return
        plan = v.business.plans.filter(name__iexact=v.plan_name).first()
        w = self.stdout.write
        w(f'Voucher {v.code}  status={v.status} source={v.source} plan={v.plan_name} devices={v.max_devices}')
        w(f'  used_at={v.used_at}  expires_at={v.expires_at}  router={v.router}')
        w(f'  TapTap time left={rt.remaining_s(v, plan)}s  slots={rt.slots(v, plan)}  live sessions={rt.session_s(v)}s')

        m = RouterHotspotUser.objects.filter(router=v.router, username__iexact=v.code).first() if v.router_id else None
        if not m:
            w('  Router mirror: NOT FOUND (the router does not have this user at all)')
        else:
            raw = m.raw_data or {}
            w(f'  Router mirror: username={m.username!r}  present={m.is_present}  last_seen={m.last_seen_at}')
            w(f'    limit_uptime={m.limit_uptime!r}  uptime={m.uptime!r}  profile={m.profile!r}  disabled={m.disabled}')
            w(f'    raw keys from the router: {sorted(raw)}')
            w(f'    raw limit-uptime={raw.get("limit-uptime", "<missing = 0s on the router>")!r}  '
              f'raw uptime={raw.get("uptime", "<missing>")!r}')
            if m.username != v.code:
                w(f'    !! letter case differs: router {m.username!r} vs TapTap {v.code!r}')
            w(f'    acceptable now? {rt.acceptable(v, m.limit_uptime, m.uptime, plan)}  '
              f'wanted limit={rt.limit_for(v, plan, rt.counted(v, m.uptime))}')

        w('  Recent time commands for this voucher:')
        n = 0
        for c in AgentCommand.objects.filter(router=v.router, kind='hotspot_users_limit').order_by('-created_at')[:300]:
            users = (c.params or {}).get('users') or []
            hit = next((u for u in users if str(u.get('n', '')).upper() == v.code.upper()), None)
            if not hit:
                continue
            w(f'    #{c.pk} {c.created_at:%d %b %H:%M} lim={hit.get("lim")} status={c.status} attempts={c.attempts} '
              f'sent={c.sent_at and c.sent_at.strftime("%H:%M:%S")} done={c.done_at and c.done_at.strftime("%H:%M:%S")} '
              f'result={c.result[:120]!r}')
            n += 1
            if n >= 12:
                break
        if not n:
            w('    none')
