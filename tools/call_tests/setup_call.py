"""Creates two people in one business with a direct chat, logs both in, prints what the browser tests need."""
import json, os, re, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO)); os.chdir(REPO)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django; django.setup()
from datetime import timedelta
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client
from django.utils import timezone
from core.models import Business
from core.models_team import TeamMember
from core import chat
owner = User.objects.create_user('sharer', password='x', first_name='Fatou', last_name='Sowe')
helper = User.objects.create_user('viewer', password='x', first_name='Lamin', last_name='Jallow')
biz = Business.objects.create(user=owner, business_name='Banjul Wi-Fi', owner_name='Fatou', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
TeamMember.objects.create(business=biz, user=helper, role='manager')
t = chat.direct_thread(biz, owner, helper)
def session(u):
    s = SessionStore(); s['_auth_user_id'] = str(u.pk); s['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
    s['_auth_user_hash'] = u.get_session_auth_hash(); s.create(); return s.session_key
c = Client(); c.force_login(owner); html = c.get('/', HTTP_HOST='127.0.0.1').content.decode()
title = re.search(r'<title>(.*?)</title>', html, re.S).group(1).strip()
print(json.dumps({'thread': t.pk, 'sharer': session(owner), 'viewer': session(helper), 'title': title}))
