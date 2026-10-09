"""Clean router memory: find what is taking space on a MikroTik and remove only what is safe.

1. **Scan** — free/total storage and RAM, the memory log, and every file (except the hotspot login pages, skins and
   other folders TapTap never touches: those are only counted).
2. **Plan** — files are sorted into groups, each with a safety level:
     * safe      old TapTap backups already stored on the TapTap server, support/crash files (*.rif), old disk logs,
                 TapTap's own leftovers (certificate import files), temporary files; selected by default
     * check     other backups/exports, the only local copy of a TapTap backup, upgrade packages (*.npk — removing one
                 cancels an upgrade waiting for a reboot); never selected by default
     * kept      everything else (certificates, hotspot pages, unknown files) — listed, never removable here
   The memory log can be cleared too (frees a little RAM; the router keeps logging).
3. **Clean** — only names from the latest scan that fell in a removable group; Link commands carry at most what
   fits one command and the page runs the next batch until everything chosen is gone. Free space is measured again
   at the end, so "space saved" is what the router really reports.

Direct API / TapTap Tunnel run at once; TapTap Link runs at the next check-in and uploads its answer to
/api/agent/v1/cleanup (signed with the command's one-time code).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from django.utils import timezone

PROTECTED_DIRS = re.compile(r'^(flash/)?(hotspot|skins|user-manager[0-9]*|pub|dude|container|certs?)(/|$)', re.I)
MAX_FILES = 400
LINK_NAMES_BUDGET = 1800          # characters of file names one Link command may carry
API_BATCH = 200

GROUPS = {
    'taptap_backups': {'label': 'Old TapTap backups', 'icon': 'bi-cloud-check', 'why': 'Already stored safely on the TapTap server.'},
    'taptap_local': {'label': 'TapTap backups only on the router', 'icon': 'bi-archive', 'why': 'No server copy — keep the newest unless you have another copy.'},
    'support': {'label': 'Support & crash files', 'icon': 'bi-bug', 'why': 'autosupout / supout files RouterOS writes after a crash. Only MikroTik support reads them.'},
    'logs': {'label': 'Old disk logs', 'icon': 'bi-journal-text', 'why': 'Log files written to storage. The memory log keeps the recent lines.'},
    'leftovers': {'label': 'TapTap leftovers & temp files', 'icon': 'bi-stars', 'why': 'Files TapTap or RouterOS used once (certificate imports, .tmp, .old).'},
    'other_backups': {'label': 'Other backups & exports', 'icon': 'bi-file-earmark-zip', 'why': 'Made by hand or by another tool. Remove only if you have a copy elsewhere.'},
    'packages': {'label': 'Upgrade packages', 'icon': 'bi-box-seam', 'why': 'A .npk file installs at the next reboot. Removing it cancels that upgrade.'},
    'kept': {'label': 'Kept', 'icon': 'bi-shield-lock', 'why': 'Certificates, login pages and files TapTap does not recognise are never removed here.'},
}
SAFE_GROUPS = ('taptap_backups', 'support', 'logs', 'leftovers')
CHECK_GROUPS = ('taptap_local', 'other_backups', 'packages')
CLEANABLE = SAFE_GROUPS + CHECK_GROUPS
NAME_OK = re.compile(r'^[A-Za-z0-9 ._()+@,=:/-]{1,200}$')


# ─────────────────────────────── parsing helpers ───────────────────────────────

def _int(v):
    try:
        return int(float(str(v or '0').strip()))
    except (TypeError, ValueError):
        return 0


_MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}


def parse_time(text):
    """RouterOS file times: '2026-10-07 01:09:04' (v7) or 'oct/07/2026 01:09:04' (v6) → aware datetime or None."""
    t = str(text or '').strip()
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})', t)
    try:
        if m:
            dt = datetime(*map(int, m.groups()))
        else:
            m = re.match(r'([a-z]{3})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})', t, re.I)
            if not m:
                return None
            mo, d, y, hh, mm, ss = m.groups()
            dt = datetime(int(y), _MONTHS[mo.lower()], int(d), int(hh), int(mm), int(ss))
    except (ValueError, KeyError):
        return None
    return timezone.make_aware(dt)


def _name_time(name):
    """TapTap backup names carry their date: taptap-<router>-YYYYMMDD-HHMMSS."""
    m = re.search(r'(\d{8})-(\d{6})', name)
    if not m:
        return None
    try:
        return timezone.make_aware(datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S'))
    except ValueError:
        return None


# ─────────────────────────────── classify ───────────────────────────────

def classify(router, scan, now=None):
    """Scan result → plan: groups with files, safety, default selection and the totals the page animates."""
    from .models import RouterBackup
    now = now or timezone.now()
    files = scan.get('files') or []
    server_copies = set(RouterBackup.objects.filter(router=router, server_storage__status='ready').values_list('name', flat=True)) \
        if hasattr(RouterBackup, 'server_storage') else set()
    if not server_copies:
        try:
            from .models_backup_storage import RouterBackupStorage
            server_copies = set(RouterBackupStorage.objects.filter(backup__router=router, status='ready').values_list('backup__name', flat=True))
        except Exception:
            server_copies = set()
    groups = {k: [] for k in GROUPS}
    local_taptap = []
    for f in files:
        name = str(f.get('name') or '')
        low = name.lower()
        base = low.rsplit('/', 1)[-1]
        size = _int(f.get('size'))
        when = parse_time(f.get('time')) or _name_time(name)
        age_days = (now - when).days if when else None
        item = {'name': name, 'size': size, 'age': age_days, 'time': when.isoformat() if when else ''}
        stem = re.sub(r'\.(backup|rsc)$', '', name.rsplit('/', 1)[-1])
        if PROTECTED_DIRS.match(name) or not NAME_OK.match(name):
            groups['kept'].append(item)
        elif base.endswith(('.rif',)) or base.startswith(('autosupout', 'supout')):
            groups['support'].append(item)
        elif base.startswith('taptap-') and base.endswith(('.backup', '.rsc')):
            if stem in server_copies:
                groups['taptap_backups'].append(item)
            else:
                local_taptap.append(item)
        elif base.endswith(('.backup', '.rsc')):
            groups['other_backups'].append(item)
        elif base.endswith('.npk'):
            groups['packages'].append(item)
        elif re.match(r'^(log|disk-?log|logs?)[\w.-]*\.(txt|log)$', base) or base.endswith('.log'):
            (groups['logs'] if (age_days is None or age_days >= 1) else groups['kept']).append(item)
        elif base.startswith('taptap-ca-') or base.endswith(('.tmp', '.old', '.core')) or base.startswith(('crash', 'core.')):
            groups['leftovers'].append(item)
        else:
            groups['kept'].append(item)
    # TapTap backups with no server copy: the newest pair stays "kept"; older ones can go (check first).
    if local_taptap:
        newest_stem = max(local_taptap, key=lambda i: i['time'] or '')['name'].rsplit('.', 1)[0]
        for item in local_taptap:
            (groups['kept'] if item['name'].rsplit('.', 1)[0] == newest_stem else groups['taptap_local']).append(item)
    out = []
    for key, meta in GROUPS.items():
        items = sorted(groups[key], key=lambda i: -i['size'])
        if not items and key != 'kept':
            continue
        level = 'safe' if key in SAFE_GROUPS else ('check' if key in CHECK_GROUPS else 'kept')
        for i in items:
            i['selected'] = level == 'safe'
        out.append({'key': key, 'level': level, 'label': meta['label'], 'icon': meta['icon'], 'why': meta['why'],
                    'files': items[:150], 'count': len(items), 'bytes': sum(i['size'] for i in items)})
    order = {'safe': 0, 'check': 1, 'kept': 2}
    out.sort(key=lambda g: (order[g['level']], -g['bytes']))
    free, total = _int(scan.get('free_hdd')), _int(scan.get('total_hdd'))
    safe_bytes = sum(g['bytes'] for g in out if g['level'] == 'safe')
    log_lines = _int(scan.get('log_lines'))
    return {
        'storage': {'free': free, 'total': total, 'used': max(0, total - free),
                    'used_pct': round((total - free) * 100 / total, 1) if total else None},
        'ram': {'free': _int(scan.get('free_mem')), 'total': _int(scan.get('total_mem'))},
        'board': scan.get('board', ''), 'version': scan.get('version', ''),
        'log': {'lines': log_lines, 'memory_lines': _int(scan.get('memory_lines')), 'suggest': log_lines >= 300},
        'groups': out, 'safe_bytes': safe_bytes,
        'protected': {'count': _int(scan.get('protected_count')), 'bytes': _int(scan.get('protected_bytes'))},
        'file_count': len(files) + _int(scan.get('protected_count')),
        'truncated': bool(scan.get('truncated')),
    }


def cleanable_names(plan):
    return {f['name'] for g in plan.get('groups', []) if g['level'] in ('safe', 'check') for f in g['files']}


# ─────────────────────────────── direct API / tunnel ───────────────────────────────

def _resource(svc):
    rows = svc.safe_get('/system/resource')
    return rows[0] if rows else {}


def api_scan(svc):
    r = _resource(svc)
    scan = {'free_hdd': r.get('free-hdd-space'), 'total_hdd': r.get('total-hdd-space'), 'free_mem': r.get('free-memory'),
            'total_mem': r.get('total-memory'), 'version': str(r.get('version') or ''), 'board': str(r.get('board-name') or '')}
    try:
        scan['log_lines'] = len(svc.safe_get('/log'))
    except Exception:
        scan['log_lines'] = 0
    mem = [a for a in svc.safe_get('/system/logging/action') if str(a.get('name')) == 'memory']
    scan['memory_lines'] = mem[0].get('memory-lines') if mem else 0
    files, pcount, pbytes = [], 0, 0
    for f in svc.safe_get('/file'):
        name, ftype = str(f.get('name') or ''), str(f.get('type') or '')
        if ftype == 'directory':
            continue
        if PROTECTED_DIRS.match(name):
            pcount += 1
            pbytes += _int(f.get('size'))
            continue
        if len(files) >= MAX_FILES:
            scan['truncated'] = True
            continue
        files.append({'name': name, 'size': f.get('size'), 'type': ftype,
                      'time': f.get('last-modified') or f.get('creation-time') or ''})
    scan.update(files=files, protected_count=pcount, protected_bytes=pbytes)
    return scan


def api_clean(svc, names, clear_log):
    removed, failed, freed = [], [], 0
    by_name = {str(f.get('name')): f for f in svc.safe_get('/file')}
    res = svc.resource('/file')
    for name in names[:API_BATCH]:
        row = by_name.get(name)
        if not row:
            removed.append(name)                         # already gone: nothing to do
            continue
        try:
            res.remove(id=row.get('id') or row.get('.id'))
            removed.append(name)
            freed += _int(row.get('size'))
        except Exception:
            failed.append(name)
    log_cleared = None
    if clear_log:
        log_cleared = False
        try:
            act = svc.resource('/system/logging/action')
            mem = [a for a in act.get() if str(a.get('name')) == 'memory']
            if mem:
                aid, lines = mem[0].get('id') or mem[0].get('.id'), str(mem[0].get('memory-lines') or '1000')
                act.set(id=aid, **{'memory-lines': '1'})
                act.set(id=aid, **{'memory-lines': lines})
                log_cleared = True
        except Exception:
            log_cleared = False
    r = _resource(svc)
    return {'removed_count': len(removed), 'failed_count': len(failed), 'freed': freed, 'log_cleared': log_cleared,
            'free_hdd': _int(r.get('free-hdd-space')), 'total_hdd': _int(r.get('total-hdd-space')),
            'free_mem': _int(r.get('free-memory')), 'left': names[API_BATCH:]}


# ─────────────────────────────── TapTap Link ───────────────────────────────

def link_scan_body(rs):
    prot = '^(flash/)?(hotspot|skins|user-manager[0-9]*|pub|dude|container|certs?)/'      # no $: RouterOS strings
    return (':local r [/system resource get]; '
            ':set out ("fd=" . ($r->"free-hdd-space") . "\\ntd=" . ($r->"total-hdd-space") . "\\nfm=" . ($r->"free-memory") . '
            '"\\ntm=" . ($r->"total-memory") . "\\nver=" . ($r->"version") . "\\nbd=" . ($r->"board-name") . "\\n"); '
            ':do { :set out ($out . "ll=" . [:len [/log find]] . "\\n") } on-error={}; '
            ':do { :set out ($out . "ml=" . [/system logging action get [find name=memory] memory-lines] . "\\n") } on-error={}; '
            f':local pc 0; :local pb 0; :local n 0; :local tr 0; '
            ':foreach f in=[/file find] do={ :do { :local nm [/file get $f name]; :local ty [/file get $f type]; '
            ':if ($ty != "directory") do={ :local sz [/file get $f size]; '
            f':if ($nm ~ {rs(prot)}) do={{ :set pc ($pc + 1); :set pb ($pb + $sz) }} else={{ '
            f':if ($n < {MAX_FILES}) do={{ :local ct ""; :do {{ :set ct [/file get $f last-modified] }} on-error={{ '
            ':do { :set ct [/file get $f creation-time] } on-error={} }; '
            ':set out ($out . "f=" . $nm . "|" . $sz . "|" . $ty . "|" . $ct . "\\n"); :set n ($n + 1) } else={ :set tr 1 } } } } on-error={} }; '
            ':set out ($out . "pc=" . $pc . "\\npb=" . $pb . "\\ntr=" . $tr . "\\n"); ')


def link_clean_body(params, rs):
    names = params.get('files') or []
    arr = '{' + ';'.join(rs(n) for n in names) + '}' if names else '{}'
    body = (':local ok 0; :local bad 0; :local fr 0; '
            f':foreach nm in={arr} do={{ :do {{ :local id [/file find name=$nm]; '
            ':if ([:len $id] > 0) do={ :local sz [/file get $id size]; /file remove $id; :set fr ($fr + $sz) }; :set ok ($ok + 1) } '
            'on-error={ :set bad ($bad + 1) } }; ')
    if params.get('clear_log'):
        body += (':do { :local a [/system logging action find name=memory]; :local ml [/system logging action get $a memory-lines]; '
                 '/system logging action set $a memory-lines=1; :delay 1s; /system logging action set $a memory-lines=$ml; '
                 ':set out ($out . "log=1\\n") } on-error={ :set out ($out . "log=0\\n") }; ')
    body += (':delay 1s; :local r [/system resource get]; '
             ':set out ($out . "ok=" . $ok . "\\nbad=" . $bad . "\\nfr=" . $fr . "\\nfd=" . ($r->"free-hdd-space") . '
             '"\\ntd=" . ($r->"total-hdd-space") . "\\nfm=" . ($r->"free-memory") . "\\n"); ')
    return body


def link_body(cmd, url, check, nonce_value):
    from .agent import rs
    p = cmd.params
    upload = rs(f'{url}/api/agent/v1/cleanup?c={cmd.pk}&n={nonce_value}')
    inner = link_scan_body(rs) if p.get('action') == 'scan' else link_clean_body(p, rs)
    send = (f'/tool fetch url={upload} http-method=post http-header-field="Content-Type: text/plain" '
            f'http-data=$out output=none check-certificate={check} duration=20s idle-timeout=15s')
    return '{ :local out ""; ' + inner + send + ' }'


def link_batches(names):
    """Split chosen names so each Link command stays small."""
    batches, cur, size = [], [], 0
    for n in names:
        if cur and size + len(n) + 3 > LINK_NAMES_BUDGET:
            batches.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += len(n) + 3
    if cur:
        batches.append(cur)
    return batches


def parse_link(action, body, params=None):
    lines = []
    for line in (body or b'').decode('utf-8', 'replace').splitlines()[:MAX_FILES + 40]:
        k, _, v = line.partition('=')
        lines.append((k.strip(), v.strip()))
    get = lambda k: next((v for kk, v in lines if kk == k), '')
    if action == 'scan':
        files = []
        for k, v in lines:
            if k == 'f':
                parts = v.rsplit('|', 3)
                if len(parts) == 4:
                    files.append({'name': parts[0], 'size': parts[1], 'type': parts[2], 'time': parts[3]})
        return {'free_hdd': get('fd'), 'total_hdd': get('td'), 'free_mem': get('fm'), 'total_mem': get('tm'), 'version': get('ver'),
                'board': get('bd'), 'log_lines': get('ll'), 'memory_lines': get('ml'), 'files': files,
                'protected_count': get('pc'), 'protected_bytes': get('pb'), 'truncated': get('tr') == '1'}
    return {'removed_count': _int(get('ok')), 'failed_count': _int(get('bad')),
            'freed': _int(get('fr')), 'log_cleared': (get('log') == '1') if get('log') else None,
            'free_hdd': _int(get('fd')), 'total_hdd': _int(get('td')), 'free_mem': _int(get('fm')), 'left': (params or {}).get('left', [])}


def receive_link(cmd, body):
    from .models_router_cleanup import RouterCleanup
    job = RouterCleanup.objects.filter(pk=cmd.params.get('job_id'), router=cmd.router).first()
    if not job:
        return None
    raw = parse_link(job.action, body, job.params)
    finish(job, raw)
    return job


def finish(job, raw):
    """Store a scan (with its plan) or a clean result on the job."""
    if job.action == 'scan':
        plan = classify(job.router, raw)
        job.result = {'plan': plan}
    else:
        before = (job.params or {}).get('free_before')
        raw['saved'] = max(0, raw.get('free_hdd', 0) - before) if before else raw.get('freed', 0)
        job.result = raw
    job.status, job.finished_at = 'done', timezone.now()
    job.save(update_fields=['result', 'status', 'finished_at'])
    return job


def expire_waiting(job):
    if job.status != 'waiting':
        return job
    from .models import AgentCommand
    cmd = AgentCommand.objects.filter(pk=job.command_id).first() if job.command_id else None
    reason = ''
    if not cmd:
        reason = 'The command to the router was lost.'
    elif cmd.status in ('failed', 'expired', 'cancelled'):
        reason = f'The router did not run it ({cmd.get_status_display().lower()}).'
    elif cmd.status == 'done' and cmd.done_at and (timezone.now() - cmd.done_at).total_seconds() > 90:
        reason = 'The router ran it but its answer never reached TapTap.'
    elif (timezone.now() - job.created_at).total_seconds() > 420:
        reason = 'The router did not answer within 7 minutes — is TapTap Link online?'
    if reason:
        job.status, job.result, job.finished_at = 'failed', {'error': reason}, timezone.now()
        job.save(update_fields=['status', 'result', 'finished_at'])
    return job
