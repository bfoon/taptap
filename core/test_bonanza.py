import json
from datetime import timedelta
from collections import Counter

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import bonanza as eng
from .models import Agent, Bonanza, BonanzaPrize, BonanzaSpin, Business, Voucher, VoucherPlan, VoucherSale


class BonanzaTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.u, business_name='Kairaba Net', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.day = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        self.hour = VoucherPlan.objects.create(business=self.b, name='1 Hour', price=5, duration_minutes=60)
        self.agent = Agent.objects.create(business=self.b, name='Awa Shop')
        self.bz = Bonanza.objects.create(business=self.b, name='Tabaski', slug='tabaski1', status='live')
        self.bz.plans.set([self.day])
        now = timezone.now()
        self.v = [Voucher.objects.create(business=self.b, code=f'CODE{i:04d}', plan_name='1 Day', price=25,
                                         duration_minutes=1440, sold_at=now, used_at=now - timedelta(hours=1)) for i in range(40)]

    def prize(self, **kw):
        return BonanzaPrize.objects.create(bonanza=self.bz, **{'label': 'P', 'kind': 'none', 'weight': 10, **kw})

    def test_who_can_spin(self):
        self.prize()
        other = Voucher.objects.create(business=self.b, code='HOUR0001', plan_name='1 Hour', sold_at=timezone.now())
        stock = Voucher.objects.create(business=self.b, code='SHELF001', plan_name='1 Day')
        self.assertTrue(eng.eligibility(self.bz, self.v[0].code).ok)
        self.assertFalse(eng.eligibility(self.bz, other.code).ok)
        self.assertFalse(eng.eligibility(self.bz, stock.code).ok)
        self.bz.agents.set([self.agent])
        self.assertFalse(eng.eligibility(self.bz, self.v[0].code).ok)
        self.bz.agents.clear(); self.bz.status = 'paused'; self.bz.save()
        self.assertFalse(eng.eligibility(self.bz, self.v[0].code).ok)

    def test_one_spin_per_voucher_and_stock(self):
        self.prize(label='Shirt', kind='gift', weight=1000, quantity=2, when_out='remove')
        self.prize(label='Again', kind='none', weight=1)
        eng.spin(self.bz, self.v[0].code)
        with self.assertRaises(eng.BonanzaError):
            eng.spin(self.bz, self.v[0].code)
        labels = Counter(eng.spin(self.bz, v.code).prize_label for v in self.v[1:20])
        self.assertLessEqual(labels['Shirt'] + (1 if BonanzaSpin.objects.first().prize_label == 'Shirt' else 0), 2)
        self.assertNotIn('Shirt', [x['label'] for x in eng.wheel_json(self.bz)])

    def test_payouts(self):
        pv = self.prize(label='Free hour', kind='voucher', plan=self.hour, weight=1)
        s = eng.spin(self.bz, self.v[0].code)
        self.assertEqual(s.payout, 'done')
        self.assertEqual(s.reward_voucher.plan_name, '1 Hour')
        self.assertEqual(s.reward_voucher.price, 0)
        self.assertFalse(VoucherSale.objects.filter(voucher=s.reward_voucher).exists())
        pv.active = False; pv.save()
        self.prize(label='+30', kind='time', minutes=30, weight=1)
        s = eng.spin(self.bz, self.v[1].code)
        self.assertEqual(s.payout, 'done')
        BonanzaPrize.objects.filter(kind='time').update(active=False)
        self.prize(label='D50', kind='cash', amount=50, weight=1)
        s = eng.spin(self.bz, self.v[2].code)
        self.assertEqual(s.payout, 'pending'); self.assertTrue(s.claim_code)
        eng.mark_paid(s, self.u, self.agent)
        s.refresh_from_db(); self.assertEqual(s.payout, 'paid'); self.assertEqual(s.paid_by_agent, self.agent)

    def test_public_pages(self):
        self.prize(label='Again', kind='none', weight=1)
        c = Client()
        self.assertEqual(c.get('/b/tabaski1/').status_code, 200)
        r = c.post('/b/tabaski1/spin/', json.dumps({'code': self.v[0].code}), content_type='application/json').json()
        self.assertTrue(r['ok']); self.assertEqual(r['result']['kind'], 'none')
        r = c.post('/b/tabaski1/spin/', json.dumps({'code': self.v[0].code}), content_type='application/json').json()
        self.assertFalse(r['ok'])
        self.bz.status = 'draft'; self.bz.save()
        self.assertEqual(c.get('/b/tabaski1/').status_code, 404)
