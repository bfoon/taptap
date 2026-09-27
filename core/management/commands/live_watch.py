"""Run live sync without Celery:  python manage.py live_watch          (loops)
                                  python manage.py live_watch --once   (one pass, e.g. from cron)"""
import time

from django.core.management.base import BaseCommand

from core.live import interval, watch_all


class Command(BaseCommand):
    help = 'Check every router for voucher, session and IP-binding changes every few seconds.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Run a single pass and exit')

    def handle(self, *args, **opts):
        while True:
            started = time.monotonic()
            results = watch_all()
            if isinstance(results, list):
                changed = [r for r in results if any(r.get(k) for k in ('new_vouchers', 'activated', 'fixed', 'incidents', 'bindings_changed'))]
                for r in changed:
                    self.stdout.write(f"{r.get('router')}: {r}")
                errors = [r for r in results if r.get('error')]
                for r in errors:
                    self.stderr.write(f"{r.get('router')}: {r['error']}")
            if opts['once']:
                return
            time.sleep(max(1, interval() - (time.monotonic() - started)))
