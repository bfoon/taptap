from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business, DeviceSignature, Voucher, VoucherCodeAlias


class GoLinkTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.c = Client(); self.c.force_login(self.owner)

    def test_voucher_code_opens_its_page(self):
        v = Voucher.objects.create(business=self.b, code='ABCD2345', plan_name='x')
        self.assertRedirects(self.c.get('/go/voucher/abcd2345/'), f'/vouchers/{v.pk}/', fetch_redirect_response=False)
        VoucherCodeAlias.objects.create(business=self.b, voucher=v, code='OLDCODE1')
        self.assertRedirects(self.c.get('/go/voucher/OLDCODE1/'), f'/vouchers/{v.pk}/', fetch_redirect_response=False)
        r = self.c.get('/go/voucher/NOPE0000/')
        self.assertTrue(r['Location'].startswith('/vouchers/?q=NOPE0000'))

    def test_device_mac_opens_its_page(self):
        d = DeviceSignature.objects.create(business=self.b, fingerprint='fp1', last_mac='AA:BB:CC:00:00:01', macs=['02:11:22:33:44:55', 'AA:BB:CC:00:00:01'])
        self.assertRedirects(self.c.get('/go/device/aa-bb-cc-00-00-01/'), f'/devices/{d.pk}/', fetch_redirect_response=False)
        self.assertRedirects(self.c.get('/go/device/02:11:22:33:44:55/'), f'/devices/{d.pk}/', fetch_redirect_response=False)   # an older random MAC
        self.assertTrue(self.c.get('/go/device/DE:AD:BE:EF:00:00/')['Location'].startswith('/devices/?q=DE:AD:BE:EF:00:00'))

    def test_other_business_cannot_open(self):
        other = User.objects.create_user('x', 'x@x.com', 'pw12345678')
        b2 = Business.objects.create(user=other, business_name='Z', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        v = Voucher.objects.create(business=b2, code='THEIRS01', plan_name='x')
        self.assertNotEqual(self.c.get('/go/voucher/THEIRS01/')['Location'], f'/vouchers/{v.pk}/')

    def test_pages_render_with_links(self):
        for url in ('/active-users/', '/security/', '/alerts/', '/finance/', '/ip-bindings/', '/traffic/', '/security/fair-usage/slowed/'):
            r = self.c.get(url)
            self.assertIn(r.status_code, (200, 302), url)
