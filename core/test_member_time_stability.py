"""Regression tests for the recurring member 'No time limit' bug."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import durations
from . import member_time_stability as stability
from . import sync
from .member_time import expected_limit
from .models import (
    AgentCommand,
    Business,
    Router,
    RouterHotspotUser,
    Voucher,
)
from .models_member_plans import MemberPlan, MemberPlanAssignment


class MemberTimeStabilityTests(TestCase):
    def setUp(self):
        stability.install()

        self.user = User.objects.create_user(
            username="member-stability@example.com",
            email="member-stability@example.com",
            password="pw12345678",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Member Stability Test",
            owner_name="Owner",
            phone="2200001",
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

    def test_generic_duration_sender_uses_exact_member_clock(self):
        want = expected_limit(self.member)
        self.assertEqual(want, "30d2m")
        self.assertEqual(durations.router_limit(self.member), want)

    def test_sync_early_bound_alias_is_replaced_too(self):
        want = expected_limit(self.member)
        self.assertIs(sync.router_limit, durations.router_limit)
        self.assertEqual(sync.router_limit(self.member), want)

    def test_normal_voucher_still_uses_original_generic_rule(self):
        voucher = Voucher.objects.create(
            business=self.business,
            router=self.router,
            code="NORMAL-1",
            plan_name="Daily",
            duration_minutes=1440,
            source="taptap",
            status="active",
        )
        self.assertEqual(durations.router_limit(voucher), "1d")

    def test_inventory_zero_limit_queues_exact_member_repair(self):
        RouterHotspotUser.objects.create(
            business=self.business,
            router=self.router,
            username=self.member.code,
            profile=self.plan.router_profile_name,
            limit_uptime="0s",
            source="taptap",
            is_present=True,
        )

        count = stability.repair_member_drift(self.router)
        self.assertEqual(count, 1)

        cmd = AgentCommand.objects.get(
            router=self.router,
            kind="hotspot_users_limit",
            status="queued",
        )
        self.assertEqual(cmd.params["users"][0]["n"], self.member.code)
        self.assertEqual(cmd.params["users"][0]["lim"], "30d2m")
        self.assertEqual(
            cmd.params["users"][0]["prof"],
            self.plan.router_profile_name,
        )
        self.assertTrue(cmd.params["member_alignment"]["automatic"])
        self.assertEqual(
            cmd.params["member_alignment"]["reason"],
            "inventory_drift",
        )

        self.member.refresh_from_db()
        self.assertEqual(self.member.mikrotik_sync_status, "Queued")

    def test_inventory_correct_member_queues_nothing(self):
        RouterHotspotUser.objects.create(
            business=self.business,
            router=self.router,
            username=self.member.code,
            profile=self.plan.router_profile_name,
            limit_uptime="30d2m",
            source="taptap",
            is_present=True,
        )

        self.assertEqual(stability.repair_member_drift(self.router), 0)
        self.assertFalse(
            AgentCommand.objects.filter(
                router=self.router,
                kind="hotspot_users_limit",
            ).exists()
        )

    def test_pending_exact_repair_is_not_duplicated(self):
        RouterHotspotUser.objects.create(
            business=self.business,
            router=self.router,
            username=self.member.code,
            profile=self.plan.router_profile_name,
            limit_uptime="0s",
            source="taptap",
            is_present=True,
        )

        self.assertEqual(stability.repair_member_drift(self.router), 1)
        self.assertEqual(stability.repair_member_drift(self.router), 0)
        self.assertEqual(
            AgentCommand.objects.filter(
                router=self.router,
                kind="hotspot_users_limit",
                status__in=["queued", "sent"],
            ).count(),
            1,
        )

    def test_wrong_profile_is_also_repaired(self):
        RouterHotspotUser.objects.create(
            business=self.business,
            router=self.router,
            username=self.member.code,
            profile="default",
            limit_uptime="30d2m",
            source="taptap",
            is_present=True,
        )

        self.assertEqual(stability.repair_member_drift(self.router), 1)
        cmd = AgentCommand.objects.get(
            router=self.router,
            kind="hotspot_users_limit",
        )
        self.assertEqual(
            cmd.params["users"][0]["prof"],
            self.plan.router_profile_name,
        )
