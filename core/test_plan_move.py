from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business, Router, Voucher, VoucherBatch, VoucherPlan


class PlanMoveTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        self.old = VoucherPlan.objects.create(business=self.b, name='Old Month', price=Decimal('1000'), duration_minutes=43200, max_devices=5)
        self.new = VoucherPlan.objects.create(business=self.b, name='New Month', price=Decimal('1200'), duration_minutes=43200 * 2, max_devices=10, mikrotik_profile_name='month10')
        self.batch = VoucherBatch.objects.create(business=self.b, name='B1', plan=self.old, quantity=3)
        self.started = timezone.now() - timedelta(days=10)
        self.in_use = Voucher.objects.create(business=self.b, router=self.r, batch=self.batch, code='USE00001', plan_name='Old Month', price=1000,
                                             duration_minutes=43200, max_devices=5, used_at=self.started, sold_at=self.started)
        self.unused = Voucher.objects.create(business=self.b, router=self.r, batch=self.batch, code='NEW00001', plan_name='Old Month', price=1000, duration_minutes=43200, max_devices=5)
        self.mk = Voucher.objects.create(business=self.b, router=self.r, code='MK000001', plan_name='Old Month', price=1000, duration_minutes=43200, max_devices=5,
                                         source='mikrotik', used_at=self.started)
        self.c = Client(); self.c.force_login(self.owner)

    def move(self, **extra):
        with mock.patch('core.voucher_push.push_vouchers', return_value={}) as push, \
             mock.patch('core.voucher_push.set_router_profile', return_value=(True, 'ok')) as srp:
            self.c.post(f'/plans/{self.old.pk}/move/', {'to': self.new.pk, **extra})
        return push, srp

    def test_move_without_disruption(self):
        push, srp = self.move()
        u, n, m = (Voucher.objects.get(pk=x.pk) for x in (self.in_use, self.unused, self.mk))
        # in use: same start, same length (no restart, no extra time), more devices
        self.assertEqual((u.plan_name, u.used_at, u.duration_minutes, u.max_devices, u.price), ('New Month', self.started, 43200, 10, Decimal('1000')))
        # unused: becomes the new plan (length, devices, price — not sold)
        self.assertEqual((n.plan_name, n.duration_minutes, n.max_devices, n.price), ('New Month', 86400, 10, Decimal('1200')))
        self.assertEqual(VoucherBatch.objects.get(pk=self.batch.pk).plan, self.new)
        self.assertEqual(sorted(v.code for v in push.call_args.args[0]), ['NEW00001', 'USE00001'])   # TapTap's vouchers re-sent
        self.assertEqual((srp.call_args.args[0].code, srp.call_args.args[1]), ('MK000001', 'month10'))  # router-made: profile only
        self.assertEqual(m.used_at, self.started)

    def test_devices_never_drop_for_vouchers_in_use(self):
        VoucherPlan.objects.filter(pk=self.new.pk).update(max_devices=2)
        self.new.refresh_from_db()
        self.move()
        self.assertEqual(Voucher.objects.get(pk=self.in_use.pk).max_devices, 5)      # nobody cut off
        self.assertEqual(Voucher.objects.get(pk=self.unused.pk).max_devices, 2)

    def test_move_then_delete_the_empty_plan(self):
        self.move(delete_after='on')
        self.assertIsNotNone(VoucherPlan.all_objects.get(pk=self.old.pk).deleted_at)
        self.assertEqual(Voucher.objects.filter(plan_name='New Month').count(), 3)

    def test_delete_refused_while_plan_has_vouchers(self):
        self.c.post(f'/plans/{self.old.pk}/delete/', {'reason': 'old'})
        self.assertIsNone(VoucherPlan.all_objects.get(pk=self.old.pk).deleted_at)
