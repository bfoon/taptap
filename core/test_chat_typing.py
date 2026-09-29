from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import chat
from .models import Business
from .models_chat import SupportAgent
from .models_team import TeamMember


class TypingBubbleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678', first_name='Baboucarr')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.awa = User.objects.create_user('awa', 'a@x.com', 'pw12345678', first_name='Awa')
        TeamMember.objects.create(business=self.b, user=self.awa, role='viewer')
        self.agent = User.objects.create_user('sup', 's@x.com', 'pw12345678', first_name='Sara'); SupportAgent.objects.create(user=self.agent)

    def test_typing_shows_in_conversation_and_list(self):
        team, sup = chat.team_thread(self.b), chat.support_thread(self.b)
        ca = Client(); ca.force_login(self.awa)
        ca.get(f'/chat/state/?open={team.pk}&typing=1&since=-1')
        co = Client(); co.force_login(self.owner)
        d = co.get(f'/chat/state/?open={team.pk}&since=-1').json()
        self.assertEqual([t['name'] for t in d['typing']], ['Awa'])
        self.assertEqual(d['typing_threads'], {str(team.pk): ['Awa']} if isinstance(next(iter(d['typing_threads'])), str) else {team.pk: ['Awa']})
        # support agent typing to a business account is marked as TapTap support
        chat.post(sup, self.owner, 'Hello, my router is offline')
        cs = Client(); cs.force_login(self.agent)
        cs.get(f'/chat/state/?open={sup.pk}&typing=1&since=-1')
        d = co.get(f'/chat/state/?open={sup.pk}&since=-1').json()
        self.assertTrue(d['typing'][0]['agent'])
        # the typer does not see their own bubble
        self.assertEqual(ca.get(f'/chat/state/?open={team.pk}&since=-1').json()['typing'], [])
