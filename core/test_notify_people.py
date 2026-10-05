"""Notification emails reach every person who should get them — in every business they belong to.

Run:  DB_ENGINE=sqlite python manage.py test core.test_notify_people
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import notify
from .models import Business, NotificationRecipient
from .models_team import TeamMember


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example', EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class NotifyPeopleTests(TestCase):
    def setUp(self):
        week = timezone.now() + timedelta(days=7)
        self.me = User.objects.create_user('me@x.com', 'me@x.com', 'pw', first_name='Abdou')
        self.mine = Business.objects.create(user=self.me, business_name='My Wi-Fi', owner_name='Abdou', phone='1', trial_ends_at=week)
        self.other_owner = User.objects.create_user('lamin@x.com', 'lamin@x.com', 'pw', first_name='Lamin')
        self.other = Business.objects.create(user=self.other_owner, business_name='Lamin Net', owner_name='Lamin', phone='2', trial_ends_at=week)
        TeamMember.objects.create(business=self.other, user=self.me, role='admin')            # I am admin there
        self.cashier = User.objects.create_user('awa@x.com', 'awa@x.com', 'pw', first_name='Awa')
        TeamMember.objects.create(business=self.other, user=self.cashier, role='voucher_creator')
        for b in (self.mine, self.other):              # only the alerts these tests send (no morning summary)
            s = notify.prefs(b); s.daily_summary = False; s.save()

    def alert(self, business, subject='Router Serrekunda is offline'):
        mail.outbox.clear()
        notify.notify(business, 'router_offline', subject, 'TapTap cannot reach it.')
        notify.deliver()
        return {m.to[0]: m for m in mail.outbox}

    # ── the bug: an admin of another business got nothing ──
    def test_admin_of_another_business_receives_its_emails(self):
        got = self.alert(self.other)
        self.assertEqual(set(got), {'lamin@x.com', 'me@x.com'})                       # owner + me (admin)
        self.assertTrue(got['me@x.com'].subject.startswith('[Lamin Net]'))
        self.assertEqual(set(self.alert(self.mine)), {'me@x.com'})                    # and my own business still works

    def test_other_roles_are_off_by_default_and_can_be_switched_on(self):
        self.assertNotIn('awa@x.com', self.alert(self.other))
        notify.set_receives(self.other, f'u:{self.cashier.pk}', True, self.other_owner)
        self.assertIn('awa@x.com', self.alert(self.other))

    def test_one_email_each_with_a_personal_link(self):
        got = self.alert(self.other)
        for addr, m in got.items():
            self.assertEqual(m.to, [addr])                                           # nobody sees the others' addresses
            token = NotificationRecipient.objects.get(business=self.other, user__email=addr).token
            self.assertIn(f'https://taptap.example/n/me/{token}/', m.body)
            self.assertEqual(m.extra_headers['List-Unsubscribe'], f'<https://taptap.example/n/me/{token}/>')

    def test_unsubscribing_stops_only_that_person_in_that_business(self):
        self.alert(self.other)
        row = NotificationRecipient.objects.get(business=self.other, user=self.me)
        c = Client()
        self.assertContains(c.get(f'/n/me/{row.token}/'), 'Stop your TapTap emails from Lamin Net')      # asks first, no login
        c.post(f'/n/me/{row.token}/')
        self.assertEqual(set(self.alert(self.other)), {'lamin@x.com'})                # the owner still gets them
        self.assertEqual(set(self.alert(self.mine)), {'me@x.com'})                    # my own business unaffected

    def test_one_click_unsubscribe_from_the_mail_app(self):
        self.alert(self.other)
        row = NotificationRecipient.objects.get(business=self.other, user=self.me)
        r = Client(enforce_csrf_checks=True).post(f'/n/me/{row.token}/', {'List-Unsubscribe': 'One-Click'})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db(); self.assertFalse(row.receives)
        self.assertEqual(Client().get('/n/me/not-a-real-token-at-all-xxxx/').status_code, 404)

    def test_old_business_link_still_turns_everything_off(self):
        s = notify.prefs(self.other)
        Client().post(f'/n/off/{s.unsubscribe_token}/')
        self.assertEqual(self.alert(self.other), {})

    def test_extra_addresses_and_no_duplicates(self):
        s = notify.prefs(self.other)
        s.extra_recipients = 'tech@x.com, me@x.com'; s.save()
        self.assertEqual(sorted(self.alert(self.other)), ['lamin@x.com', 'me@x.com', 'tech@x.com'])
        self.assertTrue(NotificationRecipient.objects.filter(business=self.other, user__isnull=True, email='tech@x.com').exists())

    def test_the_page(self):
        c = Client(); c.force_login(self.other_owner)
        r = c.get(reverse('notifications'))
        self.assertContains(r, 'Who receives these')
        self.assertContains(r, 'awa@x.com'); self.assertContains(r, 'me@x.com')
        c.post(reverse('notifications'), {'enabled': 'on', 'people_form': '1', 'recv': [f'u:{self.other_owner.pk}', f'u:{self.cashier.pk}'],
                                          'summary_hour': 8, 'digest_minutes': notify.prefs(self.other).digest_minutes})
        self.assertEqual(set(self.alert(self.other)), {'lamin@x.com', 'awa@x.com'})    # admin switched off, cashier on
