"""Team › Performance (core/team_performance.py): vouchers, batches, sales and cash per person, the owner included."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import fit_charts, fit_tools, team_performance as tp
from core.models import Agent, Business, CashCollection, Expense, Router, Voucher, VoucherBatch, VoucherSale
from core.models_team import TeamMember
from core.templatetags.team_perf import heat, initials

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'team-perf'}}


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner@x.gm', 'owner@x.gm', 'pw-123456', first_name='Musa', last_name='Jallow')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='Musa', phone='7000000',
                                           trial_ends_at=timezone.now() + timedelta(days=14), feature_tier='pro')
        self.router = Router.objects.create(business=self.biz, name='Hap', connection_mode='agent', status='Online')
        self.awa = User.objects.create_user('awa@x.gm', 'awa@x.gm', 'pw-123456', first_name='Awa', last_name='Ceesay')
        self.lamin = User.objects.create_user('lamin@x.gm', 'lamin@x.gm', 'pw-123456', first_name='Lamin', last_name='Sowe')
        TeamMember.objects.create(business=self.biz, user=self.awa, role='cashier')
        TeamMember.objects.create(business=self.biz, user=self.lamin, role='viewer')
        self.agent = Agent.objects.create(business=self.biz, name='Shop agent')
        self.now = timezone.now()
        self.n = 0

    def voucher(self, by, price=50, days_ago=0, member=False):
        self.n += 1
        v = Voucher.objects.create(business=self.biz, router=self.router, code=f'TP{self.n:04d}', plan_name='Day', price=price,
                                   created_by=by, login_type='member' if member else 'voucher')
        Voucher.all_objects.filter(pk=v.pk).update(created_at=self.now - timedelta(days=days_ago))
        return v

    def sale(self, voucher=None, by=None, amount=50, method='cash', days_ago=0, agent=None, commission=0):
        return VoucherSale.objects.create(business=self.biz, voucher=voucher, router=self.router, plan_name='Day', amount=amount,
                                          payment_method=method, recorded_by=by, agent=agent, commission=commission,
                                          sold_at=self.now - timedelta(days=days_ago))

    def period(self, preset='30d'):
        return tp.period_for({'range': preset})

    def row(self, data, user):
        return next(r for r in data['rows'] if r['user_id'] == user.pk)


class Figures(Base):
    def test_credit_cash_and_drawer_per_person(self):
        v1, v2, v3 = self.voucher(self.awa), self.voucher(self.awa), self.voucher(self.owner, price=100)
        self.voucher(self.awa, member=True)
        VoucherBatch.objects.create(business=self.biz, name='B1', quantity=20, created_by=self.awa)
        self.sale(v1, by=self.awa, amount=50, method='cash')
        self.sale(v2, by=None, amount=50, method='auto')            # recorded by itself → the voucher's creator (Awa)
        self.sale(v3, by=self.owner, amount=100, method='wave')
        self.sale(None, by=None, amount=30, method='auto')           # nobody to credit → Unassigned
        self.sale(None, by=self.lamin, amount=200, agent=self.agent, commission=20)  # an agent's sale → Agents row
        CashCollection.objects.create(business=self.biz, agent=self.agent, amount=80, payment_method='cash', recorded_by=self.awa)
        Expense.objects.create(business=self.biz, description='Fuel', amount=10, payment_method='cash', recorded_by=self.awa)
        Expense.objects.create(business=self.biz, description='Data', amount=99, payment_method='wave', recorded_by=self.awa)

        data = tp.report(self.biz, self.period())
        awa, owner, lamin = self.row(data, self.awa), self.row(data, self.owner), self.row(data, self.lamin)
        self.assertEqual((awa['vouchers'], awa['members'], awa['batches'], awa['batch_vouchers']), (2, 1, 1, 20))
        self.assertEqual((awa['sales'], awa['amount']), (2, Decimal('100')))
        self.assertEqual(awa['methods']['cash'], Decimal('50'))
        self.assertEqual(awa['methods']['auto'], Decimal('50'))
        self.assertEqual(awa['cash_drawer'], Decimal('120'))          # 50 cash + 80 collected − 10 cash expense
        self.assertEqual(awa['expenses'], Decimal('109'))
        self.assertEqual((owner['vouchers'], owner['amount'], owner['methods']['mobile'], owner['cash_drawer']),
                         (1, Decimal('100'), Decimal('100'), Decimal('0')))
        self.assertEqual((lamin['sales'], lamin['amount']), (0, Decimal('0')))   # the agent sold it, not Lamin
        pseudo = {r['pseudo']: r for r in data['rows'] if r['pseudo']}
        self.assertEqual(pseudo['unassigned']['amount'], Decimal('30'))
        self.assertEqual((pseudo['agents']['amount'], pseudo['agents']['commission']), (Decimal('200'), Decimal('20')))
        self.assertEqual(data['total'], Decimal('430'))
        self.assertEqual(data['total_cash'], Decimal('250'))      # Awa's 50 + the agent's 200 (cash by default)
        # leaderboard: owner and Awa tie on 100, Awa made more vouchers → first
        self.assertEqual([r['name'] for r in data['team']][:2], ['Awa Ceesay', 'Musa Jallow'])
        self.assertIn('Top seller', [b[1] for b in data['team'][0]['badges']])
        self.assertIn('Voucher maker', [b[1] for b in awa['badges']])
        self.assertEqual(data['team'][-1]['rank'], 3)
        alone = tp.report(self.biz, self.period(), only=self.owner.pk)['team'][0]
        self.assertNotIn('Top seller', [b[1] for b in alone['badges']])   # alone, nobody is 'top'

    def test_daily_and_monthly_buckets_and_change(self):
        self.sale(self.voucher(self.awa, days_ago=1), by=self.awa, amount=40, days_ago=1)
        self.sale(self.voucher(self.awa), by=self.awa, amount=60)
        self.sale(self.voucher(self.awa, days_ago=10), by=self.awa, amount=25, days_ago=10)
        p = self.period('7d')
        awa = self.row(tp.report(self.biz, p, 'day'), self.awa)
        self.assertEqual(len(awa['series']), 7)
        self.assertEqual([b['amount'] for b in awa['series'][-2:]], [Decimal('40'), Decimal('60')])
        self.assertEqual(awa['best']['amount'], Decimal('60'))
        self.assertEqual(awa['streak'], 2)
        self.assertEqual(awa['prev_amount'], Decimal('25'))
        self.assertEqual(awa['change'], 300.0)
        year = tp.report(self.biz, self.period('year'), 'month')
        self.assertEqual(year['bucket'], 'month')
        self.assertEqual(sum(b['amount'] for b in self.row(year, self.awa)['series']), self.row(year, self.awa)['amount'])
        self.assertEqual(tp.bucket_of(self.period('year')), 'month' if self.period('year').days > 62 else 'day')

    def test_deleted_vouchers_and_switched_off_people_still_count(self):
        v = self.voucher(self.lamin)
        Voucher.all_objects.filter(pk=v.pk).update(deleted_at=self.now)
        TeamMember.objects.filter(user=self.lamin).update(is_active=False)
        lamin = self.row(tp.report(self.biz, self.period()), self.lamin)
        self.assertEqual(lamin['vouchers'], 1)
        self.assertFalse(lamin['active'])

    def test_other_businesses_never_leak(self):
        other = User.objects.create_user('o2@x.gm', 'o2@x.gm', 'pw-123456')
        b2 = Business.objects.create(user=other, business_name='Other', owner_name='O', phone='1',
                                     trial_ends_at=self.now + timedelta(days=3))
        VoucherSale.objects.create(business=b2, plan_name='X', amount=999, recorded_by=self.awa)
        self.assertEqual(tp.report(self.biz, self.period())['total'], Decimal('0'))

    def test_template_filters(self):
        self.assertEqual(initials('Awa Ceesay'), 'AC')
        self.assertEqual(initials('awa@x.gm'), 'AX')
        self.assertEqual(initials(''), '?')
        self.assertEqual((heat(0, 10), heat(1, 10), heat(10, 10), heat(5, 0)), (0, 1, 4, 0))


class Pages(Base):
    def test_owner_sees_everybody_and_details(self):
        self.sale(self.voucher(self.awa), by=self.awa, amount=70)
        self.client.force_login(self.owner)
        r = self.client.get(reverse('team_performance'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Awa Ceesay')
        self.assertContains(r, 'Lamin Sowe')
        self.assertContains(r, 'Musa Jallow')
        for q in ('?range=7d', '?range=year&group=month', '?range=custom&start=2026-01-01&end=2026-01-31', '?range=today&group=day'):
            self.assertEqual(self.client.get(reverse('team_performance') + q).status_code, 200, q)
        d = self.client.get(reverse('team_performance_person', args=[self.awa.pk]))
        self.assertEqual(d.status_code, 200)
        self.assertContains(d, 'The cash drawer')
        self.assertContains(d, '#1')
        self.assertEqual(self.client.get(reverse('team_performance_person', args=[self.owner.pk])).status_code, 200)
        team = self.client.get(reverse('team'))
        self.assertContains(team, reverse('team_performance_person', args=[self.owner.pk]))
        self.assertContains(team, reverse('team_performance_person', args=[self.awa.pk]))

    def test_strangers_are_not_found(self):
        self.client.force_login(self.owner)
        stranger = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        self.assertEqual(self.client.get(reverse('team_performance_person', args=[stranger.pk])).status_code, 404)

    def test_a_member_without_rights_sees_only_themselves(self):
        self.sale(self.voucher(self.awa), by=self.awa, amount=70)
        self.sale(self.voucher(self.lamin), by=self.lamin, amount=15)
        self.client.force_login(self.lamin)
        own = self.client.get(reverse('team_performance_person', args=[self.lamin.pk]))
        self.assertEqual(own.status_code, 200)
        self.assertEqual(self.client.get(reverse('team_performance_person', args=[self.awa.pk])).status_code, 404)
        board = self.client.get(reverse('team_performance'))
        self.assertEqual(board.status_code, 200)
        self.assertContains(board, 'You see your own figures')
        self.assertNotContains(board, 'Awa Ceesay')
        self.assertNotContains(board, 'Unassigned')


class Fit(Base):
    def ctx(self, user, perms):
        return fit_tools.FitContext(business=self.biz, user=user, perms=frozenset(perms), settings=None)

    def test_tool_answers_per_person_and_per_day(self):
        self.sale(self.voucher(self.awa, days_ago=2), by=self.awa, amount=40, days_ago=2)
        self.sale(self.voucher(self.awa), by=self.awa, amount=60, method='qmoney')
        VoucherBatch.objects.create(business=self.biz, name='B', quantity=5, created_by=self.awa)
        res = json.loads(fit_tools.call(self.ctx(self.owner, {'finance.view'}), 'team_performance', {'period': '7d'}))
        names = [p['name'] for p in res['people']]
        self.assertIn('Musa Jallow', names)
        awa = next(p for p in res['people'] if p['name'] == 'Awa Ceesay')
        self.assertEqual((awa['vouchers_created'], awa['batches_created'], awa['sales']), (2, 1, 2))
        self.assertIn('D60', awa['by_payment']['Mobile money'])
        one = json.loads(fit_tools.call(self.ctx(self.owner, {'finance.view'}), 'team_performance', {'person': 'awa', 'group': 'day'}))
        self.assertEqual(len(one['people']), 1)
        self.assertEqual(len(one['people'][0]['per_day']), 2)
        month = json.loads(fit_tools.call(self.ctx(self.owner, {'finance.view'}), 'team_performance', {'period': 'year', 'group': 'month'}))
        self.assertEqual(month['grouped_by'], 'month')
        self.assertIn('error', json.loads(fit_tools.call(self.ctx(self.owner, {'finance.view'}), 'team_performance', {'person': 'nobody'})))
        custom = json.loads(fit_tools.call(self.ctx(self.owner, {'finance.view'}), 'team_performance',
                                           {'start': (self.now - timedelta(days=2)).date().isoformat()}))
        self.assertEqual(custom['grouped_by'], 'day')

    def test_tool_keeps_others_private_without_rights(self):
        self.sale(self.voucher(self.awa), by=self.awa, amount=60)
        res = json.loads(fit_tools.call(self.ctx(self.lamin, {'vouchers.view'}), 'team_performance', {}))
        self.assertTrue(res['only_yourself'])
        self.assertEqual([p['name'] for p in res['people']], ['Lamin Sowe'])

    def test_chart_source(self):
        self.sale(self.voucher(self.awa), by=self.awa, amount=60)
        self.assertIn('sales_by_teammate', fit_charts.SOURCES)
        fn = fit_charts.SOURCES['sales_by_teammate'][4]
        out = fn(self.ctx(self.owner, {'finance.view'}), period='30d')
        self.assertEqual(out['labels'], ['Awa Ceesay'])
        self.assertEqual(out['series'][0]['data'], [60.0])
