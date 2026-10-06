from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .business_roaming import balances, ingest_roaming_sessions
from .models import Business, Router, Voucher
from .models_collaboration_buy import BusinessCollaboration
from .models_roaming import RoamingAgreement, RoamingMirror, RoamingOffset, RoamingPayment, RoamingSession


class BusinessRoamingTests(TestCase):
    def setUp(self):
        self.ua = User.objects.create_user(username="a@example.com", email="a@example.com", password="test")
        self.ub = User.objects.create_user(username="b@example.com", email="b@example.com", password="test")
        far = timezone.now() + timedelta(days=30)
        self.a = Business.objects.create(user=self.ua, business_name="Business A", owner_name="A", phone="7000001", trial_ends_at=far, is_unlimited=True, currency="D")
        self.b = Business.objects.create(user=self.ub, business_name="Business B", owner_name="B", phone="7000002", trial_ends_at=far, is_unlimited=True, currency="D")
        self.collab = BusinessCollaboration.objects.create(source_business=self.a, target_business=self.b, status="active", source_grants=["dashboard.view"], target_grants=["dashboard.view"], invited_by=self.ua, accepted_by=self.ub, accepted_at=timezone.now())
        self.agreement = RoamingAgreement.objects.create(collaboration=self.collab, status="active", enabled=True, source_host_rate_per_hour=Decimal("3.00"), target_host_rate_per_hour=Decimal("2.00"), billing_increment_minutes=1, currency="D", proposed_by_business=self.a, proposed_by=self.ua, accepted_by=self.ub, accepted_at=timezone.now())
        self.router_a = Router.objects.create(business=self.a, name="A Router", ip_address="192.168.1.1", username="admin", password="x")
        self.router_b = Router.objects.create(business=self.b, name="B Router", ip_address="192.168.2.1", username="admin", password="x")
        now = timezone.now()
        self.va = Voucher.objects.create(business=self.a, router=self.router_a, code="AVOUCH24", plan_name="24 Hours", price=25, duration_minutes=1440, max_devices=1, source="taptap", status="active", sold_at=now-timedelta(hours=16), used_at=now-timedelta(hours=16), expires_at=now+timedelta(hours=8))
        self.vb = Voucher.objects.create(business=self.b, router=self.router_b, code="BVOUCH24", plan_name="24 Hours", price=25, duration_minutes=1440, max_devices=1, source="taptap", status="active", sold_at=now-timedelta(hours=12), used_at=now-timedelta(hours=12), expires_at=now+timedelta(hours=12))

    def test_14_hours_at_b_bills_a_at_b_host_rate(self):
        RoamingMirror.objects.create(agreement=self.agreement, voucher=self.va, router=self.router_b, issuer_business=self.a, visited_business=self.b, status="synced")
        now = timezone.now()
        ingest_roaming_sessions(self.router_b, [{"user":"AVOUCH24", "mac-address":"AA:BB:CC:DD:EE:01", "address":"172.16.0.20", "uptime":"14h", "id":"*A1"}], now)
        s = RoamingSession.objects.get(voucher=self.va, router=self.router_b)
        self.assertEqual(s.billable_minutes, 840)
        self.assertEqual(s.rate_per_hour, Decimal("2.00"))
        self.assertEqual(s.amount, Decimal("28.00"))
        b = balances(self.agreement)
        self.assertEqual(b["source_to_target_due"], Decimal("28.00"))
        self.assertEqual(b["target_to_source_due"], Decimal("0"))

    def test_two_way_debt_can_offset_and_leave_net_due(self):
        now = timezone.now()
        RoamingSession.objects.create(agreement=self.agreement, voucher=self.va, issuer_business=self.a, visited_business=self.b, router=self.router_b, session_key="a-at-b", username=self.va.code, started_at=now-timedelta(hours=14), last_seen_at=now, seconds_used=14*3600, billable_minutes=840, rate_per_hour=Decimal("2.00"), amount=Decimal("28.00"), status="ended", ended_at=now)
        RoamingSession.objects.create(agreement=self.agreement, voucher=self.vb, issuer_business=self.b, visited_business=self.a, router=self.router_a, session_key="b-at-a", username=self.vb.code, started_at=now-timedelta(hours=10), last_seen_at=now, seconds_used=10*3600, billable_minutes=600, rate_per_hour=Decimal("3.00"), amount=Decimal("30.00"), status="ended", ended_at=now)
        before = balances(self.agreement)
        self.assertEqual(before["source_to_target_due"], Decimal("28.00"))
        self.assertEqual(before["target_to_source_due"], Decimal("30.00"))
        self.assertEqual(before["available_offset"], Decimal("28.00"))
        RoamingOffset.objects.create(agreement=self.agreement, amount=Decimal("28.00"), created_by=self.ua)
        after = balances(self.agreement)
        self.assertEqual(after["source_to_target_due"], Decimal("0"))
        self.assertEqual(after["target_to_source_due"], Decimal("2.00"))
        RoamingPayment.objects.create(agreement=self.agreement, payer_business=self.b, payee_business=self.a, amount=Decimal("2.00"), payment_method="wave", recorded_by=self.ub)
        settled = balances(self.agreement)
        self.assertEqual(settled["source_to_target_due"], Decimal("0"))
        self.assertEqual(settled["target_to_source_due"], Decimal("0"))
