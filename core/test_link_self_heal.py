"""TapTap Link v6: the heartbeat repairs a wrong clock / dead DNS itself and reports outages when it is back.

Run:  DB_ENGINE=sqlite CELERY_EAGER=1 python manage.py test core.test_link_self_heal
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import agent as link
from .models import Business, Notification, Router, RouterAgent
from .test_link_install import routeros_balanced


class HeartbeatScriptTests(TestCase):
    def script(self):
        return link.agent_script('https://taptap.example.com', 'ttl_' + 'x' * 30, 'yes-without-crl')

    def test_balanced_and_versioned(self):
        t = self.script()
        self.assertTrue(routeros_balanced(t))
        self.assertIn(f'# TapTap Link v{link.SCRIPT_VERSION}', t)
        self.assertGreaterEqual(link.SCRIPT_VERSION, 6)
        self.assertIn('&hs=', t)                                       # the system-health add-on still applies

    def test_reports_outage_fields(self):
        t = self.script()
        self.assertIn('"&lf=" . $ttLinkFail . "&lw=" . $ttLinkWhy . "&lg=" . $ttLinkGw', t)
        self.assertIn(':set ttLinkFail ($ttLinkFail + 1)', t)

    def test_clock_repair_never_sends_the_token_unverified(self):
        t = self.script()
        hello = t[t.index('/hello'):]
        line = t[t.rindex('\n', 0, t.index('/hello')):t.index('\n', t.index('/hello'))]
        self.assertIn('check-certificate=no', line)
        self.assertNotIn('Authorization', line)
        self.assertNotIn('$tok', line)
        self.assertIn('/system clock set date=$sd time=$st', hello)

    def test_dns_fallback_is_scoped_to_taptap(self):
        t = self.script()
        self.assertIn(':local host "taptap.example.com"', t)
        self.assertIn('/ip dns static add name=$host address=$ip comment="TapTap Link DNS fallback"', t)
        self.assertIn('/ip dns static remove [find where comment="TapTap Link DNS fallback"]', t)   # never touches others

    def test_self_update_carries_the_new_script(self):
        body = link.self_update_body('https://taptap.example.com', 'yes-without-crl')
        self.assertIn('ttLinkFail', body)


@override_settings(SITE_URL='https://taptap.example.com')
class OutageReportTests(TestCase):
    def setUp(self):
        cache.clear()
        u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='Tower', status='Online', connection_mode='agent')
        self.agent = RouterAgent.objects.create(router=self.r, last_seen_at=timezone.now(), enrolled_at=timezone.now(),
                                                poll_seconds=10, script_version=link.SCRIPT_VERSION)

    def poll(self, **extra):
        data = {'id': 'Tower', 'ver': '7.15.3', 'sv': str(link.SCRIPT_VERSION), **extra}
        link.handle_poll(self.agent, data, '41.223.1.2', 'https://taptap.example.com')

    def test_back_after_dead_wan_is_explained(self):
        self.poll(lf='1080', lw='route', lg='192.168.1.1 ')
        out = cache.get(f'tt:link:outage:{self.r.pk}')
        self.assertEqual((out['missed'], out['why']), (1080, 'route'))
        self.assertIn('3 h 0 min', out['detail'])
        self.assertIn('192.168.1.1', out['detail'])
        self.assertIn('failover', out['detail'])

    def test_ordinary_polls_say_nothing(self):
        self.poll(lf='0')
        self.poll()
        self.assertIsNone(cache.get(f'tt:link:outage:{self.r.pk}'))

    def test_dns_reason(self):
        self.poll(lf='9', lw='dns', lg='')
        self.assertIn('DNS', cache.get(f'tt:link:outage:{self.r.pk}')['detail'])
