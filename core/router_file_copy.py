"""Save a file from a MikroTik to your computer — before Clean router memory removes it.

A browser cannot read a router's storage, so the router sends the file to TapTap and the browser downloads it:

1. TapTap creates a copy job (RouterCleanup action="copy") with a one-time upload token.
2. The router runs a short script — at once through a one-shot scheduler (Direct API / TapTap Tunnel) or at its next
   check-in (TapTap Link) — that reads the file in 16 KB pieces (/file read, RouterOS 7.13+) and posts them to
   /api/router-files/<job>/upload/ with the token. A refused read is retried smaller; the true length is reported
   when finished (the same self-correcting reader as server backups).
3. The page shows the progress; when the copy is complete the browser downloads it from TapTap.

Copies sit in private storage (never under /media) and are deleted after KEEP_HOURS.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models_router_cleanup import RouterCleanup

logger = logging.getLogger('taptap.cleanup')

CHUNK = 16384
MAX_BYTES = 64 * 1024 * 1024
KEEP_HOURS = 24
TOKEN_MINUTES = 60
SCHED_NAME = 'taptap-file-copy'


def _root():
    from .backup_server import PRIVATE_ROOT
    return PRIVATE_ROOT / 'router-files'


def _path(job):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', (job.params or {}).get('name', 'file'))[:120]
    return _root() / f'{job.router_id}' / f'{job.pk}-{safe}'


def received(job):
    p = _path(job)
    return p.stat().st_size if p.exists() else 0


def purge_old():
    """Delete copies older than KEEP_HOURS (files and their jobs' download)."""
    cutoff = timezone.now() - timedelta(hours=KEEP_HOURS)
    # (the "purged" flag is checked here, not in the query: a missing JSON key compares as NULL in SQL and would
    # silently skip every copy that was never marked)
    for job in RouterCleanup.objects.filter(action='copy', created_at__lt=cutoff).order_by('created_at')[:500]:
        if (job.result or {}).get('purged'):
            continue
        try:
            _path(job).unlink(missing_ok=True)
        except OSError:
            pass
        job.result = {**(job.result or {}), 'purged': True}
        job.save(update_fields=['result'])


def upload_url(job, request=None):
    from django.conf import settings
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    if not base and request is not None:
        base = request.build_absolute_uri('/').rstrip('/')
    if not base:
        raise ValueError('SITE_URL is required so the router knows where to send the file.')
    return f'{base}/api/router-files/{job.pk}/upload/'


def router_script(name, url, token, check):
    """RouterOS 7.13+: send one file to TapTap. Works inline (TapTap Link) and in a scheduler (Direct API)."""
    from .agent import rs
    post = ('/tool fetch url=$url http-method=post http-header-field=$h output=none check-certificate=$c idle-timeout=20s '
            'http-data=')
    return (f':local f {rs(name)}; :local url {rs(url)}; :local h ("X-TapTap-File: " . {rs(token)}); :local c "{check}"; '
            ':onerror err in={ '
            ':local i [/file find name=$f]; :if ([:len $i] = 0) do={ :error "the file is no longer on the router" }; '
            ':local z [/file get $i size]; :local o 0; '
            ':while ($o < $z) do={ '
            f':local n {CHUNK}; :if (($o + $n) > $z) do={{ :set n ($z - $o) }}; :local r ""; :local g 0; '
            ':while (($g = 0) and ($n > 0)) do={ '
            ':onerror e in={ :local rr [/file read file=$f offset=$o chunk-size=$n as-value]; :set r ($rr->"data"); '
            ':if ([:typeof $r] = "nil") do={ :set r (($rr->0)->"data") }; :set g [:len $r] } do={ :set g 0 }; '
            ':if ($g = 0) do={ :set n ($n / 2) } }; '
            ':if ($g = 0) do={ :if ($o = 0) do={ :error ("cannot read " . $f) }; :set z $o } else={ '
            + post + '("kind=chunk&offset=" . $o . "&total=" . $z . "&data=" . [:convert [:convert $r to=base64] to=url]); '
            ':set o ($o + $g) } }; '
            + post + '("kind=finish&total=" . $o) '
            '} do={ :do { ' + post + '("kind=error&message=" . [:convert $err to=url]) } on-error={} }')


def start(request, router, name, size):
    """Create a copy job and get the router sending. Returns the job."""
    from .agent import tls_flag
    from .backup_server import can_upload_to_server
    from .voucher_history import channel
    purge_old()
    if size and int(size) > MAX_BYTES:
        raise ValueError(f'{name} is larger than {MAX_BYTES // (1024 * 1024)} MB — copy it with WinBox instead.')
    version = ''
    try:
        version = (router.config_snapshot.ros_version if hasattr(router, 'config_snapshot') else '') or ''
    except Exception:
        version = ''
    if version and not can_upload_to_server(version):
        raise ValueError(f'{router.name} runs RouterOS {version}; saving files through TapTap needs RouterOS 7.13 or newer. '
                         f'Use WinBox › Files › Download instead.')
    token = secrets.token_urlsafe(32)
    via = channel(router)
    job = RouterCleanup.objects.create(business=router.business, router=router, action='copy', via=via, created_by=request.user,
                                       status='waiting',
                                       params={'name': name, 'size': int(size or 0),
                                               'token_sha': hashlib.sha256(token.encode()).hexdigest(),
                                               'expires': (timezone.now() + timedelta(minutes=TOKEN_MINUTES)).isoformat()})
    url = upload_url(job, request)
    check = tls_flag(url)
    if via == 'TapTap Link':
        from .linkops import send
        cmd = send(router, 'filecopy', {'job_id': job.pk, 'name': name, 'upload_url': url, 'upload_token': token},
                   label=f'Save {name} to the computer', user=request.user, minutes=TOKEN_MINUTES)
        job.command_id = cmd.pk
        job.save(update_fields=['command_id'])
        return job
    from .mikrotik import MikroTikService
    from .portctl import schedule_on_router
    svc = MikroTikService(router, timeout=30).connect()
    try:
        if check != 'no':
            from .backup_server import _ensure_trust_api
            _ensure_trust_api(svc)
        schedule_on_router(svc, SCHED_NAME, 5, router_script(name, url, token, check))
    finally:
        svc.close()
    return job


def link_body(cmd, url, check, nonce_value):
    p = cmd.params
    from .agent import tls_flag
    return '{ ' + router_script(p['name'], p['upload_url'], p['upload_token'], tls_flag(p['upload_url'])) + ' }'


def _param(data, key):
    v = data.get(key) or ['']
    return str(v[0])


@csrf_exempt
def upload(request, job_id):
    """The router's pieces of the file (urlencoded: kind=chunk|finish|error)."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    if len(request.body or b'') > 128 * 1024:
        return HttpResponse('too large', status=413)
    job = RouterCleanup.objects.filter(pk=job_id, action='copy').first()
    token = request.headers.get('X-TapTap-File', '')
    if not job or not token or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), (job.params or {}).get('token_sha', '')):
        return JsonResponse({'ok': False}, status=403)
    expires = (job.params or {}).get('expires')
    if job.status in ('done', 'failed') or (expires and datetime.fromisoformat(expires) < timezone.now()):
        return JsonResponse({'ok': False, 'message': 'This copy is closed.'}, status=410)
    try:
        data = parse_qs(request.body.decode('ascii'), keep_blank_values=True)
    except UnicodeDecodeError:
        return JsonResponse({'ok': False}, status=400)
    kind = _param(data, 'kind')
    path = _path(job)
    if kind == 'error':
        msg = _param(data, 'message')[:300] or 'The router could not send the file.'
        job.status, job.result, job.finished_at = 'failed', {'error': f'Router: {msg}'}, timezone.now()
        job.save(update_fields=['status', 'result', 'finished_at'])
        path.unlink(missing_ok=True)
        return JsonResponse({'ok': True})
    if kind == 'finish':
        try:
            total = int(_param(data, 'total'))
        except ValueError:
            return JsonResponse({'ok': False}, status=400)
        have = received(job)
        if total < 1 or have != total:
            job.status, job.result, job.finished_at = 'failed', {'error': f'Incomplete copy: TapTap has {have} of {total} bytes.'}, timezone.now()
            job.save(update_fields=['status', 'result', 'finished_at'])
            return JsonResponse({'ok': False}, status=409)
        h = hashlib.sha256()
        with path.open('rb') as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b''):
                h.update(block)
        job.status, job.finished_at = 'done', timezone.now()
        job.result = {'bytes': have, 'sha256': h.hexdigest()}
        job.save(update_fields=['status', 'result', 'finished_at'])
        return JsonResponse({'ok': True, 'status': 'ready'})
    if kind != 'chunk':
        return JsonResponse({'ok': False}, status=400)
    try:
        offset, total = int(_param(data, 'offset')), int(_param(data, 'total'))
        raw = _param(data, 'data').replace(' ', '+').replace('-', '+').replace('_', '/')
        chunk = base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True)
    except Exception:
        return JsonResponse({'ok': False, 'message': 'Invalid piece.'}, status=400)
    if offset < 0 or total < 1 or total > MAX_BYTES or len(chunk) > CHUNK + 1024 or offset + len(chunk) > total:
        return JsonResponse({'ok': False, 'message': 'Invalid size.'}, status=400)
    path.parent.mkdir(parents=True, exist_ok=True)
    have = received(job)
    if offset > have:
        return JsonResponse({'ok': False, 'message': f'Expected offset {have}.'}, status=409)
    if offset < have:                                    # a repeat of a piece TapTap already has
        with path.open('rb') as fh:
            fh.seek(offset)
            if fh.read(len(chunk)) != chunk:
                return JsonResponse({'ok': False, 'message': 'Repeat does not match.'}, status=409)
        return JsonResponse({'ok': True, 'have': have})
    with path.open('ab') as fh:
        fh.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())
    if job.status == 'waiting':
        RouterCleanup.objects.filter(pk=job.pk).update(status='running', params={**job.params, 'total': total})
    return JsonResponse({'ok': True, 'have': have + len(chunk)})
