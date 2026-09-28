"""Batch and plan detail pages: states, scroll paging, search and role access.

Run:  DB_ENGINE=sqlite python manage.py test core.test_batch_plan_detail
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import voucher_history as vh
from .models import Agent, Business, Voucher, VoucherBatch, VoucherPlan, VoucherSale
from .models_team import TeamMember
from .views_detail import state_filters

HTML = {'HTTP_ACCEPT': 'text/html'}
AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
TAB_OF_STATE = {'stock': 'remaining', 'sold': 'remaining', 'used': 'in_use', 'expired': 'expired',
                'disabled': 'other', 'frozen': 'other', 'warned': 'other'}


@override_settings(AUTH_EMAIL_OTP=False)
class BatchPlanDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.owner = User.objects.create_user('d@example.com', 'd@example.com', 'pw')
        cls.biz = Business.objects.create(user=cls.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=now + timedelta(days=7))
        cls.plan = VoucherPlan.objects.create(business=cls.biz, name='1 Hour', price=Decimal('10'), duration_minutes=60)
        cls.agent = Agent.objects.create(business=cls.biz, name='Awa Shop')
        cls.batch = VoucherBatch.objects.create(business=cls.biz, name='Morning batch', plan=cls.plan, quantity=10, agent=cls.agent)
        mk = lambda code, **kw: Voucher.objects.create(business=cls.biz, batch=cls.batch, code=code, plan_name='1 Hour', price=10,
                                                       duration_minutes=kw.pop('dur', 60), agent=cls.agent, **kw)
        mk('STOCK1')
        mk('SOLD1', sold_at=now - timedelta(minutes=5))
        mk('INUSE1', sold_at=now - timedelta(minutes=20), used_at=now - timedelta(minutes=10))
        mk('INUSE2', used_at=now - timedelta(hours=2), dur=1440)             # longer duration, still running
        mk('OUT1', used_at=now - timedelta(hours=2))                         # 60 min ran out
        mk('OUT2', used_at=now - timedelta(minutes=30), expires_at=now - timedelta(minutes=1))
        mk('OUT3', status='expired')
        mk('OFF1', status='disabled')
        mk('ICE1', used_at=now - timedelta(minutes=5), frozen_at=now, freeze_kind='freeze', frozen_left=1800)
        gone = mk('BIN1')
        Voucher.all_objects.filter(pk=gone.pk).update(deleted_at=now)
        # a single voucher of the same plan, not in the batch
        Voucher.objects.create(business=cls.biz, code='SOLO1', plan_name='1 Hour', price=10, duration_minutes=60)
        VoucherSale.objects.create(business=cls.biz, voucher=Voucher.objects.get(code='SOLD1'), plan_name='1 Hour', amount=Decimal('10'), agent=cls.agent)

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, name, pk, **q):
        return self.client.get(reverse(name, args=[pk]), q, **HTML)

    def codes(self, lists, key):
        return sorted(r['v'].code for l in lists if l['key'] == key for r in l['rows'])

    def test_batch_states(self):
        r = self.page('batch_detail', self.batch.pk)
        self.assertEqual(r.status_code, 200)
        c = r.context['counts']
        self.assertEqual((c['total'], c['remaining'], c['in_use'], c['expired'], c['other'], c['sold'], c['sold_unused']), (9, 2, 2, 3, 2, 2, 1))
        L = r.context['lists']
        self.assertEqual(self.codes(L, 'remaining'), ['SOLD1', 'STOCK1'])
        self.assertEqual(self.codes(L, 'in_use'), ['INUSE1', 'INUSE2'])
        self.assertEqual(self.codes(L, 'expired'), ['OUT1', 'OUT2', 'OUT3'])
        self.assertEqual(self.codes(L, 'other'), ['ICE1', 'OFF1'])
        self.assertEqual(r.context['sales']['v'], Decimal('10'))
        self.assertContains(r, 'vl-scroll')
        self.assertNotContains(r, 'BIN1')

    def test_tabs_match_voucher_page_state(self):
        now = timezone.now()
        qs = self.biz.vouchers.all()
        f = state_filters(qs, now)
        for v in qs:
            key, _ = vh.display_state(v, now)
            tabs = [t for t, q in f.items() if qs.filter(q, pk=v.pk).exists()]
            self.assertEqual(tabs, [TAB_OF_STATE[key]], v.code)

    def test_plan_detail(self):
        r = self.page('plan_detail', self.plan.pk)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['counts']['total'], 10)
        self.assertEqual(r.context['counts']['remaining'], 3)
        self.assertEqual(r.context['individual'], 1)
        self.assertEqual([b.name for b in r.context['batches']], ['Morning batch'])
        self.assertContains(r, reverse('batch_detail', args=[self.batch.pk]))

    def test_scroll_paging_and_search(self):
        Voucher.objects.bulk_create([Voucher(business=self.biz, batch=self.batch, code=f'BULK{i:04d}', plan_name='1 Hour',
                                             price=10, duration_minutes=60) for i in range(150)])
        r = self.page('batch_detail', self.batch.pk)
        rem = next(l for l in r.context['lists'] if l['key'] == 'remaining')
        self.assertEqual((len(rem['rows']), rem['next']), (100, 2))
        d = self.client.get(reverse('batch_detail', args=[self.batch.pk]), {'partial': 1, 'tab': 'remaining', 'page': 2}, **AJAX).json()
        self.assertEqual((d['next'], d['count'], d['html'].count('<tr>')), (None, 152, 52))
        d = self.client.get(reverse('batch_detail', args=[self.batch.pk]), {'partial': 1, 'tab': 'remaining', 'page': 1, 'q': 'bulk0149'}, **AJAX).json()
        self.assertEqual(d['count'], 1)
        self.assertIn('BULK0149', d['html'])

    def test_other_businesses_cannot_open(self):
        other = User.objects.create_user('x@example.com', 'x@example.com', 'pw')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='2', trial_ends_at=timezone.now() + timedelta(days=3))
        self.client.force_login(other)
        self.assertEqual(self.page('batch_detail', self.batch.pk).status_code, 404)
        self.assertEqual(self.page('plan_detail', self.plan.pk).status_code, 404)

    def test_roles(self):
        for role, code in [('voucher_support', 200), ('voucher_creator', 200), ('viewer', 403), ('finance', 403)]:
            u = User.objects.create_user(f'{role}@d.com', f'{role}@d.com', 'pw')
            TeamMember.objects.create(business=self.biz, user=u, role=role)
            self.client.force_login(u)
            self.assertEqual(self.page('batch_detail', self.batch.pk).status_code, code, role)
            self.assertEqual(self.page('plan_detail', self.plan.pk).status_code, code, role)

    def test_list_pages_link_to_details(self):
        self.assertContains(self.client.get(reverse('batches'), **HTML), reverse('batch_detail', args=[self.batch.pk]))
        self.assertContains(self.client.get(reverse('plans'), **HTML), reverse('plan_detail', args=[self.plan.pk]))
