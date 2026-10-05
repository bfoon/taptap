"""Free-access / HotSpot walled-garden tests.

Run:
  docker compose exec web python manage.py test core.test_free_access
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent
from .free_access import (
    FreeAccessError,
    apply_api,
    clean_items,
    link_command_body,
    normalize_host,
)
from .models import Business
from .models_free_access import FreeAccessSite


HTML = {'HTTP_ACCEPT': 'text/html'}


class FakeWG:
    def __init__(self, rows=None):
        self.rows = [dict(x) for x in (rows or [])]
        self.added = []
        self.sets = []
        self.removed = []

    def get(self):
        return [dict(x) for x in self.rows]

    def add(self, **kwargs):
        self.added.append(kwargs)
        return '*new'

    def set(self, **kwargs):
        self.sets.append(kwargs)

    def remove(self, **kwargs):
        self.removed.append(kwargs)


class FakeService:
    def __init__(self, resource):
        self.wg = resource

    def resource(self, path):
        assert path == '/ip/hotspot/walled-garden'
        return self.wg


@override_settings(AUTH_EMAIL_OTP=False)
class FreeAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'owner@example.com',
            'owner@example.com',
            'pw',
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name='TapTap',
            owner_name='Owner',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.client.force_login(self.user)

    def test_url_is_normalized_to_host(self):
        self.assertEqual(
            normalize_host('https://Pay.Example.com/login?x=1'),
            'pay.example.com',
        )
        self.assertEqual(
            normalize_host('*.cdn.example.com'),
            'cdn.example.com',
        )

    def test_invalid_broad_value_is_rejected(self):
        with self.assertRaises(FreeAccessError):
            normalize_host('*')

    def test_duplicate_hosts_are_deduplicated(self):
        rows = clean_items([
            {'host': 'https://example.com/a', 'purpose': 'advert'},
            {'host': 'example.com', 'purpose': 'portal'},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['host'], 'example.com')

    def test_settings_form_saves_sites(self):
        payload = (
            '[{"host":"https://pay.example.com/login",'
            '"label":"Customer payment","purpose":"portal",'
            '"include_subdomains":true,"enabled":true}]'
        )
        response = self.client.post(
            reverse('settings'),
            {
                'free_access_form': '1',
                'free_access_json': payload,
            },
            **HTML,
        )
        self.assertEqual(response.status_code, 302)
        row = FreeAccessSite.objects.get(business=self.business)
        self.assertEqual(row.host, 'pay.example.com')
        self.assertEqual(row.purpose, 'portal')
        self.assertTrue(row.include_subdomains)

    def test_api_reconcile_preserves_manual_rules(self):
        manual = {
            'id': '*1',
            'dst-host': 'manual.example.net',
            'comment': 'Made in WinBox',
        }
        stale = {
            'id': '*2',
            'dst-host': 'old.example.com',
            'comment': 'TapTap-free:host:old.example.com',
        }
        wg = FakeWG([manual, stale])

        FreeAccessSite.objects.create(
            business=self.business,
            host='ads.example.com',
            purpose='advert',
            include_subdomains=True,
        )

        added, updated, removed = apply_api(
            FakeService(wg),
            self.business,
        )

        self.assertEqual(added, 2)
        self.assertEqual(updated, 0)
        self.assertEqual(removed, 1)
        self.assertEqual(
            wg.removed,
            [{'id': '*2'}],
        )
        self.assertTrue(
            any(x['dst_host'] == 'ads.example.com' for x in wg.added)
        )
        self.assertTrue(
            any(x['dst_host'] == '*.ads.example.com' for x in wg.added)
        )

    def test_link_command_is_fixed_catalogue_and_only_managed_rules(self):
        self.assertIn('walled_garden_sync', agent.SAFE_KINDS)

        body = link_command_body({
            'sites': [
                {'host': 'ads.example.com', 'sub': True},
                {'host': 'pay.example.org', 'sub': False},
            ],
        })

        self.assertIn('/ip hotspot walled-garden', body)
        self.assertIn('TapTap-free:', body)
        self.assertIn('*.ads.example.com', body)
        self.assertIn('pay.example.org', body)
        self.assertNotIn('manual.example', body)

    @patch('core.free_access.apply_all')
    def test_save_and_apply_calls_all_routers(self, mocked):
        mocked.return_value = [(True, 'Router A: queued')]
        payload = (
            '[{"host":"ads.example.com","purpose":"advert",'
            '"enabled":true}]'
        )

        response = self.client.post(
            reverse('settings'),
            {
                'free_access_form': '1',
                'free_access_apply': '1',
                'free_access_json': payload,
            },
            **HTML,
        )

        self.assertEqual(response.status_code, 302)
        mocked.assert_called_once()
        args, _ = mocked.call_args
        self.assertEqual(args[0].pk, self.business.pk)

    def test_clearing_list_is_valid(self):
        FreeAccessSite.objects.create(
            business=self.business,
            host='old.example.com',
        )

        response = self.client.post(
            reverse('settings'),
            {
                'free_access_form': '1',
                'free_access_json': '[]',
            },
            **HTML,
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            FreeAccessSite.objects.filter(business=self.business).exists()
        )
