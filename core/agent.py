"""TapTap Link — manage MikroTik routers that sit behind NAT or a firewall.

The router never accepts connections from TapTap. Instead a small RouterOS script,
run by the router's scheduler every few seconds, calls TapTap over HTTPS:

    router ──HTTPS POST /api/agent/v1/poll──▶ TapTap      (heartbeat + live sessions)
    router ◀──────── RouterOS script ───────── TapTap      (queued commands, if any)
    router ──HTTPS GET  /api/agent/v1/ack───▶ TapTap      (per-command result)

Security model
* One random token per router (only its SHA-256 is stored; shown once at setup).
* HTTPS with certificate checking; the token travels in an Authorization header.
* TapTap only sends scripts built from a fixed catalogue of safe commands; free-form
  scripts are refused unless the owner switches them on for that router.
* Every command has an expiry (a queued reboot never fires hours later), is sent at
  most twice, and is acknowledged with a per-command HMAC nonce — never the token.
* Optional IP pinning, instant revoke, token rotation, rate limiting, audit log.
* The router-side script runs with ftp, read, write, test, reboot and sensitive rights only —
  never policy/password/winbox/ssh/api, so it cannot change users or management access.
"""
import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import AgentCommand, Router, RouterAgent, RouterBackup, Voucher
from .utils import log

logger = logging.getLogger('taptap.link')
MAX_SCRIPT = 3600
DEFAULT_EXPIRY = {'reboot': 5, 'port_restart': 5, 'interface_set': 15, 'port_off_for': 15, 'disconnect': 10}
POLICY = 'ftp,read,write,test,reboot,sensitive'
SAFE_KINDS = {
    'ping', 'interface_set', 'port_restart', 'port_off_for', 'hotspot_users',
    'hotspot_user_set', 'hotspot_user_remove', 'disconnect', 'binding_set',
    'binding_remove', 'limit', 'unlimit', 'reboot', 'backup', 'inventory_piece', 'self_update',
}
ACK_WAIT = {'inventory_piece': 600, 'backup': 300, 'hotspot_users': 300, 'self_update': 300}   # seconds before a resend
NAME_RE = re.compile(r'^[\w.@:+/<>-]{1,64}$')
MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(router):
    """Create (or rotate) the router token. The plain token is shown only once."""
    token = 'ttl_' + secrets.token_urlsafe(32)
    agent, _ = RouterAgent.objects.update_or_create(
        router=router,
        defaults={'token_hash': _hash(token), 'token_hint': token[:10], 'revoked': False},
    )
    return token, agent


def agent_for_token(token):
    if not token or not token.startswith('ttl_') or len(token) > 80:
        return None
    return RouterAgent.objects.select_related('router__business').filter(token_hash=_hash(token), revoked=False).first()


def base_url(request=None):
    url = getattr(settings, 'SITE_URL', '') or (request.build_absolute_uri('/').rstrip('/') if request else '')
    return url.rstrip('/')


def nonce(cmd):
    return hmac.new(settings.SECRET_KEY.encode(), f'agent:{cmd.pk}:{cmd.router_id}'.encode(), hashlib.sha256).hexdigest()[:24]


def tls_flag(url):
    if not url.startswith('https://'):
        return 'no'
    return 'yes-without-crl' if getattr(settings, 'AGENT_VERIFY_TLS', True) else 'no'


def rs(value):
    """Quote a value as a RouterOS string literal."""
    v = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\r', '').replace('\n', '\\n')
    return f'"{v}"'


SCRIPT_VERSION = 2   # bump when the router-side heartbeat changes; routers upgrade themselves


def agent_script(url, token, check):
    """The RouterOS heartbeat installed as ``taptap-link`` (runs every few seconds).

    v2: the heartbeat only reports and fetches. Commands TapTap sends back run in a
    separate background job (:execute), so a long command (inventory upload, backup)
    never delays or blocks the next check-in. The overlap guard counts real running
    jobs, so it cannot get stuck (the v1 global flag could, until a reboot).
    """
    return f''':if ([:len [/system script job find where script="taptap-link"]] > 1) do={{
  :log warning "TapTap Link: previous check-in still running, skipping"
}} else={{
:do {{
  :local url {rs(url + "/api/agent/v1")}
  :local tok {rs(token)}
  :local ver [/system resource get version]
  :local up [/system resource get uptime]
  :local cpu [/system resource get cpu-load]
  :local mf [/system resource get free-memory]
  :local mt [/system resource get total-memory]
  :local board [/system resource get board-name]
  :local a ""
  :local n 0
  :foreach s in=[/ip hotspot active find] do={{
    :if ($n < 300) do={{
      :local e [/ip hotspot active get $s]
      :set a ($a . ($e->"user") . "," . ($e->"mac-address") . "," . ($e->"address") . "," . ($e->"uptime") . "," . ($e->"bytes-in") . "," . ($e->"bytes-out") . "," . ($e->".id") . ";")
      :set n ($n + 1)
    }}
  }}
  :local body ("id=" . [/system identity get name] . "&ver=" . $ver . "&up=" . $up . "&cpu=" . $cpu . "&mf=" . $mf . "&mt=" . $mt . "&board=" . $board . "&n=" . [:len [/ip hotspot active find]] . "&sv={SCRIPT_VERSION}&act=" . $a)
  :local res [/tool fetch url=($url . "/poll") http-method=post http-data=$body http-header-field=("Authorization: Bearer " . $tok) output=user as-value check-certificate={check} duration=8s idle-timeout=5s]
  :if (($res->"status") = "finished") do={{
    :local cmd ($res->"data")
    :if ([:len $cmd] > 0) do={{ :execute script=$cmd }}
  }}
}} on-error={{
  :log warning "TapTap Link: cannot reach TapTap"
}}
}}
'''


def self_update_body(url, check):
    """Replace the router's heartbeat with the current version, keeping its own token.

    TapTap stores only a hash of the token, so the router reads the token out of its
    installed script and splices it into the new one.
    """
    marker = 'TAPTAP_TOKEN_MARKER'
    new = agent_script(url, marker, check)
    pre, post = new.split(marker, 1)
    quote = rs('"')
    return (':global taptapLinkBusy; :set taptapLinkBusy false; '
            ':local sid [/system script find where name="taptap-link"]; '
            ':local src [/system script get $sid source]; '
            ':local p [:find $src "ttl_"]; '
            f':local q [:find $src {quote} $p]; '
            ':local tok [:pick $src $p $q]; '
            ':if ([:len $tok] < 20) do={ :error "TapTap Link: token not found in installed script" }; '
            f'/system script set $sid source=({rs(pre)} . $tok . {rs(post)}); '
            ':log info "TapTap Link: heartbeat updated"')


def enrollment_script(router, token, request=None):
    url = base_url(request)
    check = tls_flag(url)
    try:
        agent = router.agent
    except RouterAgent.DoesNotExist:
        agent = None
    secs = max(10, int(agent.poll_seconds if agent else 10))
    body = agent_script(url, token, check)
    name = re.sub(r'[^\x20-\x7e]', '?', router.name)
    return f'''# --- TapTap Link for "{name}" ---
# Paste the whole block into WinBox > New Terminal.
# The router connects OUT to {url}; no port-forwarding or public IP is required.
# Keep the token private. Rotate it from TapTap if it is ever exposed.
/system scheduler disable [find where name="taptap-link"]
/system script job remove [find where trace~"scheduler:taptap-link"]
/system scheduler remove [find where name="taptap-link"]
/system script remove [find where name="taptap-link"]
:global taptapLinkBusy
:set taptapLinkBusy false
/system script add name="taptap-link" policy={POLICY} comment="TapTap Link - do not edit" source={rs(body)}
/system scheduler add name="taptap-link" interval={secs}s start-time=startup policy={POLICY} on-event="/system script run taptap-link" comment="TapTap Link"
/system script run taptap-link
:log info "TapTap Link installed"
'''


def advanced_enrollment_script(router, token, request=None):
    """Recovery installer containing the checks that solved the real RouterOS setup."""
    url = base_url(request)
    install = enrollment_script(router, token, request)
    return f'''# ================================================================
# TapTap Link ADVANCED / RECOVERY setup for {router.name}
# Use this block when the quick install does not check in.
# It safely stops old TapTap jobs, checks HTTPS, then reinstalls Link.
# ================================================================

# 1) Stop an old scheduler and clear only stuck TapTap Link jobs.
/system scheduler disable [find where name="taptap-link"]
/system script job remove [find where trace~"scheduler:taptap-link"]
:global taptapLinkBusy
:set taptapLinkBusy false

# 2) Show RouterOS, clock and certificate information.
#    HTTPS fails if the router clock is wrong: make sure the date below is today.
/system resource print
/system clock print
:do {{ /system ntp client set enabled=yes servers=pool.ntp.org }} on-error={{ :do {{ /system ntp client set enabled=yes server-dns-names=pool.ntp.org }} on-error={{}} }}
:do {{ /certificate settings print }} on-error={{}}

# 2b) DNS: the router must resolve TapTap's name.
:do {{ :put ("TapTap resolves to " . [:resolve {rs(url.split('://')[-1].split('/')[0].split(':')[0])}]) }} on-error={{ :put "DNS FAILED - set a DNS server: /ip dns set servers=1.1.1.1,8.8.8.8" }}

# 3) Verify that this MikroTik can reach TapTap with certificate validation.
#    Expected: status=finished and code=200.
:do {{
  :put ([/tool fetch url={rs(url + "/api/agent/v1/hello")} output=user as-value check-certificate=yes-without-crl duration=8s idle-timeout=5s]->"data")
  :log info "TapTap Link HTTPS test passed"
}} on-error={{
  :log warning "TapTap Link HTTPS test failed. If the log says no trusted CA, update RouterOS stable and retry."
}}

# If RouterOS is old and reports 'no trusted CA certificate found', run these manually:
# /system package update check-for-updates
# /system package update install
# After the reboot: /system routerboard upgrade
# Then reboot once more and repeat the HTTPS test above.

# 4) Clean install the current TapTap Link script.
{install}

# 5) Useful diagnostics after installation.
/system scheduler print detail where name="taptap-link"
/system script job print
/log print where message~"TapTap Link"
'''


def _ack(url, cmd, status, check, result=''):
    q = f'{url}/api/agent/v1/ack?c={cmd.pk}&n={nonce(cmd)}&s={status}' + (f'&r={quote(result)}' if result else '')
    return f'/tool fetch url={rs(q)} output=none check-certificate={check} duration=8s idle-timeout=5s'


def command_body(cmd):
    """RouterOS commands for one queued command (catalogue only)."""
    p, k = cmd.params or {}, cmd.kind
    name = lambda key='name': rs(p[key])
    if k == 'ping':
        return ':log info "TapTap Link: test from TapTap"'
    if k == 'interface_set':
        return f'/interface set [find name={name()}] disabled={"no" if p.get("enabled") else "yes"}'
    if k == 'port_restart':
        secs = max(1, min(30, int(p.get('seconds', 5))))
        return f'/interface disable [find name={name()}]; :delay {secs}s; /interface enable [find name={name()}]'
    if k == 'port_off_for':
        mins = max(1, min(10080, int(p.get('minutes', 15))))
        sched = f'taptap-on-{p["name"]}'
        ev = f'/interface enable [find name="{p["name"]}"]; /system scheduler remove [find name="{sched}"]'
        return (f'/system scheduler remove [find name={rs(sched)}]; /system scheduler add name={rs(sched)} interval={mins}m '
                f'policy={POLICY} on-event={rs(ev)} comment="TapTap automatic restore"; /interface disable [find name={name()}]')
    if k == 'hotspot_users':
        lines = []
        for prof in p.get('profiles', []):
            lines.append(f':if ([:len [/ip hotspot user profile find name={rs(prof["name"])}]] = 0) do={{ /ip hotspot user profile add name={rs(prof["name"])} '
                         f'shared-users={int(prof.get("shared") or 1)}' + (f' rate-limit={rs(prof["rate"])}' if prof.get('rate') else '') + ' }')
        for u in p.get('users', []):
            extra = (f' limit-uptime={rs(u["lim"])}' if u.get('lim') else '') + f' disabled={"yes" if u.get("dis") else "no"}'
            lines.append(f':if ([:len [/ip hotspot user find name={rs(u["n"])}]] = 0) do={{ /ip hotspot user add name={rs(u["n"])} password={rs(u["n"])} '
                         f'profile={rs(u["prof"])} comment={rs(u.get("c", "TapTap voucher"))}{extra} }} else={{ /ip hotspot user set [find name={rs(u["n"])}] '
                         f'profile={rs(u["prof"])}{extra} }}')
        return '; '.join(lines) or ':nothing'
    if k == 'hotspot_user_set':
        return f'/ip hotspot user set [find name={name()}] disabled={"yes" if p.get("disabled") else "no"}'
    if k == 'hotspot_user_remove':
        return f'/ip hotspot user remove [find name={name()}]'
    if k == 'disconnect':
        return f'/ip hotspot active remove [find user={name("user")}]'
    if k == 'binding_set':
        return f'/ip hotspot ip-binding set [find mac-address={name("mac")}] disabled={"no" if p.get("enabled") else "yes"}'
    if k == 'binding_remove':
        return f'/ip hotspot ip-binding remove [find mac-address={name("mac")}]'
    if k == 'limit':
        q = rs(f'TapTap limit {p["name"]}')
        lim = rs(f'{int(float(p.get("up", 0)) * 1000)}k/{int(float(p.get("down", 0)) * 1000)}k')
        return (f':if ([:len [/queue simple find name={q}]] = 0) do={{ /queue simple add name={q} target={name()} max-limit={lim} comment="Managed by TapTap" }} '
                f'else={{ /queue simple set [find name={q}] target={name()} max-limit={lim} }}')
    if k == 'unlimit':
        return f'/queue simple remove [find name={rs("TapTap limit " + p["name"])}]'
    if k == 'backup':
        b = rs(p['file'])
        return f':do {{ /system backup save name={b} dont-encrypt=yes }} on-error={{ /system backup save name={b} }}; /export file={b}'
    if k == 'script':
        return str(p.get('source', ''))
    raise ValueError(f'Unknown command {k}')


def wrap(cmd, url, check):
    """A command plus its acknowledgement, isolated so one failure never stops the rest."""
    if cmd.kind == 'reboot':
        return f':do {{ {_ack(url, cmd, "ok", check)}; :delay 2s; /system reboot }} on-error={{}}'
    if cmd.kind == 'inventory_piece':
        from .agent_inventory import inventory_piece_script
        body = inventory_piece_script(cmd, url, check, nonce(cmd))
    elif cmd.kind == 'self_update':
        body = self_update_body(url, check)
    else:
        body = command_body(cmd)
    result = f'{cmd.params.get("file")}.backup, {cmd.params.get("file")}.rsc' if cmd.kind == 'backup' else ''
    return (f':do {{ {body}; {_ack(url, cmd, "ok", check, result)} }} '
            f'on-error={{ :do {{ {_ack(url, cmd, "fail", check)} }} on-error={{}} }}')


def queue(router, kind, params=None, label='', user=None, minutes=None):
    if kind not in SAFE_KINDS and kind != 'script':
        raise ValueError('Not an allowed command.')
    if kind == 'script':
        try:
            agent = router.agent
        except RouterAgent.DoesNotExist:
            agent = None
        if not agent or not agent.allow_scripts:
            raise ValueError('Custom scripts are switched off for this router (TapTap Link settings).')
        if len((params or {}).get('source', '')) > 2000:
            raise ValueError('Script is too long (2000 characters max).')
    for key in ('name', 'user'):
        if params and key in params and not NAME_RE.match(str(params[key])):
            raise ValueError(f'Invalid {key}.')
    if params and 'mac' in params and not MAC_RE.match(str(params['mac'])):
        raise ValueError('Invalid MAC address.')
    mins = minutes or DEFAULT_EXPIRY.get(kind, 60)
    cmd = AgentCommand.objects.create(
        router=router,
        kind=kind,
        params=params or {},
        label=label[:200] or kind,
        created_by=user,
        expires_at=timezone.now() + timedelta(minutes=mins),
    )
    if user:
        log(router.business, 'TapTap Link', f'{router.name}: queued {cmd.label}')
    return cmd


def push_pending_vouchers(router, limit=25):
    """Queue TapTap vouchers the router does not have yet."""
    from .sync import voucher_profile
    from .utils import duration_to_routeros
    if AgentCommand.objects.filter(router=router, kind='hotspot_users', status__in=['queued', 'sent']).exists():
        return 0
    todo = list(router.vouchers.filter(source='taptap').exclude(mikrotik_sync_status__in=['Synced', 'Queued'])[:limit])
    if not todo:
        return 0
    users, profiles = [], {}
    for v in todo:
        plan = router.business.plans.filter(name__iexact=v.plan_name).first()
        prof, shared, rate = voucher_profile(v, plan)
        profiles[prof] = {'name': prof, 'shared': shared, 'rate': rate}
        users.append({'n': v.code, 'prof': prof, 'lim': duration_to_routeros(v.duration_hours), 'dis': v.status != 'active', 'c': f'TapTap voucher {v.code}'})
    queue(router, 'hotspot_users', {'users': users, 'profiles': list(profiles.values()), 'ids': [v.pk for v in todo]},
          label=f'Send {len(users)} voucher(s) to the router', minutes=60 * 24)
    Voucher.objects.filter(pk__in=[v.pk for v in todo]).update(mikrotik_sync_status='Queued', mikrotik_sync_error='')
    return len(users)


def parse_sessions(raw):
    rows = []
    for rec in str(raw or '').split(';'):
        f = rec.split(',')
        if len(f) >= 6 and f[0]:
            rows.append({'user': f[0], 'mac-address': f[1], 'address': f[2], 'uptime': f[3], 'bytes-in': f[4], 'bytes-out': f[5], 'id': f[6] if len(f) > 6 else ''})
    return rows


def handle_poll(agent, data, ip, url):
    """Record heartbeat/live sessions and return RouterOS commands to run."""
    from .notify import notify
    router = agent.router
    now = timezone.now()
    first = agent.enrolled_at is None
    old_ip = agent.last_ip
    ip_changed = bool(old_ip) and old_ip != ip
    agent.polls += 1
    agent.last_seen_at, agent.last_ip = now, ip
    agent.identity = str(data.get('id', ''))[:120]
    agent.ros_version = str(data.get('ver', ''))[:60]
    agent.board = str(data.get('board', ''))[:80]
    agent.uptime = str(data.get('up', ''))[:40]
    for f, key in (('cpu_load', 'cpu'), ('memory_free', 'mf'), ('memory_total', 'mt'), ('active_sessions', 'n')):
        try:
            setattr(agent, f, int(str(data.get(key, '')).strip() or 0))
        except ValueError:
            pass
    if first:
        agent.enrolled_at = now
    agent.save()

    was_offline = router.status == 'Offline'
    Router.objects.filter(pk=router.pk).update(
        status='Online', last_error='', last_tested_at=now, last_watch_at=now,
        connection_mode='agent', ip_address='', username='', password='', use_ssl=False,
    )
    from .live import push_event
    if first:
        push_event(router.business_id, f'{router.name} connected through TapTap Link', 'good')
    elif was_offline:
        push_event(router.business_id, f'{router.name} is back online (TapTap Link)', 'good')
        notify(router.business, 'router_online', f'{router.name} is back online', f'{router.name} reconnected through TapTap Link from {ip}.', key=f'router:{router.pk}:online')
    if first:
        notify(router.business, 'link_connected', f'{router.name} connected with TapTap Link',
               f'{router.name} ({agent.board}, RouterOS {agent.ros_version}) is now managed through TapTap Link from {ip}.')
    elif ip_changed:
        notify(router.business, 'link_ip_change', f'{router.name} now connects from a new address',
               f'TapTap Link for {router.name} moved from {old_ip or "?"} to {ip}. This is normal after an ISP change or reconnect; '
               f'if you did not expect it, revoke the token in TapTap.', key=f'link:{router.pk}:ip:{ip}')

    try:
        sv = int(str(data.get('sv', '1')).strip() or 1)
    except ValueError:
        sv = 1
    if agent.script_version != sv:
        RouterAgent.objects.filter(pk=agent.pk).update(script_version=sv)
    if (sv < SCRIPT_VERSION and cache.add(f'tt:link:upgrade:{router.pk}', 1, 1800)
            and not AgentCommand.objects.filter(router=router, kind='self_update', status__in=['queued', 'sent']).exists()):
        queue(router, 'self_update', {'to': SCRIPT_VERSION}, label=f'Update TapTap Link on the router to v{SCRIPT_VERSION}', minutes=30)
    try:
        auto_sync(router, first)
    except Exception as exc:
        logger.info('link auto-sync %s: %s', router, exc)
    try:
        ingest_sessions(router, parse_sessions(data.get('act', '')), now)
    except Exception as exc:
        logger.info('session ingest %s: %s', router, exc)
    try:
        push_pending_vouchers(router)
    except Exception as exc:
        logger.info('voucher push %s: %s', router, exc)
    return build_response(router, url)


def auto_sync(router, first=False):
    """Full inventory sync on the first check-in, then every LINK_SYNC_MINUTES (default 30).

    Also settles sync jobs left 'running' by a router that went offline mid-sync
    (their unfinished tables expired), so a stale job never blocks future syncs.
    """
    from .models import RouterSyncJob
    from .tasks import enqueue_router_sync
    if not first and not cache.add(f'tt:link:autosync:{router.pk}', 1, 60):
        return
    now = timezone.now()
    for job in router.sync_jobs.filter(status__in=['queued', 'running'], created_at__lt=now - timedelta(minutes=35)):
        from .agent_inventory import _record_piece
        _record_piece(router, job.pk, {'errors': ['Some sections did not arrive in time (router offline or busy).']})
        RouterSyncJob.objects.filter(pk=job.pk, status__in=['queued', 'running']).update(
            status='failed', phase='Timed out waiting for the router', finished_at=now)
    every = int(getattr(settings, 'LINK_SYNC_MINUTES', 30))
    last_ok = router.sync_jobs.filter(status='success').order_by('-finished_at').values_list('finished_at', flat=True).first()
    if first or not last_ok or now - last_ok > timedelta(minutes=every):
        enqueue_router_sync(router)


def build_response(router, url):
    """Return the next batch of commands, but only once the previous batch is acknowledged.

    Commands run in a background job on the router, so TapTap waits for the
    acknowledgements before sending more: nothing piles up on a slow router.
    """
    now = timezone.now()
    check = tls_flag(url)
    AgentCommand.objects.filter(router=router, status__in=['queued', 'sent'], expires_at__lt=now).update(status='expired', done_at=now)
    for cmd in AgentCommand.objects.filter(router=router, status='sent'):
        if cmd.sent_at and (now - cmd.sent_at).total_seconds() > ACK_WAIT.get(cmd.kind, 120):
            if cmd.attempts < 2:
                AgentCommand.objects.filter(pk=cmd.pk).update(status='queued')
            else:
                AgentCommand.objects.filter(pk=cmd.pk).update(status='failed', result='No answer from the router', done_at=now)
                if cmd.kind == 'inventory_piece':
                    from .agent_inventory import inventory_command_ack
                    cmd.status, cmd.result = 'failed', 'No answer from the router'
                    inventory_command_ack(cmd, False)
    if AgentCommand.objects.filter(router=router, status='sent').exists():
        return ''  # the router is still working on the last batch
    from .agent_inventory import send_function
    parts, size, helper = [], 0, False
    for cmd in AgentCommand.objects.filter(router=router, status='queued').order_by('created_at')[:40]:
        if cmd.kind in ('reboot', 'self_update') and parts:
            break  # these always travel alone
        try:
            chunk = wrap(cmd, url, check)
        except Exception as exc:
            AgentCommand.objects.filter(pk=cmd.pk).update(status='failed', result=str(exc)[:500], done_at=now)
            continue
        extra = len(send_function()) + 1 if cmd.kind == 'inventory_piece' and not helper else 0
        if parts and size + len(chunk) + extra > MAX_SCRIPT:
            break
        if extra:
            parts.insert(0, send_function())
            helper = True
            size += extra
        parts.append(chunk)
        size += len(chunk) + 1
        AgentCommand.objects.filter(pk=cmd.pk).update(status='sent', sent_at=now, attempts=cmd.attempts + 1)
        if cmd.kind in ('reboot', 'self_update'):
            break
    return '\n'.join(parts)


def handle_ack(cmd_id, given_nonce, status, result=''):
    from .live import push_event
    from .notify import notify
    cmd = AgentCommand.objects.select_related('router__business').filter(pk=cmd_id).first()
    if not cmd or not hmac.compare_digest(nonce(cmd), str(given_nonce)) or cmd.status in ('done', 'failed', 'cancelled'):
        return False
    ok = status == 'ok'
    cmd.status = 'done' if ok else 'failed'
    cmd.done_at = timezone.now()
    cmd.result = (result or ('OK' if ok else 'The router reported an error'))[:500]
    cmd.save(update_fields=['status', 'done_at', 'result'])
    r = cmd.router
    if cmd.kind == 'hotspot_users':
        ids = cmd.params.get('ids', [])
        Voucher.objects.filter(pk__in=ids).update(mikrotik_sync_status='Synced' if ok else 'Error', mikrotik_sync_error='' if ok else 'Router rejected the batch')
    elif cmd.kind == 'interface_set' and ok:
        r.interfaces.filter(name=cmd.params.get('name')).update(disabled=not cmd.params.get('enabled'))
    elif cmd.kind == 'backup':
        f = cmd.params.get('file', 'taptap-backup')
        RouterBackup.objects.create(router=r, name=f, backup_file=f + '.backup' if ok else '', export_file=f + '.rsc' if ok else '',
                                    automatic=cmd.params.get('automatic', False), error='' if ok else 'The router reported an error', created_by=cmd.created_by)
        Router.objects.filter(pk=r.pk).update(last_backup_at=timezone.now())
        notify(r.business, 'backup_done' if ok else 'backup_failed', f'Backup {"saved" if ok else "failed"} on {r.name}',
               f'{f}.backup and {f}.rsc are on the router (WinBox › Files).' if ok else f'The router could not save {f}.')
    elif cmd.kind == 'inventory_piece':
        from .agent_inventory import inventory_command_ack
        inventory_command_ack(cmd, ok)
    push_event(r.business_id, f'{r.name}: {cmd.label} — {"done" if ok else "failed"}', 'info' if ok else 'bad')
    return True


def ingest_sessions(router, rows, now):
    """Same effect as live sync for API routers: sales timing, enforcement, consumption."""
    from .finance import mark_activated
    from .live import voucher_problem
    from .models import SessionIncident
    from .sync import _routeros_seconds
    from .traffic import collect_sessions
    collect_sessions(router, rows, now)
    codes = [r['user'] for r in rows if r.get('user') and not r['user'].upper().startswith('T-')]
    vouchers = {v.code.upper(): v for v in Voucher.objects.filter(business=router.business, code__in=codes)}
    bad = {}
    for s in rows:
        v = vouchers.get(str(s.get('user', '')).upper())
        if not v:
            continue
        if not v.used_at:
            mark_activated(v, now - timedelta(seconds=_routeros_seconds(s.get('uptime'))))
            v.refresh_from_db(fields=['used_at', 'expires_at', 'status'])
        if v.used_at and not v.expires_at and v.duration_hours:
            v.expires_at = v.used_at + timedelta(hours=v.duration_hours)
            Voucher.objects.filter(pk=v.pk, expires_at__isnull=True).update(expires_at=v.expires_at)
        problem = voucher_problem(v, now)
        if problem:
            bad[(v.code.upper(), str(s.get('mac-address', '')).upper())] = (s, v, problem)
    business = router.business
    open_ = {(i.username.upper(), i.mac_address.upper()): i for i in SessionIncident.objects.filter(router=router, status__in=['open', 'ignored'])}
    grace = timedelta(minutes=business.enforce_grace_minutes or 0)
    for key, (s, v, (reason, detail)) in bad.items():
        inc = open_.get(key) or SessionIncident.objects.create(
            business=business, router=router, voucher=v, username=v.code, mac_address=key[1],
            ip_address=s.get('address', ''), session_id=s.get('id', ''), reason=reason, detail=detail,
            first_seen=now, last_seen=now, fix_due_at=now + grace,
        )
        SessionIncident.objects.filter(pk=inc.pk).update(last_seen=now, detail=detail)
        if inc.status == 'open' and business.auto_enforce and inc.fix_due_at and inc.fix_due_at <= now and not cache.get(f'tt:linkfix:{inc.pk}'):
            cache.set(f'tt:linkfix:{inc.pk}', 1, 300)
            fix_incident_via_link(inc, by='auto')
    ended = [i.pk for k, i in open_.items() if k not in bad and i.status == 'open']
    if ended:
        SessionIncident.objects.filter(pk__in=ended).update(status='ended', fixed_at=now, fixed_by='router')


def fix_incident_via_link(inc, user=None, by='user'):
    from .live import push_event
    from .notify import notify
    queue(inc.router, 'disconnect', {'user': inc.username}, label=f'Disconnect {inc.username}', minutes=10)
    queue(inc.router, 'hotspot_user_set', {'name': inc.username, 'disabled': True}, label=f'Disable voucher {inc.username}', minutes=60)
    if inc.voucher_id:
        Voucher.objects.filter(pk=inc.voucher_id).update(status='expired' if inc.reason == 'expired' else 'disabled')
    inc.status, inc.fixed_at, inc.fixed_by, inc.fixed_user = 'fixed', timezone.now(), by, user
    inc.save(update_fields=['status', 'fixed_at', 'fixed_by', 'fixed_user'])
    msg = f'{"Auto-fixed" if by == "auto" else "Fixed"}: {inc.username} disconnected on {inc.router.name} through TapTap Link'
    push_event(inc.business_id, msg, 'fix')
    if by == 'auto':
        notify(inc.business, 'session_enforced', f'Expired voucher disconnected on {inc.router.name}', f'{msg} ({inc.get_reason_display().lower()}).')
    return True, msg


def check_offline_agents():
    """Called from the live-sync beat: mark Link routers offline when they stop calling in."""
    from .live import push_event
    from .notify import notify
    now = timezone.now()
    for agent in RouterAgent.objects.select_related('router__business').filter(revoked=False, last_seen_at__isnull=False, router__connection_mode='agent'):
        silent = (now - agent.last_seen_at).total_seconds()
        if silent > max(90, agent.poll_seconds * 6) and agent.router.status == 'Online':
            Router.objects.filter(pk=agent.router_id).update(status='Offline', last_error=f'No TapTap Link check-in for {int(silent)} s')
            push_event(agent.router.business_id, f'{agent.router.name} stopped calling in (TapTap Link)', 'bad')
            notify(agent.router.business, 'router_offline', f'{agent.router.name} is offline',
                   f'{agent.router.name} has not checked in through TapTap Link since {timezone.localtime(agent.last_seen_at):%H:%M}. Check its power and Internet connection.',
                   severity='critical', key=f'router:{agent.router_id}:offline')
