"""Why do pushes not reach the phones? One command walks the whole road, server to phone.

    python manage.py push_doctor                     # the server key, Google's sign-in, every signed-in phone
    python manage.py push_doctor --check             # + ask Google about each phone's push address (nothing is shown
                                                     #   on the phones: validate_only), and forget the dead ones
    python manage.py push_doctor --send o@x.gm       # + a real test notification to that person's phones, now
    python manage.py push_doctor --user o@x.gm       # only that person's phones

Every answer is in plain words with what to do. Exit code 1 when something on the road is broken.
"""
import os
import time

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def _ago(iso):
    t = parse_datetime(iso or '')
    if not t:
        return '-'
    s = int((timezone.now() - t).total_seconds())
    if s < 90:
        return f'{s}s ago'
    if s < 5400:
        return f'{s // 60} min ago'
    if s < 172800:
        return f'{s // 3600} h ago'
    return f'{s // 86400} days ago'


class Command(BaseCommand):
    help = 'Check every step of push notifications to the TapTap mobile app, and say what to fix.'

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true', help="ask Google about each phone's push address (dry run)")
        parser.add_argument('--send', metavar='EMAIL', help="send a real test notification to this person's phones")
        parser.add_argument('--user', metavar='EMAIL', help="only this person's phones")

    def handle(self, check=False, send=None, user=None, **opts):
        from core import mobile_push as mp
        from core.models_mobile import MobileDevice
        w = self.stdout.write
        good = lambda m: w(self.style.SUCCESS('  ✓ ') + m)            # noqa: E731
        bad = lambda m: w(self.style.ERROR('  ✗ ') + m)               # noqa: E731
        note = lambda m: w('    ' + m)                                 # noqa: E731
        broken = False

        w(self.style.MIGRATE_HEADING('1. The server key (FCM_SERVICE_ACCOUNT)'))
        raw = os.getenv('FCM_SERVICE_ACCOUNT', '').strip()
        sa = mp._service_account()
        if not raw:
            bad('Not set. Nothing can be pushed; the app only gets calls and messages while it is open.')
            note('Firebase → Project settings → Service accounts → Generate new private key; put the file on the server')
            note('and add FCM_SERVICE_ACCOUNT=/app/secrets/<file>.json to .env, then restart web, worker, vouchers and live.')
            raise SystemExit(1)
        if sa is None:
            bad('Set, but it cannot be read as a Firebase service-account key.')
            if not raw.startswith('{'):
                note(f'It points to a file: {raw} — exists inside this container: {os.path.exists(raw)}')
                note('The file must be inside the image or mounted (docker-compose volumes), not only on the host.')
            raise SystemExit(1)
        good(f"Firebase project {sa['project_id']} · {sa['client_email']}")

        w(self.style.MIGRATE_HEADING("2. Google's sign-in for that key"))
        try:
            t0 = time.time()
            mp._access_token(sa, fresh=True)
            good(f'Google accepted the key ({(time.time() - t0) * 1000:.0f} ms).')
        except mp.PushError as e:
            bad(e.verdict['reason'])
            note('Fix: ' + e.verdict['fix'])
            if e.verdict.get('detail'):
                note('Google said: ' + e.verdict['detail'][:200])
            raise SystemExit(1)

        w(self.style.MIGRATE_HEADING('3. Signed-in phones'))
        rows = MobileDevice.objects.filter(revoked_at__isnull=True).select_related('user').order_by('user__email', '-last_seen_at')
        if user:
            rows = rows.filter(user__email__iexact=user)
        rows = list(rows)
        if not rows:
            bad('No phone is signed in to the app' + (f' for {user}.' if user else '.'))
        for d in rows:
            h = d.push_health or {}
            who = f'{d.user.email} · {d.name or d.get_platform_display()} (app {d.app_version or "?"}, seen {_ago(d.last_seen_at.isoformat() if d.last_seen_at else "")})'
            problems = []
            if not d.push_token:
                problems.append('no push address — the app has not registered (notifications blocked, or no Firebase in that build)')
            if h.get('perm') and h['perm'] != 'allowed':
                problems.append(f"the phone does not let TapTap show notifications ({h['perm']}) — the app now offers to turn them on")
            match = mp.project_check(d)
            if match is False:
                problems.append(f'its app is from Firebase project "{d.push_project}", the server key from "{mp.server_project()}" — '
                                'Google refuses every push (use files from the same project)')
            if h.get('at') and not h.get('ok'):
                problems.append(f"last push {_ago(h['at'])} refused: {h.get('code')} — {h.get('reason')}")
            if problems:
                broken = True
                bad(who)
                for p in problems:
                    note('· ' + p)
            else:
                good(who + (f" · last push ok {_ago(h.get('last_ok'))}" if h.get('last_ok') else ' · no push yet'))

        if check:
            w(self.style.MIGRATE_HEADING("4. Google's view of each push address (dry run, nothing pops up)"))
            for d in [r for r in rows if r.push_token]:
                v = mp.deliver(sa, d.push_token, mp.test_message(), validate_only=True)
                label = f'{d.user.email} · {d.name or d.get_platform_display()}'
                if v['ok']:
                    good(f'{label}: valid')
                else:
                    broken = True
                    bad(f"{label}: {v['code']} — {v['reason']}" + (' (forgotten)' if v.get('drop') else ''))
                    note('Fix: ' + v['fix'])

        if send:
            w(self.style.MIGRATE_HEADING(f'5. A real test notification to {send}'))
            targets = [r for r in MobileDevice.objects.filter(user__email__iexact=send, revoked_at__isnull=True).exclude(push_token='')]
            if not targets:
                broken = True
                bad('That person has no phone with a push address.')
            for d in targets:
                v = mp.test(d)
                if v['ok']:
                    good(f'{d.name or d.get_platform_display()}: Google accepted it — look at the phone now.')
                else:
                    broken = True
                    bad(f"{d.name or d.get_platform_display()}: {v['code']} — {v['reason']}")
                    note('Fix: ' + v['fix'])

        w('')
        if broken:
            w(self.style.WARNING('Something on the road needs fixing (above).'))
            w('If Google accepts the pushes but a phone still shows nothing: on that phone open TapTap → More → Notifications '
              '→ Check delivery.')
            raise SystemExit(1)
        w(self.style.SUCCESS('Every step works.' + ('' if check else ' Run with --check to ask Google about each phone too.')))
