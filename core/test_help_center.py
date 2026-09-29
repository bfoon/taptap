"""Support › Help Center.

Run:  DB_ENGINE=sqlite python manage.py test core.test_help_center
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .help_guides import BY_SLUG, CATEGORIES, GUIDES
from .models import Business, Router
from .models_team import TeamMember

HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(AUTH_EMAIL_OTP=False)
class HelpCenterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('hc@example.com', 'hc@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.client.force_login(self.owner)

    def test_guides_are_well_formed(self):
        cats = {k for k, _, _ in CATEGORIES}
        self.assertEqual(len(BY_SLUG), len(GUIDES))                 # no duplicate slugs
        for g in GUIDES:
            self.assertIn(g['category'], cats, g['slug'])
            self.assertTrue(g['steps'], g['slug'])
            for r in g.get('related', []):
                self.assertIn(r, BY_SLUG, f'{g["slug"]} → {r}')
            for s in g['steps']:
                if s['link']:
                    reverse(s['link'][0])                          # every button points to a real page
        for need in ('add-router-link', 'add-router-api', 'troubleshoot-link', 'troubleshoot-api', 'fair-usage', 'shared-auto-warn'):
            self.assertIn(need, BY_SLUG)

    def test_index_and_every_guide_open(self):
        r = self.client.get(reverse('support'), **HTML)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'How can we help?')
        self.assertContains(r, 'hcIndex')
        self.assertEqual(len(r.context['index_json']), len(GUIDES))   # a list, not a JSON string (the search needs it)
        for g in GUIDES:
            r = self.client.get(reverse('support_guide', args=[g['slug']]), **HTML)
            self.assertEqual(r.status_code, 200, g['slug'])
            self.assertContains(r, escape(g['steps'][0]['title']))
        self.assertEqual(self.client.get(reverse('support_guide', args=['nope']), **HTML).status_code, 404)

    def test_markup_is_safe(self):
        r = self.client.get(reverse('support_guide', args=['troubleshoot-link']), **HTML)
        self.assertContains(r, '<code>TapTap Link OK</code>', html=False)
        self.assertContains(r, '<b>WinBox › New Terminal</b>', html=False)

    def test_links_follow_the_role(self):
        u = User.objects.create_user('v@hc.com', 'v@hc.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='viewer')
        self.client.force_login(u)
        r = self.client.get(reverse('support_guide', args=['add-router-link']), **HTML)
        self.assertEqual(r.status_code, 200)                      # everyone can read help
        self.assertNotContains(r, reverse('routers'))            # but no button to a page they can't open
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('support_guide', args=['add-router-link']), **HTML), reverse('routers'))

    def test_router_health_links_to_the_right_fix(self):
        Router.objects.create(business=self.biz, name='Garden AP', ip_address='192.168.88.1', username='u', password='p', status='Offline',
                              last_error='timed out', connection_mode='api')
        Router.objects.create(business=self.biz, name='Main Hall', ip_address='', username='u', password='p', connection_mode='agent')
        r = self.client.get(reverse('support'), **HTML)
        self.assertContains(r, 'Garden AP')
        self.assertContains(r, reverse('support_guide', args=['troubleshoot-api']))
        self.assertContains(r, reverse('support_guide', args=['troubleshoot-link']))
