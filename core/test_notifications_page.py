from datetime import time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business, Notification
from .notify import EVENTS, prefs


class NotificationsPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', email='boss@k.gm',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.c = Client(); self.c.force_login(self.owner)

    def test_page_shows_stats_presets_and_recipients(self):
        Notification.objects.create(business=self.b, event='router_offline', severity='critical', subject='TapTap K offline', body='x', status='sent', sent_at=timezone.now())
        Notification.objects.create(business=self.b, event='backup_failed', severity='warning', subject='Backup failed', body='x', status='failed', error='SMTP down')
        r = self.c.get('/notifications/')
        self.assertContains(r, 'Calm'); self.assertContains(r, 'Everything now'); self.assertContains(r, 'boss@k.gm')
        self.assertContains(r, 'send again'); self.assertContains(r, 'id="ntPresets"')
        self.assertEqual(r.context['stats']['sent'], 1); self.assertEqual(r.context['stats']['failed'], 1)
        calm = r.context['presets']['calm']
        self.assertEqual(calm['router_offline'], 'instant'); self.assertEqual(calm['backup_done'], 'digest')

    def test_save_keeps_working(self):
        data = {'enabled': 'on', 'daily_summary': 'on', 'summary_hour': '7', 'extra_recipients': 'tech@k.gm, bad-address, mgr@k.gm',
                'quiet_start': '22:00', 'quiet_end': '06:30', 'business_email': 'boss@k.gm'}
        data.update({f'ev_{k}': 'digest' for k in EVENTS}); data['ev_router_offline'] = 'instant'; data['ev_backup_done'] = 'off'
        self.c.post('/notifications/', data)
        s = prefs(self.b); s.refresh_from_db()
        self.assertEqual(s.extra_recipients, 'tech@k.gm, mgr@k.gm')
        self.assertEqual((s.summary_hour, s.quiet_start, s.quiet_end), (7, time(22, 0), time(6, 30)))
        self.assertEqual((s.events['router_offline'], s.events['backup_done']), ('instant', 'off'))

    def test_failed_emails_are_sent_again(self):
        n = Notification.objects.create(business=self.b, event='backup_failed', severity='warning', subject='x', body='x', status='failed', error='e')
        self.c.post('/notifications/resend/')
        n.refresh_from_db(); self.assertEqual((n.status, n.error), ('queued', ''))
