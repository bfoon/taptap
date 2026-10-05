from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from . import members as mem
from .agent import command_body
from .mikrotik import MikroTikService
from .models import (
    Business,
    Router,
    RouterHotspotProfile,
    RouterHotspotUser,
)
from .models_member_plans import MemberPlan
from .utils import voucher_profile


class UnlimitedMemberTests(TestCase):
    """Unlimited Member Plans must stay unlimited on MikroTik too."""

    def setUp(self):
        self.owner = User.objects.create_user(
            'owner',
            'o@x.com',
            'pw12345678',
        )
        self.b = Business.objects.create(
            user=self.owner,
            business_name='K',
            owner_name='A',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=9),
            is_unlimited=True,
        )
        self.r = Router.objects.create(
            business=self.b,
            name='Kotu',
            ip_address='1.1.1.1',
            username='a',
            password='b',
        )
        self.plan = MemberPlan.objects.create(
            business=self.b,
            name='VIP Members',
            price=Decimal('0'),
            duration_minutes=0,
            duration_unit='unlimited',
            max_devices=2,
            speed_limit='5M/5M',
            created_by=self.owner,
        )
        self.c = Client()
        self.c.force_login(self.owner)

    def member(self, username='awa'):
        return mem.create_member(
            self.b,
            username=username,
            same=True,
            plan_value=str(self.plan.pk),
            router=self.r,
            user=self.owner,
        )

    def test_unlimited_member_uses_plan_owned_unlimited_profile(self):
        v = self.member()
        prof, shared, rate = voucher_profile(v, self.plan)
        self.assertEqual(prof, self.plan.router_profile_name)
        self.assertTrue(prof.startswith('taptap-unlimited-member-plan-'))
        self.assertEqual(shared, 2)
        self.assertEqual(rate, '5M/5M')

    def test_router_profile_clears_time_limits(self):
        from types import SimpleNamespace as S

        profile = self.plan.router_profile_name
        body = command_body(
            S(
                kind='hotspot_users',
                params={
                    'profiles': [
                        {
                            'name': profile,
                            'shared': 2,
                            'rate': '5M/5M',
                        },
                    ],
                    'users': [
                        {
                            'n': 'awa',
                            'prof': profile,
                            'lim': '0s',
                            'dis': False,
                            'pw': 'secret',
                        },
                    ],
                },
            )
        )
        self.assertIn(
            'session-timeout=0s on-login=""',
            body,
        )

        added = {}

        class Res:
            def get(s, **k):
                return []

            def add(s, **k):
                added.update(k)
                return '*1'

        class Svc:
            router = self.r

            def resource(s, p):
                return Res()

        MikroTikService.ensure_hotspot_profile(
            Svc(),
            profile,
            2,
            '5M/5M',
        )
        self.assertEqual(
            (added['session_timeout'], added['on_login']),
            ('0s', ''),
        )

    def test_members_page_flags_wrong_router_profile(self):
        v = self.member()
        RouterHotspotProfile.objects.create(
            business=self.b,
            router=self.r,
            name='old-profile',
            session_timeout='',
            raw_data={},
        )
        RouterHotspotUser.objects.create(
            business=self.b,
            router=self.r,
            username='awa',
            profile='old-profile',
            is_present=True,
        )
        r = self.c.get('/members/')
        self.assertContains(r, 'expected ' + self.plan.router_profile_name)
        with mock.patch(
            'core.views_members.push_one',
            return_value=True,
        ) as pushed:
            self.c.post(
                f'/members/{v.pk}/push/',
                {'next': '/members/'},
            )
            self.assertTrue(pushed.called)
