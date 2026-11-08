"""Fit's wardrobe: skins and masks per person or for the whole team, and "Auto" following the calendar."""
import json
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings as dj
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import fit_skins as fs
from core.models import Business
from core.models_fit import FitSettings
from core.models_team import TeamMember

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'fitskins'}}


class Calendar(TestCase):
    def test_seasons(self):
        self.assertEqual(fs.season(date(2026, 12, 10)), 'santa')
        self.assertEqual(fs.season(date(2026, 12, 31)), 'party')
        self.assertEqual(fs.season(date(2027, 1, 1)), 'party')
        self.assertEqual(fs.season(date(2027, 2, 18)), 'gambia')            # Independence Day
        self.assertEqual(fs.season(date(2026, 10, 31)), 'pumpkin')
        self.assertEqual(fs.season(date(2027, 3, 10)), 'eid')               # Eid al-Fitr
        self.assertEqual(fs.season(date(2027, 2, 20)), 'eid')               # Ramadan
        self.assertEqual(fs.season(date(2027, 5, 17)), 'tobaski')           # Eid al-Adha
        self.assertEqual(fs.season(date(2026, 7, 7)), 'classic')

    def test_catalogue_has_plenty_and_every_look_is_drawn(self):
        self.assertGreaterEqual(len(fs.CATALOGUE), 20)
        self.assertEqual(len(fs.IDS), len(fs.CATALOGUE), 'ids are unique')
        js = (Path(dj.BASE_DIR) / 'static' / 'js' / 'fit-skins.js').read_text()
        for skin in fs.IDS:
            self.assertIn(f'\n    {skin}: {{', js, f'{skin} has no drawing in fit-skins.js')
        self.assertTrue({k for k, _ in fs.GROUPS} >= {c[3] for c in fs.CATALOGUE}, 'every look is in a shown group')


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Choosing(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Foon')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.s = FitSettings.objects.create(business=self.biz)
        self.staff = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456', first_name='Awa')
        TeamMember.objects.create(business=self.biz, user=self.staff, is_active=True, role='voucher_support')

    def post(self, data):
        return self.client.post('/fit/look/', json.dumps(data), content_type='application/json')

    def test_my_look_team_look_and_auto(self):
        self.assertEqual(fs.resolve(self.s, self.owner, date(2026, 7, 7)), 'classic')          # nothing picked: auto
        self.assertEqual(fs.resolve(self.s, self.owner, date(2026, 12, 10)), 'santa')
        fs.choose(self.s, self.owner, 'ninja', team=True)
        self.assertEqual(fs.resolve(self.s, self.staff), 'ninja')                              # the team's look
        fs.choose(self.s, self.staff, 'panda')
        self.assertEqual(fs.resolve(self.s, self.staff), 'panda')                              # my own wins
        fs.choose(self.s, self.staff, '')
        self.assertEqual(fs.resolve(self.s, self.staff), 'ninja')                              # back to the team
        fs.choose(self.s, self.staff, 'auto')
        self.assertEqual(fs.resolve(self.s, self.staff, date(2026, 10, 30)), 'pumpkin')
        with self.assertRaises(ValueError):
            fs.choose(self.s, self.staff, 'darth-vader')
        self.assertIn('Foon', fs.greeting('santa', 'Foon'))

    def test_endpoint_and_state(self):
        self.client.force_login(self.owner)
        d = self.post({'skin': 'tobaski'}).json()
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['look']['skin'], 'tobaski')
        self.assertIn('Dewenati', d['look']['wearing']['greeting'])
        st = self.client.get('/fit/state/').json()
        self.assertEqual(st['look']['skin'], 'tobaski')
        self.assertTrue(st['look']['can_set_team'])
        self.assertEqual(len(st['look']['list']), len(fs.CATALOGUE))
        self.assertEqual(self.post({'skin': 'nope'}).status_code, 400)
        self.assertTrue(self.post({'skin': 'gambia', 'team': True}).json()['ok'])

    def test_staff_choose_for_themselves_only(self):
        self.client.force_login(self.staff)
        self.assertTrue(self.post({'skin': 'cat'}).json()['ok'])
        r = self.post({'skin': 'cat', 'team': True})
        self.assertEqual(r.status_code, 403)
        self.s.refresh_from_db()
        self.assertEqual(self.s.looks.get(str(self.staff.pk)), 'cat')
        self.assertNotIn('team', self.s.looks)
        self.assertFalse(self.client.get('/fit/state/').json()['look']['can_set_team'])

    def test_costume_line_in_fits_instructions(self):
        from core import fit_ai, fit_tools
        fs.choose(self.s, self.owner, 'pirate')
        ctx = fit_tools.FitContext(business=self.biz, user=self.owner, perms=frozenset(), settings=self.s)
        self.assertIn('Pirate', fit_ai.system_prompt(ctx))
        fs.choose(self.s, self.owner, 'classic')
        self.assertNotIn('dressed as', fit_ai.system_prompt(ctx))
