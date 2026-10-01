from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Business
from .models_team import TeamMember


class MultiBusinessAccessTests(TestCase):
    def make_business(self, email, name):
        user = User.objects.create_user(
            username=email,
            email=email,
            password='Test-pass-123!',
        )
        business = Business.objects.create(
            user=user,
            business_name=name,
            owner_name=name + ' Owner',
            phone='000',
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        return user, business

    def test_same_login_can_belong_to_multiple_businesses(self):
        user_a, _ = self.make_business(
            'a@example.com',
            'Business A',
        )
        _, business_b = self.make_business(
            'b@example.com',
            'Business B',
        )
        _, business_c = self.make_business(
            'c@example.com',
            'Business C',
        )

        TeamMember.objects.create(
            business=business_b,
            user=user_a,
            role='admin',
        )
        TeamMember.objects.create(
            business=business_c,
            user=user_a,
            role='voucher_support',
        )

        self.assertEqual(
            TeamMember.objects.filter(user=user_a).count(),
            2,
        )

    def test_owner_can_switch_to_business_where_they_are_admin(self):
        user_a, business_a = self.make_business(
            'a@example.com',
            'Business A',
        )
        _, business_b = self.make_business(
            'b@example.com',
            'Business B',
        )

        TeamMember.objects.create(
            business=business_b,
            user=user_a,
            role='admin',
        )

        self.client.force_login(user_a)

        response = self.client.post(
            reverse(
                'business_switch',
                args=[business_b.pk],
            )
        )
        self.assertRedirects(
            response,
            reverse('dashboard'),
        )

        response = self.client.get(
            reverse('dashboard')
        )
        self.assertEqual(
            response.wsgi_request.tt_business.pk,
            business_b.pk,
        )
        self.assertEqual(
            response.wsgi_request.tt_role,
            'admin',
        )

        self.client.post(
            reverse(
                'business_switch',
                args=[business_a.pk],
            )
        )
        response = self.client.get(
            reverse('dashboard')
        )

        self.assertEqual(
            response.wsgi_request.tt_business.pk,
            business_a.pk,
        )
        self.assertEqual(
            response.wsgi_request.tt_role,
            'owner',
        )

    def test_cannot_switch_to_unrelated_business(self):
        user_a, business_a = self.make_business(
            'a@example.com',
            'Business A',
        )
        _, business_b = self.make_business(
            'b@example.com',
            'Business B',
        )

        self.client.force_login(user_a)

        self.client.post(
            reverse(
                'business_switch',
                args=[business_b.pk],
            )
        )

        response = self.client.get(
            reverse('dashboard')
        )

        self.assertEqual(
            response.wsgi_request.tt_business.pk,
            business_a.pk,
        )

    def test_removing_one_membership_keeps_login_and_other_business(self):
        owner, business_a = self.make_business(
            'owner@example.com',
            'Business A',
        )

        user = User.objects.create_user(
            username='staff@example.com',
            email='staff@example.com',
            password='Test-pass-123!',
        )

        _, business_b = self.make_business(
            'b@example.com',
            'Business B',
        )

        first = TeamMember.objects.create(
            business=business_a,
            user=user,
            role='voucher_support',
        )
        second = TeamMember.objects.create(
            business=business_b,
            user=user,
            role='admin',
        )

        self.client.force_login(owner)

        response = self.client.post(
            reverse(
                'team_member_action',
                args=[first.pk],
            ),
            {'action': 'delete'},
        )

        self.assertRedirects(
            response,
            reverse('team'),
        )

        self.assertTrue(
            User.objects.filter(pk=user.pk).exists()
        )
        self.assertFalse(
            TeamMember.objects.filter(pk=first.pk).exists()
        )
        self.assertTrue(
            TeamMember.objects.filter(pk=second.pk).exists()
        )
