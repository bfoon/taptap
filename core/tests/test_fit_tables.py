"""Fit tables (core/fit_tables.py): Fit builds a saved, filtered table — a card in the chat, its own page with live
filters, columns, sort, chips and CSV, the phone API, the workspace — and each viewer only sees what their role allows."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import fit_missions as fm, fit_tables as ft, fit_tools
from core.models import Business, Router, Voucher, VoucherSale
from core.models_fit import FitSettings, FitTable
from core.models_team import TeamMember


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'fittables'}})
class FitTableTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1', currency='D',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='Mast', status='Online')
        self.settings = FitSettings.objects.create(business=self.biz)
        self.perms = fm.perms_for(self.owner, self.biz)[0]
        now = timezone.now()
        for i, days in enumerate((90, 45, 40, 10, 2)):
            v = Voucher.objects.create(business=self.biz, code=f'OLD{i}', plan_name='Daily' if i % 2 == 0 else 'Weekly', price=10 + i,
                                       status='active', router=self.r)
            Voucher.objects.filter(pk=v.pk).update(created_at=now - timedelta(days=days))
        Voucher.objects.filter(code='OLD1').update(used_at=now - timedelta(days=30), expires_at=now + timedelta(days=2))
        self.ctx = fit_tools.FitContext(business=self.biz, user=self.owner, perms=self.perms, settings=self.settings)

    def test_fit_builds_old_vouchers_table_with_card(self):
        res = json.loads(fit_tools.call(self.ctx, 'create_table', {
            'source': 'vouchers', 'title': 'Old unused vouchers', 'filters': {'older_than_days': 30, 'state': 'unused'},
            'sort_by': 'created', 'sort_dir': 'asc'}))
        self.assertTrue(res.get('saved'), res)
        self.assertEqual(res['rows'], 2)                                    # OLD0 (90 d) and OLD2 (40 d) — OLD1 was used
        t = FitTable.objects.get(pk=res['table_id'])
        self.assertEqual(t.filters, {'state': 'unused', 'older_than_days': 30})   # stays relative
        card = self.ctx.cards['tables'][0]
        self.assertEqual(card['link'], f'/fit/tables/{t.pk}/')
        self.assertEqual(card['rows'][0]['cells'][0], 'OLD0')               # oldest first
        self.assertIn('Older than 30 days', card['filters'])
        self.assertIn('/fit/tables/', res['next'])
        # bad filters are explained to Fit
        bad = json.loads(fit_tools.call(self.ctx, 'create_table', {'source': 'vouchers', 'filters': {'plan': 'Gold'}}))
        self.assertIn('No plan Gold', bad['error'])

    def test_page_filters_sort_chips_csv_and_save(self):
        t = FitTable.objects.create(business=self.biz, created_by=self.owner, title='All', source='vouchers', filters={},
                                    columns=['code', 'plan', 'price', 'age_days'], sort={'by': 'age_days', 'dir': 'desc'})
        self.client.force_login(self.owner)
        r = self.client.get(f'/fit/tables/{t.pk}/')
        self.assertContains(r, 'OLD0')
        self.assertEqual(r.context['data']['total'], 5)
        self.assertEqual(r.context['data']['rows'][0]['cells'][0], 'OLD0')
        self.assertTrue(any(c['label'] == 'Daily' and c['href'] for c in r.context['chips']))
        # live filter from the address bar (a chip), then sort by another column
        r = self.client.get(f'/fit/tables/{t.pk}/', {'edited': '1', 'f_plan': 'Weekly', 'sort': 'price', 'dir': 'asc', 'cols': 'code,price'})
        self.assertEqual([x['cells'][0] for x in r.context['data']['rows']], ['OLD1', 'OLD3'])
        self.assertTrue(r.context['changed'])
        csv = self.client.get(f'/fit/tables/{t.pk}/', {'edited': '1', 'f_plan': 'Weekly', 'format': 'csv', 'cols': 'code,price'})
        self.assertEqual(csv['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('OLD3,D13', csv.content.decode('utf-8-sig'))
        # save these filters
        self.client.post(f'/fit/tables/{t.pk}/', {'op': 'save', 'filters': json.dumps({'plan': ['Weekly']}), 'cols': 'code,price',
                                                  'sort': 'price', 'dir': 'asc'})
        t.refresh_from_db()
        self.assertEqual(t.filters, {'plan': ['Weekly']})
        self.client.post(f'/fit/tables/{t.pk}/', {'op': 'pin'})
        t.refresh_from_db()
        self.assertTrue(t.pinned)
        # the phone's view
        d = self.client.get(f'/fit/tables/{t.pk}/data/').json()
        self.assertEqual(d['total'], 2)
        self.assertEqual(d['columns'][0]['key'], 'code')
        self.assertEqual(d['rows'][0]['link'][:10], '/vouchers/')
        # workspace lists it
        w = self.client.get('/fit/workspace/data/').json()
        self.assertEqual(w['tables'][0]['id'], t.pk)
        self.assertContains(self.client.get('/fit/workspace/'), 'All')

    def test_sales_table_and_rights_of_the_viewer(self):
        for amt in (50, 200):
            VoucherSale.objects.create(business=self.biz, plan_name='Daily', amount=Decimal(amt), router=self.r)
        res = json.loads(fit_tools.call(self.ctx, 'create_table', {'source': 'sales', 'title': 'Big sales', 'filters': {'min_amount': 100}}))
        self.assertEqual(res['rows'], 1)
        self.assertIn('D200', json.dumps(res['summary']))
        # a voucher seller without finance rights cannot open the sales table, nor ask Fit to make one
        lamin = User.objects.create_user('l@x.gm', 'l@x.gm', 'pw-123456', first_name='Lamin')
        TeamMember.objects.create(business=self.biz, user=lamin, role='voucher_creator')
        self.client.force_login(lamin)
        self.assertEqual(self.client.get(f'/fit/tables/{res["table_id"]}/').status_code, 404)
        lp = fm.perms_for(lamin, self.biz)[0]
        if not ft.can(lp, 'sales'):
            lctx = fit_tools.FitContext(business=self.biz, user=lamin, perms=lp, settings=self.settings)
            self.assertIn('error', json.loads(fit_tools.call(lctx, 'create_table', {'source': 'sales'})))
        # a private table is only for its maker
        priv = FitTable.objects.create(business=self.biz, created_by=self.owner, title='Mine', source='vouchers', shared=False)
        self.assertEqual(self.client.get(f'/fit/tables/{priv.pk}/').status_code, 404)

    def test_routers_table(self):
        Router.objects.create(business=self.biz, name='Tower', status='Offline')
        res = json.loads(fit_tools.call(self.ctx, 'create_table', {'source': 'routers', 'filters': {'status': 'offline'}}))
        self.assertEqual(res['rows'], 1)
        self.assertEqual(res['first_rows'][0]['Router'], 'Tower')

    def test_voucher_table_prints_as_tickets(self):
        Voucher.objects.create(business=self.biz, code='MEMB1', plan_name='Daily', status='active', login_type='member')
        res = json.loads(fit_tools.call(self.ctx, 'create_table', {'source': 'vouchers', 'title': 'Old unused',
                                                                    'filters': {'older_than_days': 30, 'state': 'unused'}}))
        self.assertEqual(res['print_as_tickets'], f'/studio/vouchers/print/?table={res["table_id"]}')
        self.assertIn('[Print as tickets]', res['next'])
        self.client.force_login(self.owner)
        r = self.client.get(res['print_as_tickets'])
        self.assertEqual(r.status_code, 200)
        rows = json.loads(r.context['rows_json'])
        self.assertEqual([x['code'] for x in rows], ['OLD0', 'OLD2'])          # the table's order (oldest first)
        stock = json.loads(r.context['stock_json'])
        self.assertEqual(stock['whole'], 'whole table')
        self.assertEqual(stock['counts']['left'], 2)
        # the page's live filters travel to the printer, members are left out
        r = self.client.get(f'/studio/vouchers/print/?table={res["table_id"]}&edited=1&f_plan=Daily')
        codes = [x['code'] for x in json.loads(r.context['rows_json'])]
        self.assertNotIn('MEMB1', codes)
        self.assertEqual(json.loads(r.context['stock_json'])['members_left_out'], 1)
        # the table page offers it, with "only what is left" and choosing by hand
        page = self.client.get(f'/fit/tables/{res["table_id"]}/')
        self.assertTrue(page.context['can_print'])
        self.assertIn('f_state=stock', page.context['print_stock_url'])
        self.assertContains(page, 'Choose the vouchers to print')
        # a sales table has no print button
        sales = FitTable.objects.create(business=self.biz, created_by=self.owner, title='S', source='sales')
        self.assertFalse(self.client.get(f'/fit/tables/{sales.pk}/').context['can_print'])
        self.assertEqual(self.client.get(f'/studio/vouchers/print/?table={sales.pk}').status_code, 404)
