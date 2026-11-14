"""Print only what is left of a batch (core/batch_print.py + the stock ribbon on the print page)."""
import json
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import batch_print
from core.models import Business, Voucher, VoucherBatch


@override_settings(AUTH_EMAIL_OTP=False)
class BatchPrintTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.batch = VoucherBatch.objects.create(business=self.biz, name='Market <b>day</b>', quantity=10)
        now = timezone.now()
        for i in range(10):
            v = Voucher.objects.create(business=self.biz, batch=self.batch, code=f'CODE{i:02d}', plan_name='1 hour', price=10,
                                       duration_minutes=60, serial=f'KN-{100 + i:06d}')
            extra = {}
            if i in (0, 1, 2):
                extra = {'sold_at': now}                          # sold, not used
            elif i in (3, 4):
                extra = {'sold_at': now, 'used_at': now}          # used
            elif i == 5:
                extra = {'status': 'disabled'}                    # switched off
            elif i == 6:
                extra = {'deleted_at': now}                       # in the bin: never printed
            if extra:
                Voucher.all_objects.filter(pk=v.pk).update(**extra)
        self.c = Client()
        self.c.force_login(self.owner)

    def page(self, **params):
        r = self.c.get(reverse('voucher_print'), {'batch': self.batch.pk, **params})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        stock = json.loads(re.search(r'STOCK=(\{.*?\}|null), ALL=rows', html).group(1))
        rows = json.loads(re.search(r'rows=(\[.*?\]), STOCK=', html).group(1))
        return html, stock, rows

    def test_states(self):
        now = timezone.now()
        self.assertEqual([batch_print.state_of('active', None, None), batch_print.state_of('active', now, None),
                          batch_print.state_of('active', now, now), batch_print.state_of('expired', None, None),
                          batch_print.state_of('disabled', now, now)], ['left', 'sold', 'used', 'off', 'used'])
        self.assertEqual(batch_print.parse_show('left'), ['left'])
        self.assertEqual(batch_print.parse_show('sold, LEFT,nonsense,left'), ['sold', 'left'])
        self.assertEqual(batch_print.parse_show(''), list(batch_print.STATES))
        self.assertEqual(batch_print.parse_show('<script>'), list(batch_print.STATES))

    def test_every_voucher_of_the_batch_comes_with_where_it_stands(self):
        html, stock, rows = self.page(show='left')
        self.assertEqual(stock['counts'], {'left': 3, 'sold': 3, 'used': 2, 'off': 1})
        self.assertEqual((stock['total'], stock['show']), (9, ['left']))                # the binned one is not in the batch any more
        self.assertEqual(sorted(r['code'] for r in rows if r['state'] == 'left'), ['CODE07', 'CODE08', 'CODE09'])
        self.assertEqual({r['code']: r['state'] for r in rows}['CODE05'], 'off')
        self.assertNotIn('CODE06', html)
        self.assertIn('id="stock"', html)
        self.assertIn('value="pull"', html)
        self.assertIn('Tally slip', html)
        self.assertNotIn('<b>day</b>', html.split('<script>')[1])                       # a batch name never becomes markup in the script data

    def test_without_show_the_whole_batch_is_ticked_as_before(self):
        _html, stock, rows = self.page()
        self.assertEqual(stock['show'], list(batch_print.STATES))
        self.assertEqual(len(rows), 9)

    def test_other_print_modes_have_no_ribbon(self):
        r = self.c.get(reverse('voucher_print'))
        html = r.content.decode()
        self.assertIn('STOCK=null', html)
        self.assertNotIn('id="stock"', html)
        self.assertNotIn('value="pull"', html)
        self.assertEqual(html.count('"code":"CODE'), 3)                                 # unsold shop stock, as before

    def test_buttons_on_the_batch_pages(self):
        detail = self.c.get(reverse('batch_detail', args=[self.batch.pk])).content.decode()
        self.assertIn(f'?batch={self.batch.pk}&show=left', detail)
        self.assertIn("Print what's left", detail.replace('&#x27;', "'"))
        listing = self.c.get(reverse('batches')).content.decode()
        self.assertIn('Print 3 left', listing)
        self.assertIn('Whole batch · 9', listing)
        Voucher.objects.filter(batch=self.batch).update(sold_at=None, used_at=None, status='active')
        listing = self.c.get(reverse('batches')).content.decode()                       # nothing sold yet: the plain button
        self.assertFalse('show=left' in listing)
        self.assertNotIn('Whole batch ·', listing)

    def test_another_business_cannot_print_my_batch(self):
        other = User.objects.create_user('p@x.gm', 'p@x.gm', 'pw-123456')
        Business.objects.create(user=other, business_name='Other', owner_name='P', phone='2', trial_ends_at=timezone.now() + timedelta(days=30))
        c = Client()
        c.force_login(other)
        self.assertEqual(c.get(reverse('voucher_print'), {'batch': self.batch.pk, 'show': 'left'}).status_code, 404)
