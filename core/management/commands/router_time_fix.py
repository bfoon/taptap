"""Make every router's limit-uptime match TapTap now (core/router_time.py).

    docker compose exec web python manage.py router_time_fix --dry-run     # show the drift, change nothing
    docker compose exec web python manage.py router_time_fix               # fix it
    docker compose exec web python manage.py router_time_fix --router 3    # one router
    docker compose exec web python manage.py router_time_fix --business 2  # one business

Direct API / Tunnel routers are read live and corrected at once. TapTap Link routers are corrected from their
latest inventory: the commands are queued and applied at the router's next check-in.
Only active TapTap vouchers and members are touched; users made on the router are left alone.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Make every router's limit-uptime match TapTap's clock (fixes 'user has reached uptime limit')."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only show what disagrees')
        parser.add_argument('--router', type=int)
        parser.add_argument('--business', type=int)

    def handle(self, *args, **o):
        from core import router_time as rt
        from core.linkops import uses_link
        from core.models import Router, RouterHotspotUser, Voucher
        now = timezone.now()
        routers = Router.objects.select_related('business')
        if o.get('router'):
            routers = routers.filter(pk=o['router'])
        if o.get('business'):
            routers = routers.filter(business_id=o['business'])
        total = 0
        for r in routers:
            link = uses_link(r)
            self.stdout.write(self.style.MIGRATE_HEADING(f'{r.business.business_name} › {r.name} ({"TapTap Link" if link else "API"})'))
            plans = {p.name: p for p in r.business.plans.all()}
            try:
                if link:
                    rows = {m.username.upper(): {'limit-uptime': m.limit_uptime, 'uptime': m.uptime}
                            for m in RouterHotspotUser.objects.filter(router=r, is_present=True)}
                    svc = None
                else:
                    from core.mikrotik import MikroTikService
                    svc = MikroTikService(r).connect()
                    raw = svc.resource('/ip/hotspot/user').get()
                    rows = {str(x.get('name', '')).upper(): x for x in raw if x.get('name')}
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f'  ✗ cannot read the router: {exc}'))
                continue
            drift = []
            for v in Voucher.objects.filter(router=r, status='active', frozen_at__isnull=True, deleted_at__isnull=True):
                row = rows.get(v.code.upper())
                if row is None or not rt.platform_owned(v):
                    continue
                have, up = str(row.get('limit-uptime', row.get('limit_uptime', '')) or ''), str(row.get('uptime', '') or '')
                if not rt.acceptable(v, have, up, plans.get(v.plan_name), now):
                    drift.append((v, have, up))
                    self.stdout.write(f'  · {v.code:<14} {rt.why(v, have, up, plans.get(v.plan_name), now)} → '
                                      f'{rt.limit_for(v, plans.get(v.plan_name), rt.secs(up), now)}')
            if not drift:
                self.stdout.write(self.style.SUCCESS('  ✓ router matches TapTap'))
            elif not o['dry_run']:
                for v, _, _ in drift:          # a manual run ignores the 10-minute repeat guard
                    from django.core.cache import cache
                    cache.delete(f'tt:rt:fix:{r.pk}:{v.code.upper()}')
                n = rt.repair_link(r, now) if link else rt.repair_api(r, svc, list(rows.values()), now)
                total += n
                self.stdout.write(self.style.SUCCESS(f'  ✓ {n} fixed' + (' (queued for the next check-in)' if link else '')))
            if svc is not None:
                svc.close()
        if o['dry_run']:
            self.stdout.write('Dry run — nothing changed.')
        else:
            self.stdout.write(self.style.SUCCESS(f'Done: {total} voucher(s) corrected.'))
