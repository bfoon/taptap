"""Member renewal cash receipts, Finance posting, email and expiry reminders.

Run:
  DB_ENGINE=sqlite python manage.py test core.test_member_renewal_notifications
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import members as mem
from .member_notifications import (
    send_due_member_reminders,
    send_renewal_receipt,
)
from .models import Business, Voucher, VoucherSale
from .models_member_plans import (
    MemberPlan,
    MemberReminderLog,
    MemberRenewal,
)


HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(
    AUTH_EMAIL_OTP=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='TapTap <noreply@taptap.test>',
    SITE_URL='https://taptap.test',
)
class MemberRenewalNotificationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'owner@example.com',
            'owner@example.com',
            'pw12345678',
        )
        self.biz = Business.objects.create(
            user=self.owner,
            business_name='Kombo WiFi',
            owner_name='Owner',
            phone='2207000000',
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.plan = MemberPlan.objects.create(
            business=self.biz,
            name='Member Monthly',
            price=Decimal('500.00'),
            duration_minutes=43200,
            duration_unit='months',
            max_devices=2,
            speed_limit='10M/10M',
            created_by=self.owner,
        )
        self.member = mem.create_member(
            self.biz,
            username='fatou',
            same=True,
            plan_value=str(self.plan.pk),
            customer_name='Fatou Jallow',
            customer_phone='7000000',
            customer_email='fatou@example.com',
            reminders_enabled=True,
            remind_7_days=True,
            remind_2_days=True,
            remind_1_day=True,
            user=self.owner,
        )
        self.client.force_login(self.owner)

    def running_member(self, days_left=1):
        now = timezone.now()
        Voucher.objects.filter(pk=self.member.pk).update(
            used_at=now - timedelta(days=29),
            expires_at=now + timedelta(days=days_left),
            status='active',
        )
        self.member.refresh_from_db()
        return self.member

    def test_renewal_requires_cash_amount_from_member_screen(self):
        self.running_member()
        with self.assertRaises(mem.MemberError):
            mem.renew_with_receipt(
                self.member,
                amount='',
                method='cash',
                user=self.owner,
                require_amount=True,
            )
        self.assertEqual(MemberRenewal.objects.count(), 0)
        self.assertEqual(VoucherSale.objects.count(), 0)

    def test_renewal_actual_cash_is_finance_sale_and_receipt(self):
        self.running_member()
        ok, _, sale, minutes, renewal = mem.renew_with_receipt(
            self.member,
            amount='450.00',
            method='cash',
            reference='CASH-77',
            user=self.owner,
            require_amount=True,
        )
        self.assertTrue(ok)
        self.assertEqual(minutes, 43200)
        self.assertEqual(sale.amount, Decimal('450.00'))
        self.assertEqual(sale.payment_method, 'cash')
        self.assertEqual(sale.reference, 'CASH-77')
        self.assertEqual(sale.voucher_code, 'fatou')
        self.assertEqual(renewal.sale_id, sale.pk)
        self.assertEqual(renewal.amount_collected, Decimal('450.00'))
        self.assertEqual(renewal.plan_price, Decimal('500.00'))
        self.assertEqual(renewal.email_to, 'fatou@example.com')
        self.assertTrue(renewal.new_expires_at)
        self.assertTrue(renewal.receipt_number.startswith('MRN-'))

    def test_receipt_email_is_sent_to_saved_member_email(self):
        self.running_member()
        _, _, _, _, renewal = mem.renew_with_receipt(
            self.member,
            amount='500',
            method='wave',
            reference='WAVE123',
            user=self.owner,
            require_amount=True,
        )
        sent, _ = send_renewal_receipt(renewal)
        self.assertTrue(sent)
        renewal.refresh_from_db()
        self.assertIsNotNone(renewal.email_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['fatou@example.com'])
        self.assertIn(renewal.receipt_number, mail.outbox[0].subject)
        self.assertIn('500.00', mail.outbox[0].body)

    def test_seven_day_reminder_sends_once_for_same_expiry(self):
        now = timezone.now().replace(microsecond=0)
        expiry = now + timedelta(days=6, hours=12)
        Voucher.objects.filter(pk=self.member.pk).update(
            used_at=now - timedelta(days=23),
            expires_at=expiry,
            status='active',
        )
        self.member.refresh_from_db()

        self.assertEqual(send_due_member_reminders(now=now), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('7 days', mail.outbox[0].subject)
        log = MemberReminderLog.objects.get(
            member=self.member,
            expiry_at=expiry,
            days_before=7,
        )
        self.assertEqual(log.status, 'sent')

        self.assertEqual(
            send_due_member_reminders(now=now + timedelta(minutes=5)),
            0,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(MemberReminderLog.objects.count(), 1)

    def test_reminders_are_optional_per_member(self):
        pref = self.member.member_notification_settings
        pref.reminders_enabled = False
        pref.save(update_fields=['reminders_enabled'])
        now = timezone.now().replace(microsecond=0)
        Voucher.objects.filter(pk=self.member.pk).update(
            used_at=now - timedelta(days=29),
            expires_at=now + timedelta(hours=20),
            status='active',
        )
        self.assertEqual(send_due_member_reminders(now=now), 0)
        self.assertEqual(MemberReminderLog.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_member_renew_view_opens_receipt_and_emails_member(self):
        self.running_member()
        response = self.client.post(
            reverse('member_renew', args=[self.member.pk]),
            {
                'amount': '500.00',
                'method': 'cash',
                'reference': 'COUNTER-1',
                'next': reverse('members'),
            },
            **HTML,
        )
        self.assertEqual(response.status_code, 302)
        renewal = MemberRenewal.objects.get(member=self.member)
        self.assertIn(f'receipt={renewal.pk}', response['Location'])
        self.assertEqual(
            VoucherSale.objects.get(pk=renewal.sale_id).amount,
            Decimal('500.00'),
        )
        self.assertEqual(len(mail.outbox), 1)

        receipt = self.client.get(response['Location'], **HTML)
        self.assertEqual(receipt.status_code, 200)
        self.assertContains(receipt, renewal.receipt_number)
        self.assertContains(receipt, 'PAYMENT RECEIVED')
        self.assertContains(receipt, '500.00')
        self.assertContains(receipt, 'Fatou Jallow')

    def test_email_and_reminder_settings_can_be_changed(self):
        response = self.client.post(
            reverse('members'),
            {
                'action': 'save_notifications',
                'member_id': self.member.pk,
                'customer_email': 'new@example.com',
                'reminders_enabled': '1',
                'remind_2_days': '1',
                'next': reverse('members'),
            },
            **HTML,
        )
        self.assertEqual(response.status_code, 302)
        pref = self.member.member_notification_settings
        pref.refresh_from_db()
        self.assertEqual(pref.email, 'new@example.com')
        self.assertTrue(pref.reminders_enabled)
        self.assertFalse(pref.remind_7_days)
        self.assertTrue(pref.remind_2_days)
        self.assertFalse(pref.remind_1_day)
