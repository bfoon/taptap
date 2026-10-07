from datetime import timedelta
import json

from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import resolve
from django.utils import timezone

from .models import Business
from .models_team import PlatformAudit
from . import support_access, support_presence


class SupportPresenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()

        self.owner = User.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="test",
            first_name="Owner",
        )
        self.support = User.objects.create_superuser(
            username="support@example.com",
            email="support@example.com",
            password="test",
            first_name="Baboucarr",
        )
        self.business = Business.objects.create(
            user=self.owner,
            business_name="Presence Test",
            owner_name="Owner",
            phone="7000000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.token = "presence-test-token"
        now = timezone.now()

        PlatformAudit.objects.create(
            actor=self.support,
            business=self.business,
            action=support_access.ACTION_REQUESTED,
            details=support_access._dump(
                {
                    "token": self.token,
                    "support_user_id": self.support.pk,
                    "support_name": "Baboucarr",
                    "support_email": self.support.email,
                    "reason": "Finance support",
                    "request_expires": (now + timedelta(hours=24)).isoformat(),
                }
            ),
        )
        PlatformAudit.objects.create(
            actor=self.owner,
            business=self.business,
            action=support_access.ACTION_APPROVED,
            details=support_access._dump(
                {
                    "token": self.token,
                    "support_user_id": self.support.pk,
                    "grant_expires": (now + timedelta(hours=1)).isoformat(),
                }
            ),
        )
        self.grant = {
            "business_id": self.business.pk,
            "token": self.token,
            "support_user_id": self.support.pk,
            "expires_at": (now + timedelta(hours=1)).isoformat(),
        }

    def support_request(self, path="/finance/"):
        request = self.rf.get(path)
        request.user = self.support
        request.session = {
            support_access.VIEW_AS_KEY: self.business.pk,
            support_access.GRANT_KEY: dict(self.grant),
        }
        request.tt_business = self.business
        request.tt_view_as = self.business
        request.resolver_match = resolve(path.split('?', 1)[0])   # Django resolves the path only, never the query
        return request

    def owner_request(self, path="/finance/"):
        request = self.rf.get(path)
        request.user = self.owner
        request.session = {}
        request.tt_business = self.business
        request.tt_view_as = None
        request.resolver_match = resolve(path.split('?', 1)[0])   # Django resolves the path only, never the query
        return request

    def test_finance_page_name_is_friendly(self):
        request = self.support_request("/finance/")
        self.assertEqual(support_presence.page_name(request), "Finance")
        self.assertEqual(support_presence.safe_path(request), "/finance/")

    def test_navigation_records_page_without_query_string(self):
        request = self.support_request("/finance/?secret=value")
        support_presence._record_support_navigation(request)
        data = cache.get(
            support_presence._presence_key(self.business.pk, self.token)
        )
        self.assertEqual(data["page_name"], "Finance")
        self.assertEqual(data["page_path"], "/finance/")
        self.assertNotIn("secret", data["page_path"])

    def test_owner_live_endpoint_reports_support_page(self):
        request = self.support_request("/finance/")
        support_presence._record_support_navigation(request)

        owner = self.owner_request("/finance/")
        response = support_presence.live_state(owner)
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertTrue(payload["sessions"][0]["live"])
        self.assertEqual(payload["sessions"][0]["support_name"], "Baboucarr")
        self.assertEqual(payload["sessions"][0]["page_name"], "Finance")

    def test_owner_banner_injected_into_portal_page(self):
        request = self.owner_request("/finance/")
        response = HttpResponse(
            '<html><body><main class="main-content"><h1>Finance</h1></main></body></html>',
            content_type="text/html",
        )
        result = support_presence._inject_response(request, response)
        html = result.content.decode()
        self.assertIn("ttSupportPresence", html)
        self.assertIn("support-presence/live", html)
        self.assertIn("Revoke access", html)

    def test_support_page_gets_heartbeat_script(self):
        request = self.support_request("/finance/")
        response = HttpResponse(
            '<html><body><main class="main-content">Finance</main></body></html>',
            content_type="text/html",
        )
        result = support_presence._inject_response(request, response)
        html = result.content.decode()
        self.assertIn("tt-support-presence-heartbeat", html)
        self.assertIn("support-presence/heartbeat", html)

    def test_revoke_uses_existing_audited_owner_decision(self):
        request = self.rf.post(
            "/api/support-presence/revoke/",
            {"token": self.token},
        )
        request.user = self.owner
        request.session = {}
        request.tt_business = self.business
        request.tt_view_as = None

        response = support_presence.revoke(request)
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])

        state = support_access.state_for_token(self.business, self.token)
        self.assertEqual(state["status"], "revoked")
        self.assertTrue(
            PlatformAudit.objects.filter(
                business=self.business,
                action=support_access.ACTION_REVOKED,
            ).exists()
        )

    def test_revoked_support_fails_heartbeat(self):
        # Revoke first.
        owner = self.rf.post(
            "/api/support-presence/revoke/",
            {"token": self.token},
        )
        owner.user = self.owner
        owner.session = {}
        owner.tt_business = self.business
        owner.tt_view_as = None
        support_presence.revoke(owner)

        # The old support session can no longer validate.
        request = self.rf.post("/api/support-presence/heartbeat/")
        request.user = self.support
        request.session = {
            support_access.VIEW_AS_KEY: self.business.pk,
            support_access.GRANT_KEY: dict(self.grant),
        }
        request.tt_business = self.business
        request.tt_view_as = self.business
        response = support_presence.heartbeat(request)
        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content)
        self.assertTrue(payload["revoked"])
