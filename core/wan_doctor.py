"""Internet Doctor — finds and fixes "the modem answers but the internet doesn't" ON the router.

Why this exists
===============
On 20 Oct 2026 a hAP ax³ on two Starlinks lost all customer traffic. Each line had one
health-check address. The Starlink router kept letting the MikroTik reach that one address
(1.1.1.1), but nothing else, so the line looked healthy and its route stayed active. TapTap
itself could not reach the router, so nothing in the platform could help. Giving the MikroTik
a new MAC address fixed it at once (the Starlink router was holding back the old one).

What it does
============
1. A small script is installed ON the router (/system script + /system scheduler, every
   minute). It keeps working when TapTap cannot reach the router, which is exactly when it
   is needed. For each internet line it checks:
      • the modem / gateway answers on its own port,
      • 3 independent internet addresses answer through THAT port (its own /32 probe
        routes, with a blackhole guard so a probe can never leak out of another line).
   A line is healthy only when at least 2 of the 3 answer.
2. When the modem answers but the internet doesn't, it escalates step by step:
      2 min  clear the router's ARP entry for the modem
      4 min  renew DHCP
      6 min  give the port a new MAC address (at most once an hour) and renew DHCP
     16 min  if the new MAC did not help, put the original MAC back (ISPs that lock a MAC)
   and, from 3 min, if ANOTHER line is healthy, it takes the broken line's TapTap default
   routes out of use (disabled, marked " ~doctor-off:<port>"). They are put back after two
   good checks in a row.
3. Over the API (when TapTap can reach the router) the same check runs live from the
   Internet lines page and from Fit, with one-click fixes.

Everything it creates carries a comment starting with TAG, so it can be found and removed
without touching anything the owner built by hand. No database migration is needed.
"""
from __future__ import annotations

import logging
import secrets
from functools import wraps

logger = logging.getLogger('taptap.wan_doctor')

TAG = 'TapTap Doctor'
NAME = 'TapTap-Doctor'                 # script and scheduler name on the router
VERSION = '1'
OFF_MARK = ' ~doctor-off:'             # appended to a route comment while the Doctor holds it disabled
MAX_LINES = 4
# Public addresses that answer ping and are NOT used as Internet Lines health checks (wan.CHECK_HOSTS),
# so a probe route never fights a health-check route. Three per line.
PROBE_POOL = ['149.112.112.112', '208.67.220.220', '94.140.14.14',
              '94.140.15.15', '76.76.2.0', '76.76.10.0',
              '185.228.168.9', '185.228.169.9', '4.2.2.1',
              '4.2.2.2', '64.6.64.6', '64.6.65.6']
ESCALATION = [(2, 'Clear the router’s memory of the modem (ARP)'), (4, 'Renew DHCP'),
              (6, 'Give the port a new MAC address and renew DHCP'),
              (16, 'Put the original MAC back if the new one did not help')]
_INSTALLED = False


class DoctorError(Exception):
    pass


def _s(v):
    return str(v if v is not None else '').strip()


def _yes(v):
    return _s(v).lower() in {'true', 'yes'}


# ═══════════════════════════ which lines ═══════════════════════════
def _assign_probes(lines):
    for i, l in enumerate(lines[:MAX_LINES]):
        l['probes'] = PROBE_POOL[i * 3:i * 3 + 3]
    return lines[:MAX_LINES]


def _clean(iface, kind, gateway='', label=''):
    return {'interface': _s(iface), 'type': kind if kind in {'dhcp', 'pppoe', 'static'} else 'static',
            'gateway': _s(gateway) if kind == 'static' else '', 'label': (_s(label) or _s(iface))[:40]}


def lines_from_cfg(cfg):
    """Lines of a normalized Internet Lines config (wan.normalize)."""
    out = []
    for l in (cfg or {}).get('links') or []:
        if l.get('interface') and (l.get('type') != 'static' or l.get('gateway')):
            out.append(_clean(l['interface'], l.get('type'), l.get('gateway'), l.get('label')))
    return _assign_probes(out)


def lines_from_setup(setup):
    """Best-effort lines without a router connection (TapTap Link routers, script download)."""
    labels, out, seen = {}, [], set()
    for l in ((setup.config or {}).get('links') or []):
        if l.get('interface'):
            labels[_s(l['interface'])] = _s(l.get('label'))
            if l.get('type') == 'static' and _s(l.get('gateway')):
                out.append(_clean(l['interface'], 'static', l['gateway'], l.get('label'))); seen.add(_s(l['interface']))
    for l in ((setup.facts or {}).get('links') or []):
        iface = _s(l.get('interface'))
        if iface and iface not in seen and l.get('type') in {'dhcp', 'pppoe'}:
            out.append(_clean(iface, l['type'], '', labels.get(iface) or l.get('label'))); seen.add(iface)
    return _assign_probes(out)


def lines_for(svc, setup=None):
    """Every internet line the router has — DHCP and PPPoE clients INCLUDING disabled ones (a line
    switched off today is watched again as soon as the owner turns it back on), plus static lines
    from the saved Internet Lines setup."""
    labels, statics = {}, []
    if setup is not None:
        for l in ((setup.config or {}).get('links') or []):
            if l.get('interface'):
                labels[_s(l['interface'])] = _s(l.get('label'))
                if l.get('type') == 'static' and _s(l.get('gateway')):
                    statics.append(_clean(l['interface'], 'static', l['gateway'], l.get('label')))
    roles = {}
    try:
        roles = {x.interface_name: x.label for x in svc.router.interface_roles.all() if x.label}
    except Exception:
        pass
    out, seen = [], set()

    def add(iface, kind, comment=''):
        if iface and iface not in seen:
            seen.add(iface)
            out.append(_clean(iface, kind, '', labels.get(iface) or roles.get(iface) or comment))

    for r in svc.safe_get('/ip/dhcp-client'):
        add(_s(r.get('interface')), 'dhcp', _s(r.get('comment')))
    for r in svc.safe_get('/interface/pppoe-client'):
        add(_s(r.get('name')), 'pppoe', _s(r.get('comment')))
    for l in statics:
        if l['interface'] not in seen:
            seen.add(l['interface']); out.append(l)
    return _assign_probes(out)


# ═══════════════════════════ the on-router script ═══════════════════════════
def _ros(v):
    """A value as a RouterOS string literal."""
    return '"' + _s(v).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def _arr(values):
    return '{' + ';'.join(_ros(v) for v in values) + '}'


_BODY = r'''
:global ttdoc
:global ttdocRun
:global ttdocBusy
:global ttdocStatus
:if ([:typeof $ttdoc] != "array") do={ :set ttdoc [:toarray ""] }
:if ([:typeof $ttdocRun] != "num") do={ :set ttdocRun 0 }
:set ttdocRun ($ttdocRun + 1)
:local busy false
:if ([:typeof $ttdocBusy] = "num") do={ :if (($ttdocRun - $ttdocBusy) < 5) do={ :set busy true } }

# ---- measure one line: returns "skip" / "down,<gw>" / "holding,<gw>" / "ok,<gw>"
:local measure do={
  :local gw ""
  :local rgw ""
  :if ($kind = "dhcp") do={
    :local c [/ip dhcp-client find where interface=$iface]
    :if ([:len $c] = 0) do={ :return "skip" }
    :if ([:tostr [/ip dhcp-client get ($c->0) disabled]] = "true") do={ :return "skip" }
    :if ([:tostr [/ip dhcp-client get ($c->0) status]] != "bound") do={ :return "down," }
    :set gw [:tostr [/ip dhcp-client get ($c->0) gateway]]
  }
  :if ($kind = "pppoe") do={
    :local p [/interface pppoe-client find where name=$iface]
    :if ([:len $p] = 0) do={ :return "skip" }
    :if ([:tostr [/interface pppoe-client get ($p->0) disabled]] = "true") do={ :return "skip" }
    :if ([:tostr [/interface pppoe-client get ($p->0) running]] != "true") do={ :return "down," }
    :set rgw $iface
  }
  :if ($kind = "static") do={
    :local n [/interface find where name=$iface]
    :if ([:len $n] = 0) do={ :return "skip" }
    :if ([:tostr [/interface get ($n->0) running]] != "true") do={ :return "down," }
    :set gw $sgw
  }
  :if ($kind != "pppoe") do={
    :if ([:len $gw] = 0) do={ :return "down," }
    :if ([/ping address=$gw interface=$iface count=2] = 0) do={ :return ("down," . $gw) }
    :set rgw ($gw . "%" . $iface)
  }
  :local tag ("TapTap Doctor probe " . $iface)
  :local passed 0
  :foreach p in=[:toarray $probes] do={
    :local dst ($p . "/32")
    :local r [/ip route find where dst-address=$dst and comment=$tag]
    :if ([:len $r] = 0) do={
      /ip route add dst-address=$dst gateway=$rgw comment=$tag
      :do { /ip route add dst-address=$dst blackhole distance=250 comment=("TapTap Doctor guard " . $iface) } on-error={}
      :delay 1s
    } else={
      :if ([:tostr [/ip route get ($r->0) gateway]] != $rgw) do={ /ip route set $r gateway=$rgw }
    }
    :if ([/ping address=$p count=2] > 0) do={ :set passed ($passed + 1) }
  }
  :if ($passed >= 2) do={ :return ("ok," . $gw) }
  :return ("holding," . $gw)
}

# ---- act on one line
:local act do={
  :global ttdoc
  :global ttdocRun
  :local kf ($iface . "-fail")
  :local kg ($iface . "-good")
  :local km ($iface . "-macat")
  :local ks ($iface . "-macset")
  :local fail ($ttdoc->$kf)
  :if ([:typeof $fail] != "num") do={ :set fail 0 }
  :local good ($ttdoc->$kg)
  :if ([:typeof $good] != "num") do={ :set good 0 }
  :local macAt ($ttdoc->$km)
  :if ([:typeof $macAt] != "num") do={ :set macAt -1000 }
  :local macSet ($ttdoc->$ks)
  :local oth [:tonum $others]
  :local who ($label . " (" . $iface . ")")

  :if ($st = "skip") do={ :set ($ttdoc->$kf) 0; :set ($ttdoc->$kg) 0; :return "" }

  :if ($st = "ok") do={
    :if ($fail >= 3) do={ :log warning ("TapTap Doctor: " . $who . " reaches the internet again") }
    :set ($ttdoc->$kf) 0
    :set ($ttdoc->$ks) false
    :set good ($good + 1)
    :set ($ttdoc->$kg) $good
    :if ($good >= 2) do={
      :foreach r in=[/ip route find where comment~("~doctor-off:" . $iface . "\$")] do={
        :local c [:tostr [/ip route get $r comment]]
        :local cut [:find $c " ~doctor-off:"]
        /ip route set $r disabled=no comment=[:pick $c 0 $cut]
        :log warning ("TapTap Doctor: " . $who . " is healthy again and back in use")
      }
    }
    :return ""
  }

  :set ($ttdoc->$kg) 0
  :set fail ($fail + 1)
  :set ($ttdoc->$kf) $fail

  :if ($fail = 3) do={
    :if ($st = "holding") do={
      :log warning ("TapTap Doctor: " . $who . " - the modem answers but the internet does not get through")
    } else={
      :log warning ("TapTap Doctor: " . $who . " - the modem is not answering (power, cable or no DHCP)")
    }
  }

  :if ($st = "holding") do={
    :do {
    :if ($fail = 2) do={
      :do { /ip arp remove [find where interface=$iface and address=$gw and dynamic=yes] } on-error={}
    }
    :if (($fail = 4) && ($kind = "dhcp")) do={
      :log info ("TapTap Doctor: " . $who . " - renewing DHCP")
      :do { /ip dhcp-client release [find where interface=$iface] } on-error={}
    }
    :if (($fail = 6) && ($allowMac = "yes")) do={
      :if (([:len [/interface ethernet find where name=$iface]] > 0) && (($ttdocRun - $macAt) > 60)) do={
        :local hex "0123456789ABCDEF"
        :local mac "02:54:54"
        :for b from=1 to=3 do={
          :local n [:rndnum from=0 to=255]
          :set mac ($mac . ":" . [:pick $hex ($n / 16)] . [:pick $hex ($n % 16)])
        }
        /interface ethernet set [find where name=$iface] mac-address=$mac
        :set ($ttdoc->$km) $ttdocRun
        :set ($ttdoc->$ks) true
        :log warning ("TapTap Doctor: " . $who . " - the modem seems to be holding back this router, new MAC " . $mac)
        :delay 2s
        :if ($kind = "dhcp") do={ :do { /ip dhcp-client release [find where interface=$iface] } on-error={} }
      }
    }
    :if (($fail = 16) && ($macSet = true)) do={
      :do { /interface ethernet reset-mac-address [find where name=$iface] } on-error={}
      :set ($ttdoc->$ks) false
      :log warning ("TapTap Doctor: " . $who . " - the new MAC did not help, original MAC put back")
      :delay 2s
      :if ($kind = "dhcp") do={ :do { /ip dhcp-client release [find where interface=$iface] } on-error={} }
    }
    } on-error={ :log error ("TapTap Doctor: a fix step failed on " . $iface) }
  }

  # another line works: stop sending customers into this one
  :if (($fail >= 3) && ($oth > 0)) do={
    :do {
    :foreach r in=[/ip route find where dst-address=0.0.0.0/0 and dynamic=no and disabled=no and comment~"^TapTap" and immediate-gw~("%" . $iface . "\$")] do={
      :local c [:tostr [/ip route get $r comment]]
      :if ([:typeof [:find $c "~doctor-off"]] != "num") do={
        /ip route set $r disabled=yes comment=($c . " ~doctor-off:" . $iface)
        :log warning ("TapTap Doctor: " . $who . " taken out of use - customers move to the other line")
      }
    }
    } on-error={ :log error ("TapTap Doctor: could not take " . $iface . " out of use") }
  }

  :if (($st = "down") && ($kind = "dhcp")) do={
    :if (($fail % 15) = 5) do={ :do { /ip dhcp-client release [find where interface=$iface] } on-error={} }
  }
  :return ""
}

:if ($busy) do={
  :log debug "TapTap Doctor: the previous check is still running"
} else={
  :set ttdocBusy $ttdocRun
  :local states [:toarray ""]
  :local gwsNow [:toarray ""]
  :local okCount 0
  :for i from=0 to=([:len $ifaces] - 1) do={
    :local res "skip"
    :do { :set res [$measure iface=($ifaces->$i) kind=($kinds->$i) sgw=($gws->$i) probes=($probes->$i)] } on-error={ :set res "skip" }
    :local st $res
    :local g ""
    :local cpos [:find $res ","]
    :if ([:typeof $cpos] = "num") do={ :set st [:pick $res 0 $cpos]; :set g [:pick $res ($cpos + 1) [:len $res]] }
    :set ($states->($ifaces->$i)) $st
    :set ($gwsNow->($ifaces->$i)) $g
    :if ($st = "ok") do={ :set okCount ($okCount + 1) }
  }
  :local summary ""
  :for i from=0 to=([:len $ifaces] - 1) do={
    :local ifc ($ifaces->$i)
    :local st ($states->$ifc)
    :local others $okCount
    :if ($st = "ok") do={ :set others ($okCount - 1) }
    :do { $act iface=$ifc kind=($kinds->$i) label=($labels->$i) st=$st gw=($gwsNow->$ifc) others=$others allowMac=$allowMac } on-error={ :log error ("TapTap Doctor: could not act on " . $ifc) }
    :set summary ($summary . $ifc . "=" . $st . " ")
  }
  :set ttdocStatus ("run " . $ttdocRun . ": " . $summary)
  :set ttdocBusy
}
'''


def render_source(lines, allow_mac=True, router_name=''):
    """The /system script source for these lines."""
    lines = _assign_probes([dict(l) for l in lines])
    if not lines:
        raise DoctorError('No internet lines to watch — connect a DHCP or PPPoE line, or set one up on the Internet lines page.')
    head = [f'# TapTap Internet Doctor v{VERSION}' + (f' - {router_name}' if router_name else ''),
            '# Checks every internet line each minute and fixes "modem answers, internet does not".',
            '# Made by TapTap - remove it from TapTap (Internet lines > Internet Doctor) or with the remove script.',
            ':local ifaces ' + _arr(l['interface'] for l in lines),
            ':local kinds ' + _arr(l['type'] for l in lines),
            ':local gws ' + _arr(l.get('gateway', '') for l in lines),
            ':local labels ' + _arr(l['label'] for l in lines),
            ':local probes ' + _arr(','.join(l['probes']) for l in lines),
            ':local allowMac ' + _ros('yes' if allow_mac else 'no')]
    return '\n'.join(head) + _BODY


def _rsc_string(text):
    return ('"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('?', '\\?')
            .replace('\r', '').replace('\t', '    ').replace('\n', '\\r\\n') + '"')


def removal_commands():
    """RouterOS commands that remove the Doctor and give back any route it was holding."""
    return [
        f'/system scheduler remove [find where name="{NAME}"]',
        f'/system script remove [find where name="{NAME}"]',
        f'/ip route remove [find where comment~"^{TAG}"]',
        ':foreach r in=[/ip route find where comment~"~doctor-off:"] do={ :local c [:tostr [/ip route get $r comment]]; '
        '/ip route set $r disabled=no comment=[:pick $c 0 [:find $c " ~doctor-off:"]] }',
        '/system script environment remove [find where name~"^ttdoc"]',
    ]


def render_install_rsc(lines, allow_mac=True, router_name=''):
    """A .rsc that installs the Doctor (paste into the terminal or /import it)."""
    src = render_source(lines, allow_mac, router_name)
    lines = _assign_probes([dict(l) for l in lines])
    out = [f'# TapTap Internet Doctor v{VERSION} - install' + (f' - {router_name}' if router_name else ''),
           '# Watches: ' + ', '.join(f'{l["label"]} ({l["interface"]})' for l in lines),
           '# 1. Remove an earlier copy']
    out += [f':do {{ {c} }} on-error={{}}' for c in removal_commands()]
    out += ['', '# 2. The check, every minute',
            f'/system script add name={NAME} policy=read,write,test dont-require-permissions=no comment="{TAG} v{VERSION}" source={_rsc_string(src)}',
            f'/system scheduler add name={NAME} interval=1m policy=read,write,test on-event="/system script run {NAME}" '
            f'comment="{TAG} - checks the internet lines every minute"',
            '', '# Done. Activity: /log print where message~"TapTap Doctor"   Status: :put $ttdocStatus']
    return '\n'.join(out) + '\n'


def render_remove_rsc():
    return '# TapTap Internet Doctor - remove\n' + '\n'.join(removal_commands()) + '\n'


# ═══════════════════════════ over the API ═══════════════════════════
def _find(res, **where):
    return [r for r in res.get() if all(_s(r.get(k)) == _s(v) for k, v in where.items())]


def install_on_router(svc, lines, allow_mac=True, router_name=''):
    src = render_source(lines, allow_mac, router_name)
    uninstall_from_router(svc, restore_routes=False)
    svc.resource('/system/script').add(name=NAME, policy='read,write,test', comment=f'{TAG} v{VERSION}', source=src)
    svc.resource('/system/scheduler').add(name=NAME, interval='1m', policy='read,write,test',
                                          on_event=f'/system script run {NAME}',
                                          comment=f'{TAG} - checks the internet lines every minute')
    return [l['interface'] for l in _assign_probes([dict(l) for l in lines])]


def uninstall_from_router(svc, restore_routes=True):
    removed = 0
    for path in ('/system/scheduler', '/system/script'):
        try:
            res = svc.resource(path)
            for row in _find(res, name=NAME):
                res.remove(id=row['id']); removed += 1
        except Exception:
            pass
    try:
        res = svc.resource('/ip/route')
        for row in res.get():
            c = _s(row.get('comment'))
            if c.startswith(TAG):
                try: res.remove(id=row['id']); removed += 1
                except Exception: pass
            elif restore_routes and OFF_MARK in c:
                try: res.set(id=row['id'], disabled='no', comment=c.split(OFF_MARK)[0])
                except Exception: pass
    except Exception:
        pass
    try:
        res = svc.resource('/system/script/environment')
        for row in res.get():
            if _s(row.get('name')).startswith('ttdoc'):
                try: res.remove(id=row['id'])
                except Exception: pass
    except Exception:
        pass
    return removed


def installed(svc):
    return bool(_find(svc.resource('/system/scheduler'), name=NAME))


def recent_activity(svc, limit=12):
    rows = []
    for r in svc.safe_get('/log'):
        msg = _s(r.get('message'))
        if msg.startswith('TapTap Doctor:'):
            rows.append({'time': _s(r.get('time')), 'message': msg[len('TapTap Doctor:'):].strip(),
                         'level': 'warning' if 'warning' in _s(r.get('topics')) else 'info'})
    return rows[-limit:][::-1]


def status_line(svc):
    for r in svc.safe_get('/system/script/environment'):
        if _s(r.get('name')) == 'ttdocStatus':
            return _s(r.get('value'))
    return ''


def _ping(svc, address, **extra):
    from .wan import ping
    return ping(svc, address, count=2, **extra)


def check_line(svc, line):
    """Live diagnosis of one line. Never leaves anything behind on the router."""
    iface, kind = line['interface'], line['type']
    out = {'interface': iface, 'label': line['label'], 'type': kind, 'gateway': '', 'state': 'ok',
           'reached': [], 'missed': [], 'finding': '', 'fix': '', 'mac': ''}
    eth = [r for r in svc.safe_get('/interface/ethernet') if _s(r.get('name')) == iface]
    if eth:
        out['mac'] = _s(eth[0].get('mac-address'))
        out['mac_changed'] = bool(_s(eth[0].get('orig-mac-address'))) and _s(eth[0].get('orig-mac-address')).upper() != out['mac'].upper()
    rgw = ''
    if kind == 'dhcp':
        c = [r for r in svc.safe_get('/ip/dhcp-client') if _s(r.get('interface')) == iface]
        if not c:
            return {**out, 'state': 'off', 'finding': f'{line["label"]}: there is no DHCP client on {iface}.'}
        if _yes(c[0].get('disabled')):
            return {**out, 'state': 'off', 'finding': f'{line["label"]} is switched off on the router (its DHCP client on {iface} is disabled).'}
        if _s(c[0].get('status')) != 'bound':
            return {**out, 'state': 'down', 'fix': 'refresh',
                    'finding': f'{line["label"]}: the modem is not giving {iface} an address (DHCP {_s(c[0].get("status")) or "not bound"}). Check its power and cable, or wait for it to finish restarting.'}
        out['gateway'] = _s(c[0].get('gateway'))
    elif kind == 'pppoe':
        p = [r for r in svc.safe_get('/interface/pppoe-client') if _s(r.get('name')) == iface]
        if not p or _yes(p[0].get('disabled')):
            return {**out, 'state': 'off', 'finding': f'{line["label"]} is switched off on the router.'}
        if not _yes(p[0].get('running')):
            return {**out, 'state': 'down', 'finding': f'{line["label"]}: the PPPoE session is not connected — check the ISP account and the cable.'}
        rgw = iface
    else:
        out['gateway'] = line.get('gateway', '')
    if kind != 'pppoe':
        if not out['gateway']:
            return {**out, 'state': 'down', 'fix': 'refresh', 'finding': f'{line["label"]}: no gateway on {iface}.'}
        g = _ping(svc, out['gateway'], interface=iface)
        if not g.get('ok'):
            return {**out, 'state': 'down', 'fix': 'refresh',
                    'finding': f'{line["label"]}: the modem at {out["gateway"]} is not answering on {iface}. Check its power and cable.'}
        rgw = f'{out["gateway"]}%{iface}'
    routes = svc.resource('/ip/route')
    added = []
    try:
        mine = {_s(r.get('dst-address')) for r in routes.get() if _s(r.get('comment')) == f'{TAG} probe {iface}'}
        for p in line['probes']:
            if f'{p}/32' not in mine:
                added.append(routes.add(dst_address=f'{p}/32', gateway=rgw, comment=f'{TAG} test {iface}'))
        for p in line['probes']:
            (out['reached'] if _ping(svc, p).get('ok') else out['missed']).append(p)
    finally:
        for row in routes.get():
            if _s(row.get('comment')) == f'{TAG} test {iface}':
                try: routes.remove(id=row['id'])
                except Exception: pass
    n = len(out['reached'])
    if n >= 2:
        out['finding'] = f'{line["label"]} is working: the modem answers and {n} of 3 internet addresses answer through {iface}.'
        return out
    out['state'] = 'holding'
    out['fix'] = 'new_mac' if eth else 'refresh'
    out['finding'] = (f'{line["label"]}: the modem answers but only {n} of 3 internet addresses get through {iface}. '
                      'The modem (often a Starlink router) is holding back this router. '
                      + ('A new MAC address on the port usually fixes it at once.' if eth else 'Renewing the connection usually fixes it.'))
    if out.get('mac_changed'):
        out['finding'] += ' This port already has a changed MAC — if a new one does not help, put the original back.'
    return out


def diagnose(svc, lines):
    rows = []
    for l in _assign_probes([dict(x) for x in lines]):
        try:
            row = check_line(svc, l)
        except Exception as exc:
            row = {'interface': l['interface'], 'label': l['label'], 'state': 'error', 'finding': f'Could not check {l["label"]}: {exc}', 'fix': ''}
        try:
            row['use'] = use_state(svc, l['interface'])
            row['switchable'] = _client(svc, l['interface'])[1] is not None
        except Exception:
            row['use'], row['switchable'] = {'state': 'out', 'has_routes': False, 'routes_off': 0}, False
        if row['state'] == 'ok' and row['use']['state'] == 'out':
            row['finding'] += ' Customers are not using it yet — press “Put in use”.'
        rows.append(row)
    working = [r for r in rows if r['state'] == 'ok']
    idle = [r for r in working if r.get('use', {}).get('state') == 'out']
    broken = [r for r in rows if r['state'] in {'down', 'holding'}]
    if idle:
        headline = ', '.join(r['label'] for r in idle) + ' works but customers are not using it — press “Put in use”.'
    elif not broken and working:
        headline = 'Every connected line reaches the internet.'
    elif broken and working:
        headline = f'{len(working)} line(s) working, {len(broken)} with a problem — customers use the working line(s).'
    elif broken:
        headline = 'No line reaches the internet right now — see the fix below.'
    else:
        headline = 'No connected internet line found.'
    return {'headline': headline, 'lines': rows}


def _release(svc, iface):
    res = svc.resource('/ip/dhcp-client')
    for row in _find(res, interface=iface):
        try: res.call('release', {'numbers': row['id']})
        except Exception: pass


def random_mac():
    return '02:54:54:' + ':'.join(f'{b:02X}' for b in secrets.token_bytes(3))


def fix(svc, iface, step):
    """One-click fixes. Returns a plain-language result."""
    iface = _s(iface)
    eth_res = svc.resource('/interface/ethernet')
    eth = _find(eth_res, name=iface)
    arp = svc.resource('/ip/arp')
    if step == 'refresh':
        for row in arp.get():
            if _s(row.get('interface')) == iface and _yes(row.get('dynamic')):
                try: arp.remove(id=row['id'])
                except Exception: pass
        _release(svc, iface)
        return f'Cleared the router’s memory of the modem on {iface} and asked it for a fresh address.'
    if step == 'new_mac':
        if not eth:
            raise DoctorError(f'{iface} is not an Ethernet port, so its MAC cannot be changed.')
        mac = random_mac()
        eth_res.set(id=eth[0]['id'], mac_address=mac)
        _release(svc, iface)
        return f'{iface} now uses MAC {mac} — the modem sees it as a new device. Give it about 20 seconds.'
    if step == 'reset_mac':
        if not eth:
            raise DoctorError(f'{iface} is not an Ethernet port.')
        eth_res.call('reset-mac-address', {'numbers': eth[0]['id']})
        _release(svc, iface)
        return f'{iface} is back on its original MAC address.'
    raise DoctorError('Unknown fix.')


# ═══════════════════════════ line control (on/off, in use) ═══════════════════════════
def _managed(row):
    c = _s(row.get('comment'))
    return c.startswith('TapTap') and not c.startswith(TAG) and not _yes(row.get('dynamic'))


def line_routes(svc, iface):
    """TapTap's routes for one line: its health-check /32 routes and its default routes.
    Works for current Internet Lines routes ('TapTap WAN …') and older ones ('TapTap <label> pinned')."""
    rows = svc.resource('/ip/route').get()
    checks = [r for r in rows if _managed(r) and _s(r.get('dst-address')).endswith('/32')
              and _s(r.get('gateway')).endswith('%' + iface)]
    hosts = {_s(r.get('dst-address')).split('/')[0] for r in checks}
    defaults = [r for r in rows if _managed(r) and _s(r.get('dst-address')) == '0.0.0.0/0'
                and (_s(r.get('gateway')) in hosts or _s(r.get('gateway')).endswith('%' + iface) or _s(r.get('gateway')) == iface)]
    plain = [r for r in rows if _yes(r.get('dynamic')) and _s(r.get('dst-address')) == '0.0.0.0/0'
             and _s(r.get('vrf-interface') or r.get('immediate-gw')).split('%')[-1] == iface]
    return checks, defaults, plain


def use_state(svc, iface):
    """'managed' = customers use it through TapTap's routes, 'plain' = only through the line's own
    DHCP default route (an emergency setup), 'out' = customers do not use it."""
    checks, defaults, plain = line_routes(svc, iface)
    on = [r for r in defaults if not _yes(r.get('disabled'))]
    active = [r for r in on if _yes(r.get('active'))]
    if active:
        state = 'managed'
    elif any(_yes(r.get('active')) for r in plain):
        state = 'plain'
    else:
        state = 'out'
    return {'state': state, 'has_routes': bool(defaults), 'routes_off': len(defaults) - len(on)}


def _client(svc, iface):
    for path, key in (('/ip/dhcp-client', 'interface'), ('/interface/pppoe-client', 'name')):
        res = svc.resource(path)
        rows = _find(res, **{key: iface})
        if rows:
            return res, rows[0], path
    return None, None, None


def _others_in_use(svc, iface, lines):
    return [l['label'] for l in lines if l['interface'] != iface and use_state(svc, l['interface'])['state'] != 'out']


def turn_line(svc, iface, on, lines):
    res, row, path = _client(svc, iface)
    if not row:
        raise DoctorError(f'{iface} has no DHCP or PPPoE client, so it cannot be switched on or off here.')
    label = next((l['label'] for l in lines if l['interface'] == iface), iface)
    if not on and not _others_in_use(svc, iface, lines):
        raise DoctorError(f'{label} is the only line customers use right now — switching it off would cut the internet '
                          'and TapTap’s connection to the router. Put another line in use first.')
    res.set(id=row['id'], disabled='no' if on else 'yes')
    if on:
        return f'{label} is switched on. Give the modem about 10 seconds, then check the line before putting it in use.'
    return f'{label} is switched off.'


def set_in_use(svc, iface, on, lines):
    label = next((l['label'] for l in lines if l['interface'] == iface), iface)
    routes = svc.resource('/ip/route')
    checks, defaults, plain = line_routes(svc, iface)
    res, client, path = _client(svc, iface)
    is_dhcp = path == '/ip/dhcp-client'
    if on:
        if client is not None and _yes(client.get('disabled')):
            raise DoctorError(f'{label} is switched off. Switch it on and check it first.')
        for r in checks + defaults:
            c = _s(r.get('comment'))
            routes.set(id=r['id'], disabled='no', comment=c.split(OFF_MARK)[0] if OFF_MARK in c else c)
        if is_dhcp:
            # TapTap's own routes carry this line now, so drop the DHCP route added during an emergency;
            # a line without TapTap routes gets its plain DHCP route instead.
            res.set(id=client['id'], add_default_route='no' if defaults else 'yes')
        if not defaults and not is_dhcp:
            raise DoctorError(f'{label} has no TapTap routes yet — set it up in step 1 above and apply.')
        return f'{label} is in use again — customers are shared across it.'
    if not _others_in_use(svc, iface, lines):
        raise DoctorError(f'{label} is the only line customers use right now. Put another line in use first.')
    for r in defaults:
        if not _yes(r.get('disabled')):
            routes.set(id=r['id'], disabled='yes')
    if is_dhcp and client is not None:
        res.set(id=client['id'], add_default_route='no')
    return f'{label} is out of use — customers use the other line(s). It stays connected so you can test it.'


# ═══════════════════════════ hooks into Internet Lines ═══════════════════════════
def _wrap_build_plan(original):
    @wraps(original)
    def build_plan(cfg, facts, run=None):
        plan = original(cfg, facts, run)
        plan['doctor_lines'] = lines_from_cfg(cfg)
        return plan
    return build_plan


def _wrap_apply_plan(original):
    @wraps(original)
    def apply_plan(svc, plan, facts, original_state, undo_minutes=5, log=None):
        result = original(svc, plan, facts, original_state, undo_minutes=undo_minutes, log=log)
        lines = plan.get('doctor_lines') or []
        if lines:
            try:
                install_on_router(svc, lines, router_name=getattr(getattr(svc, 'router', None), 'name', ''))
                result.setdefault('notes', []).append('Internet Doctor watches ' + ', '.join(l['label'] for l in lines) + ' every minute')
            except Exception as exc:  # the Doctor must never break an apply
                logger.warning('Internet Doctor install failed: %s', exc)
                result.setdefault('notes', []).append(f'Internet Doctor could not be installed ({exc})')
        return result
    return apply_plan


def _wrap_undo(original):
    @wraps(original)
    def undo(svc, original_state, v7):
        removed = original(svc, original_state, v7)
        try:
            removed += uninstall_from_router(svc)
        except Exception as exc:
            logger.warning('Internet Doctor removal failed: %s', exc)
        return removed
    return undo


def _wrap_render_undo(original):
    @wraps(original)
    def render_undo(original_state, v7, guarded=True):
        text = original(original_state, v7, guarded)
        cmds = removal_commands()
        if guarded:
            cmds = [f':do {{ {c} }} on-error={{}}' for c in cmds]
        return '\n'.join(cmds) + '\n' + text
    return render_undo


def _wrap_render_script(original):
    @wraps(original)
    def render_script(plan, cfg, facts, business_name=''):
        text = original(plan, cfg, facts, business_name)
        lines = plan.get('doctor_lines') or lines_from_cfg(cfg)
        if not lines:
            return text
        return text + '\n# 9. Internet Doctor (checks every line each minute and fixes a modem that holds the router back)\n' \
            + render_install_rsc(lines, router_name=business_name)
    return render_script


# ═══════════════════════════ Fit ═══════════════════════════
def t_internet_doctor(ctx, router=''):
    from django.urls import reverse
    from .fit_tools import _router, ToolError
    from .linkops import uses_link
    from .mikrotik import MikroTikService
    from .models import WanSetup
    r = _router(ctx, router)
    page = reverse('wan_designer', args=[r.pk])
    if uses_link(r):
        raise ToolError(f'{r.name} is on TapTap Link without a live tunnel, so a live check is not possible. '
                        f'The Internet Doctor on the router keeps working by itself; its script is on {page}.')
    setup = WanSetup.objects.filter(router=r).first()
    svc = MikroTikService(r).connect()
    try:
        out = diagnose(svc, lines_for(svc, setup))
        out['doctor_installed'] = installed(svc)
        out['doctor_status'] = status_line(svc)
        out['recent'] = recent_activity(svc, 6)
    finally:
        svc.close()
    out['router'] = r.name
    out['fix_page'] = page
    out['note'] = ('Every action is a button in Internet lines › Internet Doctor: switch a line on/off, put it in use or take it out, '
                   'new MAC, renew, original MAC, and turning the Doctor on. state=holding means the modem answers but the internet '
                   'does not get through — usually fixed by a new MAC. use.state: managed = customers use it, plain = only an emergency '
                   'DHCP route, out = not used.')
    return out


def install():
    """Called from CoreConfig.ready(): hook the Doctor into Internet Lines and Fit."""
    global _INSTALLED
    if _INSTALLED:
        return
    from . import wan
    wan.build_plan = _wrap_build_plan(wan.build_plan)
    wan.apply_plan = _wrap_apply_plan(wan.apply_plan)
    wan.undo = _wrap_undo(wan.undo)
    wan.render_undo = _wrap_render_undo(wan.render_undo)
    wan.render_script = _wrap_render_script(wan.render_script)
    try:
        from . import fit_tools
        fit_tools.TOOLS['internet_doctor'] = (
            fit_tools.NETWORK_PERMS, t_internet_doctor,
            'Live check of every internet line on a router: does the modem answer, and do 3 independent internet '
            'addresses get through each line? Finds "modem answers but no internet" (state=holding, e.g. a Starlink '
            'holding back the MikroTik) and says which fix to use. Read only.',
            fit_tools._obj({'router': {'type': 'string', 'description': 'Router name; optional when there is only one'}}))
    except Exception as exc:  # Fit is optional
        logger.info('Internet Doctor: Fit tool not registered (%s)', exc)
    _INSTALLED = True
