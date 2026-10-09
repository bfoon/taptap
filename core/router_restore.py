"""Restore a router's configuration — from your computer, from the TapTap server, or from a backup already on it.

The journey of a restore:

1. **Choose** — a .backup/.rsc uploaded from the computer (kept privately on TapTap), a backup TapTap stored on its
   server, or a file already in the router's storage.
2. **Send** (computer/server only) — the router downloads the file from TapTap with a one-time code
   (``/tool fetch … dst-path=``) and checks the size against what TapTap holds.
3. **Restore** — optionally a safety copy first (``taptap-before-restore.backup`` on the router), then:
     * .backup → ``/system backup load`` (everything comes back, users and passwords included; the router reboots)
     * .rsc    → ``/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset=<file>`` (an export
                 cannot be imported over a running configuration; it does NOT carry user passwords or certificates)
4. **Back online** — the router reboots; TapTap watches for it to come back (TapTap Link heartbeat or the API) by
   comparing its boot time with the moment the restore started.

Direct API / TapTap Tunnel: the script runs from a one-shot scheduler (it has full rights). TapTap Link: the script
runs at the next check-in; loading a backup needs rights Link does not have, so a small helper pasted once
(``taptap-restore-local``, "Allow restores") does the final step. A restore command is never re-sent.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models_router_restore import RouterRestore

logger = logging.getLogger('taptap.restore')

MAX_BYTES = 64 * 1024 * 1024
TOKEN_MINUTES = 60
BACK_ONLINE_MINUTES = 12
HELPER = 'taptap-restore-local'
HELPER_POLICY = 'ftp,reboot,read,write,policy,test,password,sensitive'
SAFETY_NAME = 'taptap-before-restore'
SCHED = 'taptap-restore'
NAME_RE = re.compile(r'^[A-Za-z0-9._()+@-]{1,180}\.(backup|rsc)$')


# ─────────────────────────────── helpers ───────────────────────────────

def _root():
    from .backup_server import PRIVATE_ROOT
    return PRIVATE_ROOT / 'restore-uploads'


def kind_of(name):
    return 'rsc' if str(name).lower().endswith('.rsc') else 'backup'


def safe_name(name):
    base = Path(str(name)).name
    base = re.sub(r'[^A-Za-z0-9._()+@-]', '-', base)[:170]
    if not base.lower().endswith(('.backup', '.rsc')):
        base += '.backup'
    return base


def event(r, text, level='info'):
    r.events = (r.events or []) + [{'at': timezone.now().isoformat(), 'text': text[:300], 'level': level}]


def _server_file(r):
    """Path of the file TapTap sends to the router (upload or server backup)."""
    if r.source == 'upload':
        return Path(r.upload_path) if r.upload_path else None
    if r.source == 'server' and r.backup_id:
        from .backup_server import _full_path, _kind_info
        try:
            storage = r.backup.server_storage
        except Exception:
            return None
        rel = getattr(storage, _kind_info(r.backup, 'export' if r.kind == 'rsc' else 'backup')['path_field'])
        return _full_path(rel) if rel else None
    return None


def parse_uptime(text):
    """RouterOS uptime '1w2d3h4m5s' → seconds."""
    total = 0
    for n, unit in re.findall(r'(\d+)([wdhms])', str(text or '')):
        total += int(n) * {'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
    return total


def warnings_for(router, name, kind, version='', size=0, free=None):
    out = []
    stem = Path(name).stem.lower()
    rname = re.sub(r'[^a-z0-9]', '', router.name.lower())
    if stem.startswith('taptap-') and rname and rname not in re.sub(r'[^a-z0-9]', '', stem):
        out.append(('warn', f'This backup seems to come from another router. A binary backup restores that router’s MAC addresses, '
                            f'identity and passwords onto {router.name} — only do this when replacing identical hardware.'))
    if kind == 'rsc':
        out.append(('warn', 'An export (.rsc) is applied after a full reset: user passwords, certificates and files it refers to are not '
                            'in an export. Have WinBox access (MAC address) ready in case login changes. A .backup restores everything.'))
    if free is not None and size and free < size + 1_500_000:
        out.append(('bad', f'The router has only {free // 1024} KB free — not enough room for this {size // 1024} KB file. '
                           f'Use Clean router memory first.'))
    out.append(('info', f'{router.name} restarts during the restore and is offline for about 1–3 minutes. Customers are disconnected '
                        f'and reconnect by themselves.'))
    return out


# ─────────────────────────────── the router-side script ───────────────────────────────

def router_script(r, *, file_url, report_url, token, check, link, password=''):
    """One script: (download + check) → report → (safety copy) → restore. Errors are reported back to TapTap."""
    from .agent import rs
    say = ('$say rp=$rp h=$h c=$c ')
    s = (f':local f {rs(r.file_name)}; :local rp {rs(report_url)}; :local h ("X-TapTap-Restore: " . {rs(token)}); :local c "{check}"; '
         ':local say do={ :do { /tool fetch url=$rp http-method=post http-header-field=$h output=none check-certificate=$c idle-timeout=15s '
         'http-data=("kind=" . $k . "&message=" . [:convert $m to=url]) } on-error={} }; '
         ':onerror err in={ ')
    if r.source != 'router':
        s += (':do { /file remove [find name=$f] } on-error={}; '
              + say + 'k="sending" m=""; '
              f'/tool fetch url={rs(file_url)} http-header-field=$h dst-path=$f check-certificate=$c idle-timeout=30s; :delay 2s; ')
    s += (':local i [/file find name=$f]; :if ([:len $i] = 0) do={ :error "the file is not on the router" }; '
          ':local z [/file get $i size]; ')
    if r.size:
        s += f':if ($z != {int(r.size)}) do={{ :error ("the file is incomplete on the router: " . $z . " of {int(r.size)} bytes") }}; '
    s += say + 'k="ready" m=$z; '
    if link:
        s += (f':if ([:len [/system script find name={HELPER}]] = 0) do={{ :error "needs-restore-helper" }}; '
              f':global ttRsFile $f; :global ttRsKind "{r.kind}"; :global ttRsSafe "{"yes" if r.safety_copy else "no"}"; '
              f':global ttRsPass {rs(password)}; '
              + say + 'k="loading" m=""; :delay 2s; '
              f'/system script run {HELPER}; ')
    else:
        if r.safety_copy:
            s += f':do {{ /system backup save name={SAFETY_NAME} dont-encrypt=yes }} on-error={{}}; '
        s += say + 'k="loading" m=""; :delay 2s; '
        if r.kind == 'rsc':
            s += '/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset=$f; '
        else:
            s += f'/system backup load name=$f password={rs(password)}; '
    s += '} do={ ' + say + 'k="error" m=$err }'
    return s


HELPER_SOURCE = (':global ttRsFile; :global ttRsKind; :global ttRsSafe; :global ttRsPass; '
                 f':if ($ttRsSafe = "yes") do={{ :do {{ /system backup save name={SAFETY_NAME} dont-encrypt=yes }} on-error={{}} }}; '
                 ':if ($ttRsKind = "rsc") do={ /system reset-configuration no-defaults=yes skip-backup=yes run-after-reset=$ttRsFile } '
                 'else={ /system backup load name=$ttRsFile password=$ttRsPass }')


def helper_install_command():
    from .agent import rs
    return ('# TapTap: allow restores through TapTap Link (one small script that only loads a backup you chose in TapTap)\n'
            f'/system script remove [find where name="{HELPER}"]\n'
            f'/system script add name="{HELPER}" policy={HELPER_POLICY} dont-require-permissions=yes '
            f'comment="TapTap restore helper" source={rs(HELPER_SOURCE)}\n'
            ':put "TapTap restores allowed on this router"')


def link_body(cmd, url, check, nonce_value):
    from .agent import tls_flag
    r = RouterRestore.objects.select_related('router').get(pk=cmd.params['restore_id'])
    p = cmd.params
    return '{ ' + router_script(r, file_url=p['file_url'], report_url=p['report_url'], token=p['token'], check=tls_flag(p['report_url']),
                                link=True, password=p.get('password', '')) + ' }'


# ─────────────────────────────── start ───────────────────────────────

def _urls(r, request=None):
    from django.conf import settings
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    if not base and request is not None:
        base = request.build_absolute_uri('/').rstrip('/')
    if not base:
        raise ValueError('SITE_URL is required so the router knows where to download the file.')
    return f'{base}/api/router-restore/{r.pk}/file/', f'{base}/api/router-restore/{r.pk}/report/'


def start(r, request, password=''):
    """Send the restore to the router. Returns r (status 'sending' or 'failed')."""
    from .agent import tls_flag
    from .voucher_history import channel
    token = secrets.token_urlsafe(32)
    r.token_sha = hashlib.sha256(token.encode()).hexdigest()
    r.token_expires = timezone.now() + timedelta(minutes=TOKEN_MINUTES)
    r.via, r.status, r.started_at = channel(r.router), 'sending', timezone.now()
    file_url, report_url = _urls(r, request)
    check = tls_flag(report_url)
    event(r, {'upload': 'Sending your file to the router', 'server': 'Sending the TapTap server copy to the router',
              'router': 'Using the file already on the router'}[r.source])
    r.save()
    if r.via == 'TapTap Link':
        from .linkops import send
        cmd = send(r.router, 'restore', {'restore_id': r.pk, 'file_url': file_url, 'report_url': report_url, 'token': token,
                                         'password': password}, label=f'Restore {r.file_name}', user=request.user, minutes=TOKEN_MINUTES)
        r.command_id = cmd.pk
        r.save(update_fields=['command_id'])
        return r
    from .mikrotik import MikroTikService
    from .portctl import schedule_on_router
    svc = MikroTikService(r.router, timeout=30).connect()
    try:
        if check != 'no':
            from .backup_server import _ensure_trust_api
            _ensure_trust_api(svc)
        schedule_on_router(svc, SCHED, 5, router_script(r, file_url=file_url, report_url=report_url, token=token, check=check,
                                                         link=False, password=password))
    finally:
        svc.close()
    return r


# ─────────────────────────────── router-facing endpoints ───────────────────────────────

def _router_auth(request, pk):
    r = RouterRestore.objects.select_related('router').filter(pk=pk).first()
    token = request.headers.get('X-TapTap-Restore', '')
    if not r or not token or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), r.token_sha or ''):
        return None
    if r.token_expires and r.token_expires < timezone.now():
        return None
    return r


@csrf_exempt
def router_file(request, pk):
    """The router downloads the chosen file (computer upload or server copy)."""
    r = _router_auth(request, pk)
    if not r:
        return HttpResponse(status=403)
    if r.status not in ('sending', 'checking'):
        return HttpResponse(status=410)
    path = _server_file(r)
    if not path or not path.is_file():
        return HttpResponse(status=404)
    return FileResponse(path.open('rb'), as_attachment=True, filename=r.file_name, content_type='application/octet-stream')


@csrf_exempt
def router_report(request, pk):
    """The router's progress: sending / ready / loading / error."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    r = _router_auth(request, pk)
    if not r:
        return HttpResponse(status=403)
    try:
        data = parse_qs(request.body.decode('ascii', 'replace'), keep_blank_values=True)
    except Exception:
        data = {}
    kind = (data.get('kind') or [''])[0]
    msg = (data.get('message') or [''])[0][:300]
    if r.status in ('done', 'failed'):
        return JsonResponse({'ok': True})
    if kind == 'sending':
        event(r, 'The router is downloading the file')
    elif kind == 'ready':
        r.status = 'checking'
        event(r, f'The file is on the router and complete ({msg} bytes)', 'ok')
    elif kind == 'loading':
        r.status, r.loading_at = 'restoring', timezone.now()
        event(r, ('Safety copy saved as ' + SAFETY_NAME + '.backup · ' if r.safety_copy else '') + 'Restoring — the router restarts now', 'ok')
        if r.command_id:                                  # the router reboots before it could confirm: never re-send
            from .models import AgentCommand
            AgentCommand.objects.filter(pk=r.command_id).update(status='done', result='Restore started', done_at=timezone.now())
    elif kind == 'error':
        r.status, r.finished_at = 'failed', timezone.now()
        if r.command_id:
            from .models import AgentCommand
            AgentCommand.objects.filter(pk=r.command_id, status__in=('queued', 'sent')).update(status='failed', result='Restore failed',
                                                                                               done_at=timezone.now())
        if 'needs-restore-helper' in msg:
            r.error = 'This router needs a one-time permission before TapTap Link can restore: paste the “Allow restores” command.'
        elif 'no such item' in msg.lower():
            r.error = f'The router could not find {r.file_name}.'
        else:
            r.error = f'Router: {msg}'
        event(r, r.error, 'bad')
    r.save()
    if kind in ('loading', 'error') and r.command_id:       # the backup password is not kept once the router has it
        from .models import AgentCommand
        cmd = AgentCommand.objects.filter(pk=r.command_id).first()
        if cmd and (cmd.params or {}).get('password'):
            cmd.params = {**cmd.params, 'password': ''}
            cmd.save(update_fields=['params'])
    return JsonResponse({'ok': True})


# ─────────────────────────────── is it back? ───────────────────────────────

def check_back(r):
    """After 'loading': the router is back when its boot time is after the restore started."""
    if r.status not in ('restoring', 'rebooting') or not r.loading_at:
        return r
    now = timezone.now()
    if r.status == 'restoring' and (now - r.loading_at).total_seconds() > 8:
        r.status = 'rebooting'
        event(r, 'The router is restarting with the restored configuration')
        r.save(update_fields=['status', 'events'])
    booted = None
    if r.via == 'TapTap Link':
        from .link_system_health import _cache_key
        data = cache.get(_cache_key(r.router_id)) or {}
        at, up = data.get('at'), parse_uptime((data.get('resource') or {}).get('uptime'))
        if at and up:
            booted = at - timedelta(seconds=up)
    else:
        key = f'tt:restore:probe:{r.pk}'
        if not cache.get(key):
            cache.set(key, 1, 8)
            try:
                from .mikrotik import MikroTikService
                svc = MikroTikService(r.router, timeout=4).connect()
                try:
                    res = (svc.safe_get('/system/resource') or [{}])[0]
                finally:
                    svc.close()
                booted = now - timedelta(seconds=parse_uptime(res.get('uptime')))
            except Exception:
                booted = None
    if booted and booted > r.loading_at - timedelta(seconds=20):
        r.status, r.finished_at = 'done', now
        event(r, f'{r.router.name} is back online with the restored configuration', 'ok')
        r.save(update_fields=['status', 'finished_at', 'events'])
    elif (now - r.loading_at).total_seconds() > BACK_ONLINE_MINUTES * 60:
        r.status, r.finished_at = 'failed', now
        r.error = (f'{r.router.name} has not come back after {BACK_ONLINE_MINUTES} minutes. Check its power and cables; reach it with '
                   f'WinBox by MAC address. {"The safety copy " + SAFETY_NAME + ".backup is on the router." if r.safety_copy else ""}')
        event(r, r.error, 'bad')
        r.save(update_fields=['status', 'finished_at', 'error', 'events'])
    return r


def expire_sending(r):
    """Sending that never started (router offline, Link command expired) fails with a reason."""
    if r.status not in ('sending', 'checking') or not r.started_at:
        return r
    if r.token_expires and r.token_expires < timezone.now():
        r.status, r.finished_at = 'failed', timezone.now()
        r.error = 'The router did not finish within an hour. Nothing was changed on it.'
        event(r, r.error, 'bad')
        r.save()
    elif r.command_id:
        from .models import AgentCommand
        cmd = AgentCommand.objects.filter(pk=r.command_id).first()
        if cmd and cmd.status in ('failed', 'expired', 'cancelled'):
            r.status, r.finished_at = 'failed', timezone.now()
            r.error = 'TapTap Link could not deliver the restore. Nothing was changed on the router.'
            event(r, r.error, 'bad')
            r.save()
    return r


# ─────────────────────────────── page endpoints ───────────────────────────────

def _router(request, pk):
    return get_object_or_404(request.user.business.routers, pk=pk)


def as_json(r):
    return {'id': r.pk, 'source': r.source, 'kind': r.kind, 'file_name': r.file_name, 'original_name': r.original_name, 'size': r.size,
            'status': r.status, 'status_label': r.get_status_display(), 'error': r.error, 'events': r.events or [], 'via': r.via,
            'safety_copy': r.safety_copy, 'loading_at': r.loading_at.isoformat() if r.loading_at else None,
            'elapsed': int((timezone.now() - r.started_at).total_seconds()) if r.started_at else 0}


def _free(router):
    try:
        from .link_system_health import _cache_key
        from .router_cleanup import _int
        data = cache.get(_cache_key(router.pk)) or {}
        v = (data.get('resource') or {}).get('free-hdd-space')
        return _int(v) if v not in (None, '') else None
    except Exception:
        return None


@login_required
@require_POST
def restore_upload(request, pk):
    """A .backup/.rsc from the computer → kept privately until the router downloads it."""
    router = _router(request, pk)
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'ok': False, 'message': 'Choose a .backup or .rsc file.'}, status=400)
    name = safe_name(f.name)
    if not name.lower().endswith(('.backup', '.rsc')) or not f.name.lower().endswith(('.backup', '.rsc')):
        return JsonResponse({'ok': False, 'message': 'Only RouterOS .backup or .rsc files can be restored.'}, status=400)
    if f.size > MAX_BYTES or f.size < 1:
        return JsonResponse({'ok': False, 'message': f'The file must be between 1 byte and {MAX_BYTES // (1024 * 1024)} MB.'}, status=400)
    head = f.read(512)
    f.seek(0)
    if name.lower().endswith('.rsc') and b'\x00' in head:
        return JsonResponse({'ok': False, 'message': 'That .rsc is not a text export.'}, status=400)
    r = RouterRestore.objects.create(business=router.business, router=router, source='upload', kind=kind_of(name),
                                     file_name=name, original_name=f.name[:200], size=f.size, created_by=request.user)
    path = _root() / str(router.pk) / f'{r.pk}-{name}'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as out:
        for chunk in f.chunks():
            out.write(chunk)
    r.upload_path = str(path)
    r.save(update_fields=['upload_path'])
    return JsonResponse({'ok': True, 'restore': as_json(r), 'warnings': warnings_for(router, name, r.kind, size=r.size, free=_free(router))})


@login_required
@require_POST
def restore_prepare(request, pk):
    """Server copy or a file on the router → a draft restore with its warnings."""
    import json
    router = _router(request, pk)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    source = data.get('source')
    if source == 'server':
        from .models import RouterBackup
        b = RouterBackup.objects.filter(router__business=router.business, pk=str(data.get('backup') or 0)).select_related('router').first()
        kind = 'rsc' if data.get('kind') == 'rsc' else 'backup'
        try:
            st = b.server_storage if b else None
        except Exception:
            st = None
        if not b or not st or st.status != 'ready':
            return JsonResponse({'ok': False, 'message': 'That backup is not stored on the TapTap server.'}, status=400)
        size = st.backup_size if kind == 'backup' else st.server_export_size
        name = f'{b.name}.{"rsc" if kind == "rsc" else "backup"}'
        r = RouterRestore.objects.create(business=router.business, router=router, source='server', kind=kind, file_name=safe_name(name),
                                         original_name=name, size=size or 0, backup=b, created_by=request.user)
        warn = warnings_for(router, name, kind, size=size or 0, free=_free(router))
        if b.router_id != router.pk:
            warn.insert(0, ('warn', f'This backup was made on {b.router.name}, not {router.name}.'))
    elif source == 'router':
        name = str(data.get('name') or '')
        if not NAME_RE.match(name):
            return JsonResponse({'ok': False, 'message': 'Choose a .backup or .rsc file from the router.'}, status=400)
        r = RouterRestore.objects.create(business=router.business, router=router, source='router', kind=kind_of(name), file_name=name,
                                         original_name=name, size=int(data.get('size') or 0), created_by=request.user)
        warn = warnings_for(router, name, r.kind)
    else:
        return JsonResponse({'ok': False, 'message': 'Choose where the backup comes from.'}, status=400)
    return JsonResponse({'ok': True, 'restore': as_json(r), 'warnings': warn})


@login_required
@require_POST
def restore_start(request, pk, rid):
    import json
    router = _router(request, pk)
    r = get_object_or_404(RouterRestore, router=router, pk=rid)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    if r.status != 'draft':
        return JsonResponse({'ok': False, 'message': 'This restore was already started.'}, status=400)
    if str(data.get('confirm') or '').strip().lower() != router.name.strip().lower():
        return JsonResponse({'ok': False, 'message': f'Type the router name “{router.name}” to confirm.'}, status=400)
    if RouterRestore.objects.filter(router=router, status__in=('sending', 'checking', 'restoring', 'rebooting')).exclude(pk=r.pk).exists():
        return JsonResponse({'ok': False, 'message': 'Another restore is running on this router.'}, status=400)
    free = _free(router)
    if r.source != 'router' and free is not None and r.size and free < r.size + 1_500_000:
        return JsonResponse({'ok': False, 'message': f'{router.name} has only {free // 1024} KB free — use Clean router memory first.'}, status=400)
    r.safety_copy = bool(data.get('safety', True))
    password = str(data.get('password') or '')[:64]
    if re.search(r'["\\$\n\r]', password):
        return JsonResponse({'ok': False, 'message': 'The backup password cannot contain quotes, $ or backslashes.'}, status=400)
    try:
        start(r, request, password=password)
    except Exception as exc:  # noqa: BLE001
        logger.info('restore %s failed to start: %s', r.pk, exc)
        r.status, r.error, r.finished_at = 'failed', f'Could not reach {router.name}: {exc}'[:500], timezone.now()
        event(r, r.error, 'bad')
        r.save()
    from .utils import log
    log(router.business, 'Router Restore', f'{router.name}: restore of {r.file_name} from {r.get_source_display()} started')
    return JsonResponse({'ok': True, 'restore': as_json(r)})


@login_required
@require_GET
def restore_status(request, pk, rid):
    router = _router(request, pk)
    r = get_object_or_404(RouterRestore, router=router, pk=rid)
    expire_sending(r)
    check_back(r)
    return JsonResponse({'ok': True, 'restore': as_json(r)})


@login_required
@require_GET
def restore_helper(request, pk):
    _router(request, pk)
    return JsonResponse({'ok': True, 'command': helper_install_command()})
