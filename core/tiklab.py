"""TikLab — the free MikroTik / RouterOS simulator — offered for download from TapTap's public pages.

Visitors never land on GitHub: every button points to /tiklab/get/<file>, which counts the download and sends
the file itself (a direct file download, the browser saves it). Three places the file can come from, first match:

1. ``TIKLAB_LOCAL_DIR`` — a folder on this server holding the files (served by Django as an attachment);
2. ``TIKLAB_DOWNLOAD_BASE`` — your own CDN / bucket, ``<base>/<file name>``;
3. the GitHub repository ``TIKLAB_REPO`` (default bfoon/mikrotik_simulation), branch ``TIKLAB_BRANCH``, through
   raw.githubusercontent.com, which answers with the file itself (application/octet-stream), not a repo page.

New versions appear by themselves: the repository's file list is read from the GitHub API (cached for hours; on
any failure the built-in list below is used), and files named like the existing ones are picked up —
``TikLab-<x.y.z>-<windows-x64|linux-x64|linux-arm64|source>.<zip|tar.gz>`` and ``TikLab-<x.y>-User-Manual.pdf``.
Upload TikLab 1.5 to the repository and the landing page offers it, with no TapTap release.

Short links that always give the newest file: /tiklab/get/latest/windows (linux-x64, linux-arm64, source, manual).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from datetime import date
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Sum
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone

logger = logging.getLogger('taptap.tiklab')

CACHE_KEY = 'tiklab:catalog:v1'
CACHE_OK = 6 * 3600        # a good file list is kept six hours
CACHE_FAIL = 15 * 60       # after a failure, the built-in list is used for 15 minutes before trying again

PLATFORMS = [
    # key, label, for whom, icon, how it unpacks
    ('windows', 'Windows', 'Windows 10 and 11, 64-bit', 'bi-windows', 'zip'),
    ('linux-x64', 'Linux', '64-bit PCs and servers', 'bi-ubuntu', 'tar.gz'),
    ('linux-arm64', 'Raspberry Pi', 'Pi 4 and 5, other ARM boards', 'bi-cpu', 'tar.gz'),
    ('source', 'Source code', 'to build TikLab yourself', 'bi-code-slash', 'zip'),
]
PLATFORM_KEYS = {p[0] for p in PLATFORMS}
_SUFFIX = {'windows': 'windows-x64'}

PKG_RE = re.compile(r'^TikLab-(\d+)\.(\d+)\.(\d+)-(windows-x64|linux-x64|linux-arm64|source)\.(zip|tar\.gz)$')
MANUAL_RE = re.compile(r'^TikLab-(\d+)\.(\d+)(?:\.(\d+))?-User-Manual\.pdf$')

# The files in the repository today, with their SHA-256 (so visitors can check what they got).
BUILT_IN = {
    'TikLab-1.4.0-windows-x64.zip': (4784928, '34fb7f59e1c6f66c662180f25081159a81dc8685c807f6000e3e2eb79603e9b5'),
    'TikLab-1.4.0-linux-x64.tar.gz': (4699125, 'd101e81cf945d95ee47928fa42acbf78e5f51551a0399661d414d1be612d7deb'),
    'TikLab-1.4.0-linux-arm64.tar.gz': (4294483, '10f940c3ef8d36938f1a5d6f520645923e872b25f9a367084abfeb86e52fbb3f'),
    'TikLab-1.4.0-source.zip': (764039, '1b43405c796c38ff06b47328c33b06add5df554a7417a6fab2068326c40727e7'),
    'TikLab-1.4-User-Manual.pdf': (6021739, '319c1b9e548664c1df238f93f62f6dd5762507e53709e7e75b830b10bede7f36'),
    'TikLab-1.3.0-windows-x64.zip': (4665093, '4f0d7cbca46ff08737593b6eef0b946e410401d819baf145eeb629917553c1fd'),
    'TikLab-1.3.0-linux-x64.tar.gz': (4575821, '89c0736f4f118f01b3209b223137af80849f0b3f28f5c5b202d287f3125494fb'),
    'TikLab-1.3.0-linux-arm64.tar.gz': (4184779, '83d246fe82983c74aca93b5d156942389b620c590032013d2de9dfcea2c5a0e9'),
    'TikLab-1.3.0-source.zip': (719637, '5fa081f897dd200ba1c633712fdf82a3d890c51e8f537dcfaf07de2a2e1e6109'),
    'TikLab-1.2.0-windows-x64.zip': (4118366, 'f7125833124c0cad3859fe4b768bb04187329336d30b4de69eb6254f163c76f8'),
    'TikLab-1.2.0-linux-x64.tar.gz': (4033135, '69fca19db1a292907b6ea9baa3d8f0cdd8a522ef7d2199184251fc2f2bdc937f'),
    'TikLab-1.2.0-linux-arm64.tar.gz': (3693072, '56282414e1a41ad73cd65469542813326c55269ed44f469df83d5813960571a6'),
    'TikLab-1.2.0-source.zip': (599670, 'f7a7fc7c97427d80cdd21e179050aa7354413846aeb5ffd1252119297dafbb88'),
}

# What the 1.4 manual covers (34 pages) — shown as its table of contents.
MANUAL_CHAPTERS = [
    'Welcome to TikLab', 'Installing and starting', 'The console at a glance', 'Simulating a router',
    'Hotspot and vouchers', 'Connecting apps and TapTap', 'Wi-Fi on this PC', 'The Network Lab',
    'Moving configurations', 'Files and hotspot pages', 'Scripts and WireGuard', 'Reference', 'Troubleshooting',
    'Limitations and licences',
]


def _repo():
    return getattr(settings, 'TIKLAB_REPO', 'bfoon/mikrotik_simulation')


def _branch():
    return getattr(settings, 'TIKLAB_BRANCH', 'main')


def human_size(n):
    n = int(n or 0)
    if n >= 1024 * 1024:
        return f'{n / 1024 / 1024:.1f} MB'
    if n >= 1024:
        return f'{n // 1024} KB'
    return f'{n} B'


# ─────────────────────────────── the file list ───────────────────────────────

def _fetch_repo_files():
    """{name: size} of the files at the top of the repository, from the GitHub API; None on any failure."""
    url = f'https://api.github.com/repos/{_repo()}/contents?ref={quote(_branch())}'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'TapTap-TikLab'}
    token = getattr(settings, 'TIKLAB_GITHUB_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=4) as r:
            data = json.loads(r.read(2_000_000).decode('utf-8'))
    except Exception:
        logger.info('TikLab file list not reachable; using the built-in list', exc_info=True)
        return None
    if not isinstance(data, list):
        return None
    return {str(x.get('name')): int(x.get('size') or 0) for x in data
            if isinstance(x, dict) and x.get('type') == 'file' and x.get('name')}


def build(files):
    """Turn {name: size} into the catalog: releases (newest first), manuals, and every known file by name."""
    releases, manuals, known = {}, [], {}
    for name, size in files.items():
        m = PKG_RE.match(name)
        if m:
            ver = tuple(int(x) for x in m.group(1, 2, 3))
            plat = 'windows' if m.group(4) == 'windows-x64' else m.group(4)
            sha = BUILT_IN.get(name, (None, ''))
            item = {'name': name, 'size': size, 'size_text': human_size(size), 'platform': plat,
                    'version': '.'.join(map(str, ver)), 'sha256': sha[1] if sha[0] == size else ''}
            releases.setdefault(ver, {})[plat] = item
            known[name] = item
            continue
        m = MANUAL_RE.match(name)
        if m:
            ver = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
            sha = BUILT_IN.get(name, (None, ''))
            item = {'name': name, 'size': size, 'size_text': human_size(size), 'platform': 'manual',
                    'version': f'{ver[0]}.{ver[1]}', 'ver': ver, 'sha256': sha[1] if sha[0] == size else ''}
            manuals.append(item)
            known[name] = item
    manuals.sort(key=lambda x: x['ver'], reverse=True)
    out = []
    for ver in sorted(releases, reverse=True):
        # the newest manual written for this version or an earlier one
        manual = next((m for m in manuals if m['ver'][:2] <= ver[:2]), None)
        out.append({'version': '.'.join(map(str, ver)), 'files': releases[ver], 'manual': manual,
                    'windows': releases[ver].get('windows'), 'linux_x64': releases[ver].get('linux-x64'),
                    'linux_arm64': releases[ver].get('linux-arm64'),
                    'platforms': [dict(zip(('key', 'label', 'who', 'icon', 'kind'), p), file=releases[ver].get(p[0]))
                                  for p in PLATFORMS if releases[ver].get(p[0])]})
    return {'releases': out, 'manuals': manuals, 'known': known}


def catalog(refresh=False):
    c = None if refresh else cache.get(CACHE_KEY)
    if c is not None:
        return c
    files = None
    if getattr(settings, 'TIKLAB_DISCOVER', True):
        files = _fetch_repo_files()
    live = bool(files) and any(PKG_RE.match(n) for n in files)
    if not live:
        files = {n: s for n, (s, _) in BUILT_IN.items()}
    c = build(files)
    c['live'] = live
    cache.set(CACHE_KEY, c, CACHE_OK if live else CACHE_FAIL)
    return c


def latest(c=None):
    c = c or catalog()
    return c['releases'][0] if c['releases'] else None


# ─────────────────────────────── downloads ───────────────────────────────

def source_url(name):
    base = (getattr(settings, 'TIKLAB_DOWNLOAD_BASE', '') or '').rstrip('/')
    if base:
        return f'{base}/{quote(name)}'
    return f'https://raw.githubusercontent.com/{_repo()}/{quote(_branch())}/{quote(name)}'


def _count(name):
    from .models_tiklab import TikLabDownload
    today = timezone.localdate()
    n = TikLabDownload.objects.filter(day=today, file=name).update(count=F('count') + 1)
    if not n:
        obj, made = TikLabDownload.objects.get_or_create(day=today, file=name, defaults={'count': 1})
        if not made:
            TikLabDownload.objects.filter(pk=obj.pk).update(count=F('count') + 1)


def total_downloads():
    v = cache.get('tiklab:downloads')
    if v is None:
        from .models_tiklab import TikLabDownload
        v = TikLabDownload.objects.aggregate(n=Sum('count'))['n'] or 0
        cache.set('tiklab:downloads', v, 600)
    return v


def download(request, name):
    from .landing import BOTS
    c = catalog()
    if name not in c['known']:
        c = catalog(refresh=True)            # a version uploaded a moment ago
        if name not in c['known']:
            raise Http404('No such TikLab file.')
    if not BOTS.search(request.META.get('HTTP_USER_AGENT', '')) and request.method == 'GET':
        try:
            _count(name)
        except Exception:
            logger.debug('tiklab count', exc_info=True)
    local = getattr(settings, 'TIKLAB_LOCAL_DIR', '')
    if local:
        path = os.path.join(local, os.path.basename(name))
        if os.path.isfile(path):
            ctype = 'application/pdf' if name.endswith('.pdf') else 'application/octet-stream'
            return FileResponse(open(path, 'rb'), as_attachment=True, filename=name, content_type=ctype)
    return HttpResponseRedirect(source_url(name))


def download_latest(request, platform):
    c = catalog()
    rel = latest(c)
    if platform == 'manual':
        item = (rel or {}).get('manual') or (c['manuals'][0] if c['manuals'] else None)
    else:
        item = rel['files'].get(platform) if rel and platform in PLATFORM_KEYS else None
    if not item:
        raise Http404('No such TikLab download.')
    return download(request, item['name'])


# ─────────────────────────────── pages ───────────────────────────────

def picker(rel):
    """JSON for the smart download button: one entry per system (static/tiklab/tiklab.js picks the visitor's)."""
    from django.urls import reverse
    out = {}
    for p in (rel or {}).get('platforms', []):
        out[p['key']] = {'href': reverse('tiklab_download', args=[p['file']['name']]), 'label': p['label'],
                         'size': p['file']['size_text'], 'icon': p['icon'], 'who': p['who']}
    return json.dumps(out)


def landing_ctx():
    """What the landing page's TikLab section needs (never fails the landing page)."""
    try:
        c = catalog()
        rel = latest(c)
        if not rel:
            return None
        return {'release': rel, 'manual': rel['manual'], 'downloads': total_downloads(), 'files_json': picker(rel)}
    except Exception:
        logger.exception('tiklab landing')
        return None


def page(request):
    c = catalog()
    rel = latest(c)
    stats = None
    if request.user.is_authenticated and request.user.is_superuser:
        from .models_tiklab import TikLabDownload
        stats = list(TikLabDownload.objects.values('file').annotate(n=Sum('count')).order_by('-n')[:20])
    return render(request, 'core/tiklab.html', {
        'release': rel, 'older': c['releases'][1:], 'manual': rel['manual'] if rel else None, 'chapters': MANUAL_CHAPTERS,
        'downloads': total_downloads(), 'stats': stats, 'live': c.get('live'), 'year': date.today().year,
        'page_url': request.build_absolute_uri('/tiklab/'), 'files_json': picker(rel),
    })


URLS = [
    ('tiklab/', page, 'tiklab'),
    ('tiklab/get/latest/<slug:platform>/', download_latest, 'tiklab_latest'),
    ('tiklab/get/<str:name>', download, 'tiklab_download'),
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
