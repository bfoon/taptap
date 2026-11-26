"""The TapTap phone app, downloadable from TapTap's own pages (landing page section + /app/).

Visitors never see GitHub or Actions: the button is /app/get/android/, which counts the download and sends the APK.
Where the APK comes from, first match:

1. ``APP_LOCAL_DIR`` — a folder on this server; the newest ``TapTap*.apk`` in it is served as an attachment;
2. ``APP_ANDROID_URL`` — your own CDN / bucket address of the APK (``APP_ANDROID_VERSION`` names it);
3. the GitHub releases of ``APP_REPO`` (default bfoon/taptap). The "TapTap mobile app" workflow publishes every
   build of master/main as a release ``app-v<version>-<build>`` holding ``TapTap.apk`` (+ its SHA-256); the newest
   one is offered, with its "What's new" (mobile/WHATS_NEW.md, or the commit message).

The release list is cached for an hour (15 minutes after a failure). With no APK anywhere, the pages still work:
they offer TapTap in the phone's browser instead, and say the app is on its way.
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Sum
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger('taptap.app_download')

CACHE_KEY = 'app:android:release:v1'
CACHE_OK = 3600
CACHE_FAIL = 15 * 60
TAG_RE = re.compile(r'^app-v(\d+\.\d+\.\d+)(?:-(\d+))?$')
FILE_RE = re.compile(r'TapTap[-_]?(\d+\.\d+\.\d+)?(?:[-_+](\d+))?\.apk$', re.I)
ASSET = 'TapTap.apk'
SHA_IN_BODY = re.compile(r'SHA-256 of TapTap\.apk:\s*([0-9a-f]{64})')
MIN_ANDROID = 'Android 7.0 or newer'          # minSdk 24 (mobile/tool/configure_platforms.py)
DEFAULT_SERVER = 'taptapnetwork.com'          # the address the app starts with (mobile/lib/app/api.dart)

FEATURES = [
    ('bi-graph-up-arrow', 'Today, live', "The day's sales as they happen — pace against your usual day, sellers, plans. A new sale buzzes."),
    ('bi-ticket-perforated', 'Vouchers in your hand', 'Find any voucher, add time, reset devices, send it by WhatsApp — or make a batch on the spot.'),
    ('bi-chat-dots', 'Complaints & customers', 'Customers report from the Wi-Fi page; fix it, call them back or let Fit sort it out.'),
    ('bi-telephone', 'Chat & calls', 'Team room, voice and video calls — customer calls from your Wi-Fi page ring your phone.'),
    ('bi-robot', 'Fit, your AI', 'The same Fit as on the web, by voice: "why are sales down today?"'),
    ('bi-bell', 'Notifications', 'Sales, complaints, messages and missed calls pop up — even when the app is closed.'),
]


def _repo():
    return getattr(settings, 'APP_REPO', 'bfoon/taptap')


def human_size(n):
    n = int(n or 0)
    return f'{n / 1024 / 1024:.1f} MB' if n >= 1024 * 1024 else f'{max(1, n // 1024)} KB'


def _notes(body):
    """A release body → up to 6 short lines for "What's new" (markdown bullets and headings cleaned)."""
    out = []
    for line in str(body or '').splitlines():
        t = re.sub(r'^\s*(?:[-*+]|\d+[.)]|#+)\s*', '', line).strip()
        t = re.sub(r'[*_`]', '', t)
        if t and not t.lower().startswith(('sha256', 'sha-256', 'checksum', 'full changelog')):
            out.append(t[:160])
        if len(out) == 6:
            break
    return out


# ─────────────────────────────── where the APK is ───────────────────────────────

def _from_local():
    folder = getattr(settings, 'APP_LOCAL_DIR', '')
    if not folder or not os.path.isdir(folder):
        return None
    files = [f for f in glob.glob(os.path.join(folder, '*.apk')) if os.path.isfile(f)]
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    st = os.stat(path)
    key = f'app:sha:{path}:{st.st_mtime_ns}:{st.st_size}'
    sha = cache.get(key)
    if sha is None:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        sha = h.hexdigest()
        cache.set(key, sha, 24 * 3600)
    m = FILE_RE.search(os.path.basename(path))
    version = (m.group(1) if m and m.group(1) else '') or getattr(settings, 'APP_ANDROID_VERSION', '')
    return {'version': version, 'build': (m.group(2) if m else '') or '', 'size': st.st_size, 'sha256': sha,
            'published': timezone.datetime.fromtimestamp(st.st_mtime, tz=timezone.get_current_timezone()),
            'notes': [], 'url': '', 'path': path, 'source': 'server'}


def _from_setting():
    url = getattr(settings, 'APP_ANDROID_URL', '')
    if not url.lower().startswith(('https://', 'http://')):
        return None
    return {'version': getattr(settings, 'APP_ANDROID_VERSION', ''), 'build': '', 'size': 0, 'sha256': '',
            'published': None, 'notes': [], 'url': url, 'path': '', 'source': 'link'}


def _from_github():
    url = f'https://api.github.com/repos/{_repo()}/releases?per_page=15'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'TapTap-App'}
    token = getattr(settings, 'APP_GITHUB_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=4) as r:
            data = json.loads(r.read(3_000_000).decode('utf-8'))
    except Exception:
        logger.info('App releases not reachable', exc_info=True)
        return None
    best = None
    for rel in data if isinstance(data, list) else []:
        if not isinstance(rel, dict) or rel.get('draft'):
            continue
        m = TAG_RE.match(str(rel.get('tag_name') or ''))
        apk = next((a for a in rel.get('assets') or [] if isinstance(a, dict) and a.get('name') == ASSET), None)
        if not m or not apk or not str(apk.get('browser_download_url', '')).startswith('https://'):
            continue
        key = (tuple(int(x) for x in m.group(1).split('.')), int(m.group(2) or 0))
        if best is None or key > best[0]:
            digest = str(apk.get('digest') or '')
            best = (key, {
                'version': m.group(1), 'build': m.group(2) or '', 'size': int(apk.get('size') or 0),
                'sha256': (digest[7:] if digest.startswith('sha256:') and len(digest) == 71
                           else (SHA_IN_BODY.search(str(rel.get('body') or '')) or [None, ''])[1]),
                'published': parse_datetime(str(rel.get('published_at') or '')), 'notes': _notes(rel.get('body')),
                'url': apk['browser_download_url'], 'path': '', 'source': 'github'})
    return best[1] if best else None


def release(refresh=False):
    """The APK on offer, or None. Never raises."""
    local = _from_local()           # a file on this server always wins, and is never cached stale
    if local:
        return _finish(local)
    if not refresh:
        hit = cache.get(CACHE_KEY)
        if hit is not None:
            return hit or None
    rel = _from_setting()
    if rel is None and getattr(settings, 'APP_DISCOVER', True):
        rel = _from_github()
    cache.set(CACHE_KEY, _finish(rel) if rel else False, CACHE_OK if rel else CACHE_FAIL)
    return _finish(rel) if rel else None


def _finish(rel):
    rel = dict(rel)
    rel['size_text'] = human_size(rel['size']) if rel.get('size') else ''
    rel['label'] = rel['version'] + (f' (build {rel["build"]})' if rel.get('build') else '') if rel.get('version') else ''
    return rel


# ─────────────────────────────── downloads ───────────────────────────────

def _count(version):
    from .models_app_download import AppDownload
    today = timezone.localdate()
    version = (version or '')[:30]
    if not AppDownload.objects.filter(day=today, platform='android', version=version).update(count=F('count') + 1):
        obj, made = AppDownload.objects.get_or_create(day=today, platform='android', version=version, defaults={'count': 1})
        if not made:
            AppDownload.objects.filter(pk=obj.pk).update(count=F('count') + 1)


def total_downloads():
    v = cache.get('app:downloads')
    if v is None:
        from .models_app_download import AppDownload
        v = AppDownload.objects.aggregate(n=Sum('count'))['n'] or 0
        cache.set('app:downloads', v, 600)
    return v


def download_android(request):
    from .landing import BOTS
    rel = release()
    if not rel:
        raise Http404('The Android app is not available yet.')
    if request.method == 'GET' and not BOTS.search(request.META.get('HTTP_USER_AGENT', '')):
        try:
            _count(rel.get('label') or rel.get('version'))
        except Exception:
            logger.debug('app count', exc_info=True)
    if rel.get('path'):
        name = f'TapTap-{rel["version"]}.apk' if rel.get('version') else 'TapTap.apk'
        return FileResponse(open(rel['path'], 'rb'), as_attachment=True, filename=name,
                            content_type='application/vnd.android.package-archive')
    return HttpResponseRedirect(rel['url'])


# ─────────────────────────────── pages ───────────────────────────────

def _server(request):
    """The address to type in the app, when this TapTap is not the one the app starts with."""
    host = request.get_host().split(':')[0].lower()
    if host in (DEFAULT_SERVER, 'www.' + DEFAULT_SERVER):
        return ''
    return f'{request.scheme}://{request.get_host()}'


def ctx(request):
    """For the landing page section and /app/ (never fails the page)."""
    try:
        rel = release()
    except Exception:
        logger.exception('app release')
        rel = None
    try:
        downloads = total_downloads()
    except Exception:
        downloads = 0
    return {'release': rel, 'downloads': downloads, 'server': _server(request), 'min_android': MIN_ANDROID,
            'get_url': request.build_absolute_uri('/app/get/android/'), 'page_url': request.build_absolute_uri('/app/'),
            'features': FEATURES}


def page(request):
    c = ctx(request)
    stats = None
    if request.user.is_authenticated and request.user.is_superuser:
        from .models_app_download import AppDownload
        stats = list(AppDownload.objects.values('version').annotate(n=Sum('count')).order_by('-n')[:15])
    return render(request, 'core/app_download.html', {'app': c, 'stats': stats, 'year': timezone.localdate().year})


URLS = [
    ('app/', page, 'app_download'),
    ('app/get/android/', download_android, 'app_download_android'),
]


def install():
    """Public URLs: open to everybody, signed in or not, any plan."""
    from django.urls import path
    from . import middleware, permissions, team, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name in URLS:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
        permissions.MEMBER_ALLOWED.add(name)
        team.PLATFORM_OPEN.add(name)
        middleware.SubscriptionAccessMiddleware.ALLOWED.add(name)
