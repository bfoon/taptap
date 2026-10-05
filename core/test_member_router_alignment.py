"""Regression tests for Member Plan ↔ MikroTik alignment."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import member_router_alignment as align
from .models import (
    AgentCommand,
    Business,
    Router,
    RouterHotspotUser,
    Voucher,
)
from .models_member_plans import (
    MemberPlan,
    MemberPlanAssignment,
)


class MemberRouterAlignmentTests(TestCase):
    def setUp(self):
        align.install()

        self.user = User.objects.create_user(
            username="member-align@example.com",
            email="member-align@example.com",
            password="pw12345678",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Member Alignment Test",
            owner_name="Owner",
            phone="2200000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.router = Router.objects.create(
            business=self.business,
            name="TapTap K",
            ip_address="",
            username="",
            password="",
            connection_mode="agent",
            status="Online",
        )
        self.plan = MemberPlan.objects.create(
            business=self.business,
            name="Member Monthly 7",
            price="1000.00",
            duration_minutes=43200,
            duration_unit="months",
            max_devices=7,
            speed_limit="2M/2M",
        )
        used = timezone.now() - timedelta(days=4)
        self.member = Voucher.objects.create(
            business=self.business,
            router=self.router,
            code="jimmy",
            password="testpass",
            login_type="member",
            plan_name=self.plan.name,
            price=self.plan.price,
            duration_minutes=self.plan.duration_minutes,
            max_devices=self.plan.max_devices,
            rate_limit=self.plan.speed_limit,
            router_profile=self.plan.router_profile_name,
            source="taptap",
            status="active",
            used_at=used,
            expires_at=used + timedelta(days=30, minutes=2),
            mikrotik_sync_status="Synced",
        )
        MemberPlanAssignment.objects.create(
            member=self.member,
            plan=self.plan,
            assigned_by=self.user,
        )

    def test_desired_uses_member_plan_profile_and_exact_member_clock(self):
        wanted = align.desired(self.member, self.plan)
        self.assertEqual(
            wanted["profile"],
            self.plan.router_profile_name,
        )
        self.assertEqual(wanted["shared"], 7)
        self.assertEqual(wanted["rate"], "2M/2M")
        self.assertEqual(wanted["limit"], "30d2m")

    @mock.patch("core.linkops.uses_link", return_value=True)
    def test_link_push_never_uses_bulk_pending_gate(self, _uses_link):
        result = align.aligned_push_one(
            self.member,
            self.plan,
        )
        self.assertTrue(result)

        kinds = list(
            AgentCommand.objects
            .filter(router=self.router)
            .order_by("created_at")
            .values_list("kind", flat=True)
        )
        self.assertEqual(
            kinds,
            ["hotspot_users", "hotspot_users_limit"],
        )

        final = AgentCommand.objects.filter(
            router=self.router,
            kind="hotspot_users_limit",
        ).latest("created_at")
        user = final.params["users"][0]
        self.assertEqual(user["n"], "jimmy")
        self.assertEqual(user["lim"], "30d2m")
        self.assertEqual(
            user["prof"],
            self.plan.router_profile_name,
        )
        self.assertEqual(
            final.params["member_alignment"]["voucher_id"],
            self.member.pk,
        )

        self.member.refresh_from_db()
        self.assertEqual(
            self.member.mikrotik_sync_status,
            "Queued",
        )

    @mock.patch("core.linkops.uses_link", return_value=True)
    def test_pending_fix_hides_repeated_red_mismatch(self, _uses_link):
        align.aligned_push_one(
            self.member,
            self.plan,
        )

        original = mock.Mock(return_value={
            self.member.pk: {
                "text": "No time limit",
                "mismatch": True,
                "time_mismatch": True,
                "profile_mismatch": False,
                "expected_time": "30 days 2 minutes",
            }
        })
        wrapped = align._aligned_router_view(original)
        rows = wrapped(
            self.business,
            [self.member],
        )

        self.assertFalse(rows[self.member.pk]["mismatch"])
        self.assertTrue(rows[self.member.pk]["pending_fix"])
        self.assertIn(
            "Correction queued",
            rows[self.member.pk]["text"],
        )

    def test_successful_link_alignment_updates_router_mirror(self):
        cmd = AgentCommand.objects.create(
            router=self.router,
            kind="hotspot_users_limit",
            params={
                "member_alignment": {
                    "voucher_id": self.member.pk,
                    "username": self.member.code,
                    "profile": self.plan.router_profile_name,
                    "limit": "30d2m",
                }
            },
            label="Align jimmy",
            status="done",
            done_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        RouterHotspotUser.objects.create(
            business=self.business,
            router=self.router,
            username=self.member.code,
            profile=self.plan.router_profile_name,
            limit_uptime="0s",
            source="taptap",
        )

        align._finish_link_alignment(cmd)

        mirror = RouterHotspotUser.objects.get(
            router=self.router,
            username=self.member.code,
        )
        self.assertEqual(mirror.limit_uptime, "30d2m")
        self.assertEqual(
            mirror.profile,
            self.plan.router_profile_name,
        )
        self.member.refresh_from_db()
        self.assertEqual(
            self.member.mikrotik_sync_status,
            "Synced",
        )

    def test_failed_link_alignment_does_not_hide_error(self):
        cmd = AgentCommand.objects.create(
            router=self.router,
            kind="hotspot_users_limit",
            params={
                "member_alignment": {
                    "voucher_id": self.member.pk,
                    "username": self.member.code,
                    "profile": self.plan.router_profile_name,
                    "limit": "30d2m",
                }
            },
            label="Align jimmy",
            status="failed",
            result="Router rejected the limit",
            done_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        align._finish_link_alignment(cmd)

        self.member.refresh_from_db()
        self.assertEqual(
            self.member.mikrotik_sync_status,
            "Error",
        )
        self.assertIn(
            "Router rejected",
            self.member.mikrotik_sync_error,
        )
