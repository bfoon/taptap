"""Redo the WAN analysis of routers from their stored configuration and show how each default route resolved.

    python manage.py wan_explain              # every router: upgrade stale analyses, one line per Internet path
    python manage.py wan_explain --router 7   # one router, with every default and host route it saw
"""
from django.core.management.base import BaseCommand

from core.models import RouterConfigSnapshot
from core.wan_analysis import _rows, analyze_sections


class Command(BaseCommand):
    help = 'Redo the WAN (load balancing) analysis from stored configuration and explain it.'

    def add_arguments(self, parser):
        parser.add_argument('--router', type=int, help='Only this router id (shows the routes it used)')

    def handle(self, *args, **opts):
        snaps = RouterConfigSnapshot.objects.select_related('router')
        if opts.get('router'):
            snaps = snaps.filter(router_id=opts['router'])
        for snap in snaps:
            routes = _rows(snap.sections, 'Routes')
            if not routes:
                self.stdout.write(f'{snap.router} (#{snap.router_id}): no stored routes — run a full sync first')
                continue
            lb = analyze_sections(snap.router, snap.sections)
            RouterConfigSnapshot.objects.filter(pk=snap.pk).update(load_balancing=lb)
            self.stdout.write(self.style.SUCCESS(f'{snap.router} (#{snap.router_id}): {lb["method"]}'))
            for l in lb['wan_links']:
                via = f' via {l["next_hop"]}' if l.get('next_hop') else ''
                self.stdout.write(f'   {l["interface"] or "PORT NOT FOUND":<16} → {", ".join(l["gateways"]) or "-"}{via}'
                                  f'   state={l["state"]} share={l["expected_share"]}')
            for w in lb['warnings']:
                self.stdout.write(self.style.WARNING(f'   ! {w}'))
            if opts.get('router'):
                self.stdout.write('   routes seen:')
                for r in routes:
                    keys = ('dst-address', 'gateway', 'immediate-gw', 'gateway-status', 'distance', 'active', 'disabled',
                            'routing-table', 'routing-mark')
                    self.stdout.write('     ' + '  '.join(f'{k}={r[k]}' for k in keys if r.get(k) not in (None, '')))
        from django.core.cache import cache
        for snap in snaps:
            cache.delete(f'tt:lb:{snap.router_id}')
