"""Fit's How-to tours: what each person is offered, every page exists, and Fit offers "Show me" in its replies."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import fit_tools, fit_tours as ft
from core.models import Business
from core.models_fit import FitSettings
from core.models_team import TeamMember
from core.permissions import ROLES

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'fittours'}}


class Catalogue(TestCase):
    def test_every_page_exists_and_targets_are_well_formed(self):
        for t in ft.all_tours():
            self.assertTrue(t['steps'], t['slug'])
            for s in t['steps']:
                if s['page']:
                    reverse(s['page'])                                    # NoReverseMatch = a broken tour
                tg = s['target']
                self.assertTrue(tg in (None, 'nav') or tg.split(':', 1)[0] in ('text', 'field', 'sel'), (t['slug'], tg))
                if s['match']:
                    self.assertTrue(s['go'] or any(x['page'] for x in t['steps']), t['slug'])
        slugs = [t['slug'] for t in ft.all_tours()]
        self.assertEqual(len(slugs), len(set(slugs)))
        for must in ('add-router', 'rotate-token', 'troubleshoot-link', 'generate-vouchers', 'add-member', 'add-staff', 'find-voucher'):
            self.assertIn(must, slugs)

    def test_every_help_guide_is_a_tour(self):
        from core.help_guides import GUIDES
        covered = {t.get('guide') for t in ft.TOURS} | {t['slug'][2:] for t in ft.all_tours() if t['slug'].startswith('g-')}
        self.assertEqual({g['slug'] for g in GUIDES} - covered, set())


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class PerPerson(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        FitSettings.objects.create(business=self.biz)

    def test_owner_gets_everything_staff_only_their_pages(self):
        own = {t['slug'] for t in ft.for_person(frozenset(), 'owner')}
        self.assertIn('add-router', own)
        support = {t['slug']: t for t in ft.for_person(ROLES['voucher_support'][2], 'voucher_support')}
        self.assertIn('find-voucher', support)
        self.assertNotIn('add-router', support, 'routers are for people who manage the network')
        self.assertNotIn('add-staff', support)
        for t in support.values():
            for s in t['steps']:
                self.assertTrue(s['url'] is None or s['url'].startswith('/'))

    def test_endpoint(self):
        self.client.force_login(self.owner)
        d = self.client.get('/fit/tours/').json()
        self.assertTrue(d['ok'])
        t = next(x for x in d['tours'] if x['slug'] == 'add-router')
        self.assertEqual(t['steps'][0]['url'], reverse('routers'))
        self.assertEqual(t['steps'][0]['label'], 'MikroTik Control')
        self.assertTrue(d['categories'])

    def test_viewer_cannot_see_router_tours(self):
        viewer = User.objects.create_user('v@x.gm', 'v@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.biz, user=viewer, is_active=True, role='viewer')
        self.client.force_login(viewer)
        slugs = {t['slug'] for t in self.client.get('/fit/tours/').json()['tours']}
        self.assertNotIn('rotate-token', slugs)

    def test_fit_offers_show_me(self):
        ctx = fit_tools.FitContext(business=self.biz, user=self.owner, perms=frozenset(ROLES['owner'][2]), settings=FitSettings.objects.get(business=self.biz))
        out = fit_tools.t_help(ctx, 'how do I rotate the router token')
        self.assertIn('show_me', out)
        self.assertEqual(ctx.cards['tours'][0]['slug'], 'rotate-token')
