"""Fit on the router: read anything useful, change a curated set of things — directly.

Fit talks to the MikroTik itself: over the RouterOS API (direct, or through the TapTap Tunnel) when it can
reach it, otherwise over TapTap Link (the change is applied at the router's next check-in, reads come from
the latest synced copy). The model never writes RouterOS commands: it fills in a *structured* operation

    {router, menu, verb, where: [{field, op, value}], values: {field: value}}

which is checked here against an allow-list (menus, verbs, fields, value sizes), previewed against the live
router (which rows it would touch), shown to the person verbatim — including the RouterOS command it
equals — and carried out only when they press "Take action" (core/fit_actions, group "router").

Safety rules
* Allow-listed menus only. No users, files, scripts, schedulers, services, packages, certificates, resets.
* Anything TapTap manages itself (names / comments containing "taptap", TapTap's address lists) is never
  changed or removed here — TapTap is the source of truth for vouchers, members and its own rules.
* Hotspot *users* are not edited on the router (TapTap would overwrite them): Fit uses TapTap's own voucher
  actions for those. Sessions, cookies, hosts, bindings, walled garden, address lists, queues, DNS… are fine.
* At most MAX_ROWS rows per change; an operation matching nothing is refused (nothing to do).
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

logger = logging.getLogger('taptap.fit_router')

MAX_ROWS = 50
FIELD_RE = re.compile(r'^[a-z][a-z0-9.-]{0,39}$')
PROTECT_RE = re.compile(r'taptap|^tt-', re.I)
VALUE_MAX = 160


class RouterOpError(ValueError):
    pass


# key: (API path, CLI path, label, verbs, settable fields, singleton, protect-by-comment)
MENUS = {
    'hotspot_active': ('/ip/hotspot/active', '/ip hotspot active', 'Hotspot sessions', {'remove'}, (), False, False),
    'hotspot_cookies': ('/ip/hotspot/cookie', '/ip hotspot cookie', 'Hotspot cookies', {'remove'}, (), False, False),
    'hotspot_hosts': ('/ip/hotspot/host', '/ip hotspot host', 'Hotspot hosts', {'remove'}, (), False, False),
    'ip_bindings': ('/ip/hotspot/ip-binding', '/ip hotspot ip-binding', 'IP bindings',
                    {'add', 'set', 'remove', 'enable', 'disable'}, ('mac-address', 'address', 'to-address', 'type', 'server', 'comment'), False, True),
    'walled_garden': ('/ip/hotspot/walled-garden', '/ip hotspot walled-garden', 'Walled garden',
                      {'add', 'set', 'remove', 'enable', 'disable'}, ('dst-host', 'dst-port', 'path', 'action', 'server', 'comment'), False, True),
    'walled_garden_ip': ('/ip/hotspot/walled-garden/ip', '/ip hotspot walled-garden ip', 'Walled garden IP',
                         {'add', 'set', 'remove', 'enable', 'disable'}, ('dst-address', 'dst-host', 'protocol', 'dst-port', 'action', 'server', 'comment'), False, True),
    'address_lists': ('/ip/firewall/address-list', '/ip firewall address-list', 'Firewall address lists',
                      {'add', 'set', 'remove', 'enable', 'disable'}, ('list', 'address', 'timeout', 'comment'), False, True),
    'firewall_filter': ('/ip/firewall/filter', '/ip firewall filter', 'Firewall filter rules', {'enable', 'disable', 'set'}, ('comment',), False, True),
    'firewall_nat': ('/ip/firewall/nat', '/ip firewall nat', 'Firewall NAT rules', {'enable', 'disable', 'set'}, ('comment',), False, True),
    'interfaces': ('/interface', '/interface', 'Interfaces (ports)', {'enable', 'disable', 'set'}, ('comment',), False, False),
    'queues_simple': ('/queue/simple', '/queue simple', 'Simple queues',
                      {'add', 'set', 'remove', 'enable', 'disable'}, ('name', 'target', 'max-limit', 'limit-at', 'priority', 'comment'), False, True),
    'dhcp_leases': ('/ip/dhcp-server/lease', '/ip dhcp-server lease', 'DHCP leases', {'remove', 'set', 'make-static'}, ('comment', 'address'), False, True),
    'dns_static': ('/ip/dns/static', '/ip dns static', 'DNS static entries',
                   {'add', 'set', 'remove', 'enable', 'disable'}, ('name', 'address', 'type', 'cname', 'ttl', 'comment'), False, True),
    'dns': ('/ip/dns', '/ip dns', 'DNS settings', {'set'}, ('servers', 'allow-remote-requests', 'cache-size'), True, False),
    'identity': ('/system/identity', '/system identity', 'Router name', {'set'}, ('name',), True, False),
    'wireless': ('/interface/wireless', '/interface wireless', 'Wi-Fi (wireless)', {'set', 'enable', 'disable'}, ('ssid', 'comment'), False, False),
}

# Read-only menus Fit may query (in addition to the change menus above).
READ_MENUS = {
    **{k: (v[0], v[2]) for k, v in MENUS.items()},
    'hotspot_users': ('/ip/hotspot/user', 'Hotspot users'),
    'hotspot_profiles': ('/ip/hotspot/user/profile', 'Hotspot user profiles'),
    'hotspot_servers': ('/ip/hotspot', 'Hotspot servers'),
    'resources': ('/system/resource', 'System resources'),
    'health': ('/system/health', 'Health (temperature, voltage)'),
    'clock': ('/system/clock', 'Clock'),
    'routes': ('/ip/route', 'Routes'),
    'ip_addresses': ('/ip/address', 'IP addresses'),
    'arp': ('/ip/arp', 'ARP table'),
    'neighbors': ('/ip/neighbor', 'Neighbours'),
    'dhcp_servers': ('/ip/dhcp-server', 'DHCP servers'),
    'dhcp_clients': ('/ip/dhcp-client', 'DHCP clients (WAN)'),
    'bridge_ports': ('/interface/bridge/port', 'Bridge ports'),
    'wifi': ('/interface/wifi', 'Wi-Fi (RouterOS 7 wifi)'),
    'logs': ('/log', 'Logs'),
}

TASKS = {
    'reboot': ('Reboot the router', 'danger', 'Everyone is disconnected for about a minute while it restarts.'),
    'flush_dns': ('Flush the DNS cache', 'ok', 'Clears cached DNS answers; the next lookups are fresh.'),
    'sync_clock': ('Set the correct time and turn on NTP', 'ok', 'Corrects the router clock (HTTPS, logs and voucher times depend on it).'),
}

SECRET_FIELDS = {'password', 'secret', 'passphrase', 'private-key', 'wpa2-pre-shared-key', 'wpa-pre-shared-key', 'security.passphrase'}


# ─────────────────────────────── validation ───────────────────────────────

def _clean_value(v):
    v = str(v if v is not None else '').strip()
    if len(v) > VALUE_MAX or any(ord(c) < 32 for c in v):
        raise RouterOpError('A value is too long or contains control characters.')
    return v


def _clean_where(where):
    out = []
    for w in (where or [])[:6]:
        if not isinstance(w, dict):
            raise RouterOpError('Each filter needs field, op and value.')
        field, op = str(w.get('field') or '').strip().lower(), str(w.get('op') or '=').strip()
        if not FIELD_RE.match(field) or field in SECRET_FIELDS:
            raise RouterOpError(f'“{field}” cannot be used as a filter.')
        if op not in ('=', '!=', '~'):
            raise RouterOpError('A filter op must be =, != or ~ (contains).')
        out.append({'field': field, 'op': op, 'value': _clean_value(w.get('value'))})
    return out


def validate(op):
    """Normalise and check one structured operation. Raises RouterOpError."""
    menu, verb = str(op.get('menu') or '').strip(), str(op.get('verb') or '').strip()
    if menu not in MENUS:
        raise RouterOpError(f'Fit cannot change “{menu}”. It can change: {", ".join(MENUS)}.')
    path, cli, label, verbs, fields, singleton, protect = MENUS[menu]
    if verb not in verbs:
        raise RouterOpError(f'{label}: allowed actions are {", ".join(sorted(verbs))}.')
    where = [] if singleton or verb == 'add' else _clean_where(op.get('where'))
    if not singleton and verb != 'add' and not where:
        raise RouterOpError('Say which entries: add at least one filter (e.g. user = ABC123).')
    values = {}
    for k, v in (op.get('values') or {}).items():
        k = str(k).strip().lower()
        if k not in fields:
            raise RouterOpError(f'{label}: “{k}” cannot be set here. Allowed: {", ".join(fields) or "nothing"}.')
        values[k] = _clean_value(v)
    if verb in ('add', 'set') and not values:
        raise RouterOpError('Nothing to set: give at least one value.')
    if verb not in ('add', 'set'):
        values = {}
    for k, v in values.items():
        if (k in ('comment', 'name', 'list') and PROTECT_RE.search(v)) and menu not in ('identity',):
            raise RouterOpError('Names and comments containing “taptap” are reserved for TapTap’s own entries.')
    if menu == 'dns' and 'servers' in values and not re.fullmatch(r'[0-9a-fA-F:.,\s]{2,120}', values['servers']):
        raise RouterOpError('DNS servers must be IP addresses separated by commas.')
    return {'menu': menu, 'verb': verb, 'where': where, 'values': values}


# ─────────────────────────────── matching ───────────────────────────────

def _match(row, where):
    for w in where:
        have = str(row.get(w['field'], '') if row.get(w['field']) is not None else '')
        want = w['value']
        if w['op'] == '=' and have.lower() != want.lower():
            return False
        if w['op'] == '!=' and have.lower() == want.lower():
            return False
        if w['op'] == '~' and want.lower() not in have.lower():
            return False
    return True


def _protected(row):
    return any(PROTECT_RE.search(str(row.get(k) or '')) for k in ('comment', 'name', 'list'))


def _trim(row):
    from .mikrotik import redact
    row = redact(dict(row))
    keep = ('id', '.id', 'name', 'user', 'address', 'mac-address', 'host-name', 'list', 'comment', 'disabled', 'running',
            'dst-host', 'dst-address', 'action', 'type', 'target', 'max-limit', 'uptime', 'bytes-in', 'bytes-out', 'ssid',
            'to-address', 'status', 'dynamic', 'chain', 'servers', 'profile', 'limit-uptime', 'server', 'interface')
    return {k: str(v)[:60] for k, v in row.items() if k in keep and v not in (None, '')}


# ─────────────────────────────── channel ───────────────────────────────

def channel(router):
    from .voucher_history import channel as ch
    return ch(router)


def on_link(router):
    return channel(router) == 'TapTap Link'


def _rows(router, path):
    """Current rows of a menu: live over the API, else the latest TapTap Link copy. Returns (rows, via)."""
    from .mikrotik import MikroTikService
    if not on_link(router):
        try:
            with MikroTikService(router) as svc:
                return [dict(x) for x in svc.safe_get(path) or []], channel(router)
        except Exception as exc:
            if getattr(router, 'connection_mode', 'api') != 'agent':
                raise RouterOpError(f'{router.name} did not answer: {exc}')
    from .linkops import SnapshotService
    if path == '/ip/hotspot/active':
        from .linklive import sessions
        rows = sessions(router)
    else:
        rows = SnapshotService(router).resource(path).get()
    return [dict(x) for x in rows or []], 'TapTap Link (latest synced copy)'


def query(router, menu, where=None, limit=30):
    """Read-only: rows of a menu that match the filters (secrets removed)."""
    if menu not in READ_MENUS:
        raise RouterOpError(f'Choose one of: {", ".join(READ_MENUS)}.')
    path, label = READ_MENUS[menu]
    where = _clean_where(where)
    rows, via = _rows(router, path)
    hits = [r for r in rows if _match(r, where)]
    if menu == 'logs':
        hits = hits[-limit:]
    return {'router': router.name, 'menu': label, 'via': via, 'total': len(rows), 'matching': len(hits),
            'rows': [_trim(r) for r in hits[:max(1, min(60, int(limit or 30)))]]}


def preview(router, op):
    """Which rows a change would touch (live where possible). Raises when it would touch nothing / too much."""
    op = validate(op)
    path, cli, label, verbs, fields, singleton, protect = MENUS[op['menu']]
    if singleton or op['verb'] == 'add':
        return op, [], channel(router)
    rows, via = _rows(router, path)
    hits = [r for r in rows if _match(r, op['where'])]
    if protect or op['verb'] in ('remove', 'disable', 'set'):
        mine = [r for r in hits if _protected(r)]
        if mine:
            raise RouterOpError(f'{len(mine)} matching entr{"y is" if len(mine) == 1 else "ies are"} managed by TapTap itself — '
                                'narrow the filter; Fit will not change TapTap’s own entries.')
    if not hits:
        raise RouterOpError(f'Nothing on {router.name} matches that ({label}, {via}).')
    if len(hits) > MAX_ROWS:
        raise RouterOpError(f'That matches {len(hits)} entries — more than {MAX_ROWS} at once. Narrow the filter.')
    return op, hits, via


def cli_text(op):
    """The RouterOS command the operation equals (shown on the card; also what TapTap Link runs)."""
    from .agent import rs
    path, cli, label, verbs, fields, singleton, protect = MENUS[op['menu']]
    vals = ' '.join(f'{k}={rs(v)}' for k, v in op['values'].items())
    if singleton:
        return f'{cli} set {vals}'
    if op['verb'] == 'add':
        return f'{cli} add {vals}'
    conds = []
    for w in op['where']:
        if w['op'] == '~':
            conds.append(f'{w["field"]}~{rs(re.escape(w["value"]))}')
        else:
            conds.append(f'{w["field"]}{w["op"]}{rs(w["value"])}')
    if protect:
        conds.append('!(comment~"[Tt][Aa][Pp][Tt][Aa][Pp]")')
    find = f'[find where {" and ".join(conds)}]'
    if op['verb'] in ('set',):
        return f'{cli} set {find} {vals}'
    return f'{cli} {op["verb"]} {find}'


def link_body(params):
    """TapTap Link command body (agent kind fit_router_op): re-validated, never trusted as text."""
    return cli_text(validate(params))


# ─────────────────────────────── doing it ───────────────────────────────

def _api_apply(router, op):
    from .mikrotik import MikroTikService
    path, cli, label, verbs, fields, singleton, protect = MENUS[op['menu']]
    with MikroTikService(router) as svc:
        res = svc.resource(path)
        if singleton:
            res.set(**op['values'])
            return f'{label} on {router.name}: set {", ".join(f"{k}={v}" for k, v in op["values"].items())}.'
        if op['verb'] == 'add':
            res.add(**op['values'])
            return f'{label} on {router.name}: added.'
        rows = [dict(r) for r in svc.safe_get(path) or []]
        hits = [r for r in rows if _match(r, op['where']) and not (protect and _protected(r))][:MAX_ROWS]
        done = 0
        for r in hits:
            rid = r.get('id') or r.get('.id')
            if not rid:
                continue
            if op['verb'] == 'remove':
                res.remove(id=rid)
            elif op['verb'] == 'set':
                res.set(id=rid, **op['values'])
            elif op['verb'] in ('enable', 'disable'):
                res.set(id=rid, disabled='yes' if op['verb'] == 'disable' else 'no')
            elif op['verb'] == 'make-static':
                res.call('make-static', {'numbers': rid})
            done += 1
        word = {'remove': 'removed', 'set': 'changed', 'enable': 'enabled', 'disable': 'disabled', 'make-static': 'made static'}[op['verb']]
        return f'{label} on {router.name}: {done} {word}.'


def apply(router, op, user=None):
    """Carry out a validated operation. Returns a sentence for the person."""
    op = validate(op)
    if not on_link(router):
        try:
            return _api_apply(router, op)
        except RouterOpError:
            raise
        except Exception as exc:
            if getattr(router, 'connection_mode', 'api') != 'agent':
                raise RouterOpError(f'{router.name} refused it: {exc}')
            logger.info('Fit router op over tunnel failed for %s, using the Link: %s', router, exc)
    from .linkops import send
    send(router, 'fit_router_op', op, label=f'Fit: {MENUS[op["menu"]][2]} — {op["verb"]}', user=user, minutes=30)
    return f'Sent to {router.name} through TapTap Link — it applies at the next check-in (usually within a minute).'


def run_task(router, task, user=None):
    if task not in TASKS:
        raise RouterOpError(f'Unknown router task. Choose one of: {", ".join(TASKS)}.')
    if task == 'sync_clock':
        from . import router_clock
        r = router_clock.correct(router, user, ntp=True)
        return ('Clock correction sent through TapTap Link.' if r.get('pending')
                else f'Clock set — the router now reads {r.get("router_local") or "the correct time"} with NTP on.')
    if on_link(router):
        from .linkops import send
        if task == 'reboot':
            send(router, 'reboot', {}, label='Fit: reboot', user=user, minutes=10)
        else:
            send(router, 'fit_router_op', {'menu': 'dns', 'verb': 'flush'}, label='Fit: flush DNS cache', user=user, minutes=15)
        return f'Sent to {router.name} through TapTap Link — it runs at the next check-in.'
    from .mikrotik import MikroTikService
    try:
        with MikroTikService(router) as svc:
            if task == 'reboot':
                svc.resource('/system').call('reboot')
                return f'{router.name} is restarting — back in about a minute.'
            svc.resource('/ip/dns/cache').call('flush')
            return f'DNS cache flushed on {router.name}.'
    except Exception as exc:
        if task == 'reboot' and 'closed' in str(exc).lower():
            return f'{router.name} is restarting — back in about a minute.'
        raise RouterOpError(f'{router.name} refused it: {exc}')


def link_task_body(params):
    """Link body for the one non-menu operation sent as fit_router_op (DNS flush)."""
    if params.get('menu') == 'dns' and params.get('verb') == 'flush':
        return '/ip dns cache flush'
    return None


def describe(router, op):
    """(title, detail, danger) for an approval card — with what it touches right now."""
    op, hits, via = preview(router, op)
    path, cli, label, verbs, fields, singleton, protect = MENUS[op['menu']]
    word = {'add': 'Add to', 'set': 'Change', 'remove': 'Remove from', 'enable': 'Enable in', 'disable': 'Disable in',
            'make-static': 'Make static in'}[op['verb']]
    title = f'{word} {label} on {router.name}'
    sample = ''
    if hits:
        def short(r):
            t = _trim(r)
            return ', '.join(f'{k}={v}' for k, v in t.items() if k not in ('id', '.id'))[:90]
        sample = f' Touches {len(hits)} entr{"y" if len(hits) == 1 else "ies"}: ' + '; '.join(short(r) for r in hits[:3]) + ('…' if len(hits) > 3 else '') + '.'
    danger = 'danger' if op['verb'] in ('remove', 'disable') and op['menu'] in ('interfaces', 'firewall_filter', 'firewall_nat', 'wireless') \
        else 'warn' if op['verb'] in ('remove', 'disable') or singleton else 'ok'
    detail = f'RouterOS: {cli_text(op)} — via {via}.{sample}'
    return op, title, detail, danger, timezone.now()
