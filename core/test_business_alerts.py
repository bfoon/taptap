from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import business_alerts as ba
from .finance import record_sale
from .models import Business, Router, Voucher, VoucherPlan
from .models_events import EventAlert, EventRule


class BusinessAlertTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', currency='D',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.plan = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        self.c = Client(); self.c.force_login(self.owner)

    def stock(self, n):
        for i in range(n):
            Voucher.objects.create(business=self.b, code=f'S{Voucher.objects.count():07d}', plan_name='1 Day', price=25, duration_minutes=1440)

    def test_stock_low_fires_once_then_restocked(self):
        low = EventRule.objects.create(business=self.b, name='Low', kind='stock_low', params={'threshold': 5, 'plan': '1 Day'})
        back = EventRule.objects.create(business=self.b, name='Back', kind='stock_restocked', params={'threshold': 5})
        self.stock(3)
        self.assertEqual(ba.evaluate(self.b, force=True), 1)
        self.assertEqual(ba.evaluate(self.b, force=True), 0)          # still low: no repeat (repeat_hours=0)
        self.assertIn('Voucher stock low', EventAlert.objects.get(rule=low).title)
        self.stock(4)                                                # restock to 7
        self.assertEqual(ba.evaluate(self.b, force=True), 1)
        self.assertIn('Restocked', EventAlert.objects.get(rule=back).title)
        low.refresh_from_db(); self.assertFalse(low.firing)          # re-armed

    def test_repeat_and_quiet_hours(self):
        r = EventRule.objects.create(business=self.b, name='Low', kind='stock_low', params={'threshold': 5}, repeat_hours=2,
                                     quiet_from=0, quiet_to=23)
        now = timezone.now()
        ba.evaluate(self.b, now=now, force=True)
        ba.evaluate(self.b, now=now + timedelta(hours=1), force=True)
        ba.evaluate(self.b, now=now + timedelta(hours=3), force=True)
        alerts = EventAlert.objects.filter(rule=r)
        self.assertEqual(alerts.count(), 2)
        self.assertFalse(any(a.sound or a.desktop for a in alerts) and timezone.localtime(now).hour < 23)

    def test_sales_rules(self):
        EventRule.objects.create(business=self.b, name='Target', kind='daily_revenue', params={'amount': 40})
        EventRule.objects.create(business=self.b, name='Quiet', kind='no_sales', params={'hours': 2})
        self.assertEqual(ba.evaluate(self.b, force=True), 1)          # no sale ever → "no sales"
        self.stock(2)
        for v in Voucher.objects.all():
            record_sale(self.b, v, method='cash')
        ba.evaluate(self.b, force=True)
        self.assertTrue(EventAlert.objects.filter(title__startswith='Sales target reached').exists())
        self.assertEqual(ba.evaluate(self.b, force=True), 0)          # celebrated once today

    def test_router_offline_and_bell_feed(self):
        Router.objects.create(business=self.b, name='Kotu', ip_address='1.1.1.1', username='a', password='b', status='Offline')
        EventRule.objects.create(business=self.b, name='Down', kind='router_offline', params={'minutes': 0}, level='danger')
        ba.evaluate(self.b, force=True)
        feed = ba.unread(self.b)
        self.assertEqual(feed['unread'], 1); self.assertEqual(feed['fresh'][0]['level'], 'danger')
        d = self.c.get('/live/tick/').json()
        self.assertEqual(d['biz_alerts']['unread'], 1)
        self.c.post('/alerts/read/')
        self.assertEqual(ba.unread(self.b)['unread'], 0)

    def test_page_save_preset_and_test(self):
        self.assertEqual(self.c.get('/alerts/?f=business').status_code, 200)
        self.c.post('/alerts/business/add/', {'action': 'preset', 'kind': 'stock_low'})
        r = EventRule.objects.get(kind='stock_low')
        self.c.post('/alerts/business/', {'id': r.id, 'kind': 'stock_low', 'threshold': '8', 'plan': '1 Day', 'level': 'danger',
                                          'bell': 'on', 'sound': 'on', 'enabled': 'on', 'repeat_hours': '4'})
        r.refresh_from_db()
        self.assertEqual((r.params['threshold'], r.params['plan'], r.level, r.desktop, r.repeat_hours), ('8', '1 Day', 'danger', False, 4))
        self.c.post(f'/alerts/business/{r.id}/', {'action': 'test'})
        self.assertTrue(EventAlert.objects.filter(title__startswith='Test:').exists())
        self.assertContains(self.c.get('/alerts/?f=business'), 'true now')
