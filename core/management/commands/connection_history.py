"""Why does a router keep going "offline" and "back"? Recent changes per router, with the cause each time.

    python manage.py connection_history              # every router with changes
    python manage.py connection_history "Shop"       # routers whose name contains Shop
"""
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Recent offline / back-online changes per router, with the reported cause.'

    def add_arguments(self, parser):
        parser.add_argument('name', nargs='?', default='')

    def handle(self, name='', **opts):
        from core import uptime_guard
        from core.models import Router
        qs = Router.objects.select_related('business').order_by('business__business_name', 'name')
        if name:
            qs = qs.filter(name__icontains=name)
        shown = 0
        for r in qs:
            hist = uptime_guard.history(r.pk)
            fails = cache.get(f'tt:watch:fails:{r.pk}')
            down = uptime_guard.down_since(r.pk)
            if not hist and not fails and not down and not name:
                continue
            shown += 1
            via = 'TapTap Link' if r.connection_mode == 'agent' else 'API'
            self.stdout.write(self.style.MIGRATE_HEADING(f'{r.business.business_name} › {r.name} ({via}) — now {r.status}'))
            try:
                a = r.agent
                self.stdout.write(f'  Link: heartbeat v{a.script_version}, last check-in {timezone.localtime(a.last_seen_at):%d %b %H:%M:%S}'
                                  if a.last_seen_at else '  Link: never checked in')
            except Exception:
                pass
            if down:
                self.stdout.write(self.style.WARNING(f'  unreachable since {timezone.localtime(down):%d %b %H:%M:%S}'))
            if fails:
                self.stdout.write(f'  failed checks in a row: {fails.get("n")}')
            if uptime_guard.quiet(r.pk):
                self.stdout.write('  connection unstable: per-drop emails are paused for up to an hour')
            for e in hist[-20:]:
                at = uptime_guard._dt(e.get('at'))
                when = timezone.localtime(at).strftime('%d %b %H:%M:%S') if at else '?'
                kind = {'down': 'went offline', 'up': 'back online', 'blip': 'short blip (not emailed)', 'outage': 'Link outage'}.get(e.get('event'), e.get('event'))
                extra = ' · '.join(x for x in (uptime_guard.took(e['secs']) if e.get('secs') else '', e.get('via', ''), e.get('why', '')) if x)
                self.stdout.write(f'  {when}  {kind}' + (f' — {extra}' if extra else ''))
        if not shown:
            self.stdout.write('No offline or back-online changes recorded.')
