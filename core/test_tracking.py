from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from . import business_alerts as ba
from .finance import record_sale
from .models import Business, Voucher, VoucherBatch, VoucherPlan
from .models_events import EventAlert
from .models_team import TeamMember
from .models_watch import Watch
from .voucher_history import record


class TrackingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'owner@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', currency='D',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.staff = User.objects.create_user('awa', 'awa@x.com', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=self.staff, role='admin')
        self.plan = VoucherPlan.objects.create(business=self.b, name='Monthly', price=400, duration_minutes=43200)
        self.batch = VoucherBatch.objects.create(business=self.b, name='B1', plan=self.plan, quantity=3)
        self.v = Voucher.objects.create(business=self.b, batch=self.batch, code='TRK00001', plan_name='Monthly', price=400, duration_minutes=43200)
        self.c = Client(); self.c.force_login(self.owner)

    def watch(self, kind, oid, **kw):
        self.c.post('/tracking/save/', {'kind': kind, 'object_id': oid, **kw})
        return Watch.objects.get(user=self.owner, kind=kind, object_id=oid)

    def test_track_voucher_everything_only_you_see_it(self):
        self.watch('voucher', self.v.pk)
        record(self.v, 'frozen', user=self.staff, reason='customer complaint')
        a = EventAlert.objects.get(user=self.owner)
        self.assertIn('TRK00001', a.body); self.assertIn('by awa', a.body); self.assertEqual(a.link, f'/vouchers/{self.v.pk}/')
        self.assertEqual(ba.unread(self.b, 0, self.owner)['unread'], 1)
        self.assertEqual(ba.unread(self.b, 0, self.staff)['unread'], 0)        # nobody else sees your tracking alerts
        record(self.v, 'unfrozen', user=self.owner)                           # you did it: not sent back to you
        self.assertEqual(EventAlert.objects.filter(user=self.owner).count(), 1)

    def test_plan_only_new_vouchers(self):
        self.watch('plan', self.plan.pk, mode='some', events=['vouchers_added'])
        record_sale(self.b, self.v, method='cash', user=self.staff)          # not chosen
        self.assertFalse(EventAlert.objects.exists())
        staff = Client(); staff.force_login(self.staff)
        staff.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 5})
        a = EventAlert.objects.get(user=self.owner)
        self.assertIn('new vouchers added', a.title); self.assertIn('5 new voucher(s) of Monthly', a.body)

    def test_busy_plan_is_grouped(self):
        self.watch('plan', self.plan.pk, mode='some', events=['sold'])
        for i in range(5):
            v = Voucher.objects.create(business=self.b, code=f'GRP0000{i}', plan_name='Monthly', price=400, duration_minutes=43200)
            record_sale(self.b, v, method='cash', user=self.staff)
        a = EventAlert.objects.get(user=self.owner)
        self.assertIn('×5', a.title)
        self.assertEqual(Watch.objects.get(kind='plan').hits, 5)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_app_and_email(self):
        self.watch('batch', self.batch.pk, channel='app_email', note='hotel order')
        record(self.v, 'extended', user=self.staff, text='added 2 h')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('B1', mail.outbox[0].subject); self.assertIn('hotel order', mail.outbox[0].body)

    def test_button_stop_and_tracking_page(self):
        page = self.c.get(f'/vouchers/{self.v.pk}/')
        self.assertContains(page, 'Start tracking')
        self.watch('voucher', self.v.pk, mode='some', events=['ended', 'devices'])
        self.assertContains(self.c.get(f'/vouchers/{self.v.pk}/'), 'Tracking')
        page = self.c.get('/tracking/')
        self.assertContains(page, 'TRK00001'); self.assertContains(page, 'Time ran out / expired')
        self.c.post('/tracking/save/', {'kind': 'voucher', 'object_id': self.v.pk, 'stop': '1'})
        self.assertFalse(Watch.objects.exists())

    def test_cannot_track_other_business_items(self):
        other = User.objects.create_user('x', 'x@x.com', 'pw12345678')
        b2 = Business.objects.create(user=other, business_name='Z', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        p2 = VoucherPlan.objects.create(business=b2, name='Theirs', price=1, duration_minutes=60)
        r = self.c.post('/tracking/save/', {'kind': 'plan', 'object_id': p2.pk})
        self.assertEqual(r.status_code, 404); self.assertFalse(Watch.objects.exists())
