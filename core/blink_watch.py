"""Blink watch — why the internet goes off for a few seconds and comes back.

Short drops are the hardest problems on a hotspot: by the time anyone looks, everything works again. Two parts:

1. **The watch, ON the router** (/system script + scheduler ``TapTap-Watch``, every 5 seconds). It pings three
   public addresses that are NOT any line's health-check or Doctor probe address (so it measures what customers get
   through whichever line is in use), and tries a DNS lookup when the pings fail (some providers block ping). When the
   internet comes back after at least one miss it writes one line to the router's log and keeps it in a small
   history that survives a busy log:

       TapTap Watch: internet was out ~15s from 18:02:11 (cpu 12% routes 192.168.1.1%ether1 -> routes 192.168.8.1%ether2)

   A restart of the router is noted too ("TapTap Watch: started (router uptime 00:00:41)"). It costs one ping every
   5 seconds. It is installed by the "Why the internet blinks" test and together with the Internet Doctor.

2. **The detective, in TapTap** (Network tools → Why the internet blinks; Fit's ``run_network_test kind=blinks``).
   It reads the watch's history and the router's own log — ports going down and up, DHCP addresses lost, PPPoE
   sessions dropped, restarts and power cuts, loops, line switches, the Internet Doctor's fixes, many customers
   signed out at the same minute — puts every blink next to what happened at that moment, and says in plain words
   what causes them and what to do.
"""
from __future__ import annotations

import re
from collections import OrderedDict

NAME = 'TapTap-Watch'
TAG = 'TapTap Watch'
EVERY = 5                                   # seconds between two checks on the router
HOSTS = ('1.1.1.2', '1.0.0.2', '9.9.9.11')  # not in wan.CHECK_HOSTS / wan.DNS_SERVERS / wan_doctor.PROBE_POOL
LOG_LINES = 120                             # log lines a Link router sends (the upload is limited to 32 kB)
HIST_MAX = 2400                             # characters of history kept on the router

SOURCE = r''':global ttwN
:global ttwSince
:global ttwCtx
:global ttwHist
:if ([:typeof $ttwN] != "num") do={
  :set ttwN 0
  :if ([:typeof $ttwHist] != "str") do={ :set ttwHist "" }
  :local up ""
  :do { :set up [:tostr [/system resource get uptime]] } on-error={}
  :log warning ("TapTap Watch: started (router uptime " . $up . ")")
}
:local ok false
:foreach a in={"1.1.1.2";"1.0.0.2";"9.9.9.11"} do={
  :if (!$ok) do={ :do { :if ([/ping address=$a count=1] > 0) do={ :set ok true } } on-error={} }
}
:if (!$ok) do={
  # some providers block ping: a name looked up straight at a resolver proves the internet works too
  :do { :local q [:parse ":return [:resolve domain-name=\"cloudflare.com\" server=1.1.1.2]"]; :local x [$q]; :if ([:len [:tostr $x]] > 0) do={ :set ok true } } on-error={}
}
:local rt ""
:do { :foreach r in=[/ip route find where dst-address=0.0.0.0/0 and active] do={ :set rt ($rt . [:tostr [/ip route get $r gateway]] . " ") } } on-error={}
:local cpu ""
:do { :set cpu [:tostr [/system resource get cpu-load]] } on-error={}
:if ($ok) do={
  :if ($ttwN > 0) do={
    :local secs ($ttwN * 5)
    :local day ""
    :do { :set day [:tostr [/system clock get date]] } on-error={}
    :log warning ("TapTap Watch: internet was out ~" . $secs . "s from " . $ttwSince . " (" . $ttwCtx . " -> routes " . $rt . ")")
    :set ttwHist ($day . " " . $ttwSince . "|" . $secs . "|" . $ttwCtx . "|" . $rt . ";" . $ttwHist)
    :if ([:len $ttwHist] > 2400) do={ :set ttwHist [:pick $ttwHist 0 2400] }
  }
  :set ttwN 0
} else={
  :if ($ttwN = 0) do={
    :set ttwSince [:tostr [/system clock get time]]
    :set ttwCtx ("cpu " . $cpu . "% routes " . $rt)
  }
  :set ttwN ($ttwN + 1)
  :if ($ttwN = 60) do={ :log warning ("TapTap Watch: internet out for 5 min since " . $ttwSince) }
}
'''


def _lit(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\r', '').replace('\n', '\\n') + '"'


def removal_commands():
    return [f'/system scheduler remove [find where name="{NAME}"]', f'/system script remove [find where name="{NAME}"]']


def install_commands():
    """RouterOS commands that (re)install the watch. Its history is kept across a reinstall."""
    return [f':do {{ {c} }} on-error={{}}' for c in removal_commands()] + [
        f'/system script add name={NAME} policy=read,write,test comment="{TAG} - notes every short internet drop" source={_lit(SOURCE)}',
        f'/system scheduler add name={NAME} interval={EVERY}s policy=read,write,test on-event="/system script run {NAME}" '
        f'comment="{TAG} - checks the internet every {EVERY} seconds"',
    ]


def link_install_snippet():
    """For a Link script: install the watch when it is missing (one line)."""
    body = '; '.join(install_commands())
    return f':if ([:len [/system scheduler find where name="{NAME}"]] = 0) do={{ {body}; :set out ($out . "watch=new\\n") }} else={{ :set out ($out . "watch=on\\n") }}; '


def install_api(svc):
    """Install over the API when missing. Returns 'new' or 'on'."""
    sched = svc.resource('/system/scheduler')
    if any(str(r.get('name')) == NAME for r in sched.get()):
        return 'on'
    scripts = svc.resource('/system/script')
    for r in scripts.get():
        if str(r.get('name')) == NAME:
            scripts.remove(id=r['id'])
    scripts.add(name=NAME, policy='read,write,test', comment=f'{TAG} - notes every short internet drop', source=SOURCE)
    sched.add(name=NAME, interval=f'{EVERY}s', policy='read,write,test', on_event=f'/system script run {NAME}',
              comment=f'{TAG} - checks the internet every {EVERY} seconds')
    return 'new'


# ─────────────────────────────── reading ───────────────────────────────

LOG_RE = re.compile(r'link (down|up)|lost IP|got IP|pppoe|rebooted|power outage|loop|TapTap|logged out|disconnected|'
                    r'terminating|dhcp', re.I)


def keep(message):
    """Is this log line worth sending / reading?"""
    return bool(LOG_RE.search(str(message or '')))


def read_api(svc):
    """(log entries [(time, topics, message)], history text, facts) straight from the router."""
    entries = [(str(r.get('time', '')), str(r.get('topics', '')), str(r.get('message', ''))) for r in svc.resource('/log').get()
               if keep(r.get('message'))]
    hist, n = '', 0
    for r in svc.safe_get('/system/script/environment'):
        if str(r.get('name')) == 'ttwHist':
            hist = str(r.get('value') or '')
        if str(r.get('name')) == 'ttwN':
            try:
                n = int(str(r.get('value') or 0))
            except ValueError:
                n = 0
    res = (svc.safe_get('/system/resource') or [{}])[0]
    wans = {str(r.get('interface')) for r in svc.safe_get('/ip/dhcp-client') if r.get('interface')}
    wans |= {str(r.get('name')) for r in svc.safe_get('/interface/pppoe-client') if r.get('name')}
    wans |= {str(r.get('interface')) for r in svc.safe_get('/interface/pppoe-client') if r.get('interface')}
    return entries[-400:], hist, {'uptime': str(res.get('uptime', '')), 'cpu': str(res.get('cpu-load', '')), 'wans': sorted(wans),
                                  'down_now': n}


def link_body():
    """Link script: install the watch if needed, then send its history, the facts and the useful log lines."""
    return (link_install_snippet()
            + ':global ttwHist; :global ttwN; '
            ':do { :set out ($out . "hist=" . [:tostr $ttwHist] . "\\n") } on-error={}; '
            ':do { :set out ($out . "downnow=" . [:tostr $ttwN] . "\\n") } on-error={}; '
            ':do { :set out ($out . "up=" . [:tostr [/system resource get uptime]] . "\\n" . "cpu=" . [:tostr [/system resource get cpu-load]] . "\\n") } on-error={}; '
            ':do { :foreach c in=[/ip dhcp-client find] do={ :set out ($out . "wan=" . [:tostr [/ip dhcp-client get $c interface]] . "\\n") } } on-error={}; '
            ':do { :foreach c in=[/interface pppoe-client find] do={ :set out ($out . "wan=" . [:tostr [/interface pppoe-client get $c name]] . "\\n" . '
            '"wan=" . [:tostr [/interface pppoe-client get $c interface]] . "\\n") } } on-error={}; '
            ':local rows [:toarray ""]; '
            ':do { :set rows [/log print as-value where (message~"link down" or message~"link up" or message~"lost IP" or message~"got IP" '
            'or message~"pppoe" or message~"rebooted" or message~"power outage" or message~"loop" or message~"TapTap" or message~"logged out" '
            'or message~"disconnected" or message~"terminating")] } on-error={ :set out ($out . "err=log\\n") }; '
            f':local n [:len $rows]; :local s 0; :if ($n > {LOG_LINES}) do={{ :set s ($n - {LOG_LINES}) }}; '
            ':if ($n > 0) do={ :for i from=$s to=($n - 1) do={ :do { :local e ($rows->$i); :local m [:tostr ($e->"message")]; '
            ':if ([:len $m] > 140) do={ :set m [:pick $m 0 140] }; '
            ':set out ($out . "l=" . ($e->"time") . "|" . [:tostr ($e->"topics")] . "|" . $m . "\\n") } on-error={} } }; ')


def parse_link(lines):
    """[(key, value)] from a Link upload → (entries, hist, facts, watch)."""
    entries, wans, facts = [], set(), {}
    hist, watch = '', ''
    for k, v in lines:
        if k == 'l':
            t, _, rest = v.partition('|')
            topics, _, msg = rest.partition('|')
            entries.append((t, topics, msg))
        elif k == 'hist':
            hist = v
        elif k == 'wan' and v:
            wans.add(v)
        elif k == 'up':
            facts['uptime'] = v
        elif k == 'cpu':
            facts['cpu'] = v
        elif k == 'downnow':
            try:
                facts['down_now'] = int(v or 0)
            except ValueError:
                facts['down_now'] = 0
        elif k == 'watch':
            watch = v
    facts['wans'] = sorted(wans)
    return entries, hist, facts, watch


# ─────────────────────────────── the detective ───────────────────────────────

TIME_RE = re.compile(r'(\d{1,2}):(\d{2}):(\d{2})')
CAUSES = OrderedDict([
    ('reboot', ('The router restarted', 'danger',
                'Every restart cuts everyone for a minute or two. “Without proper shutdown” means the power went off: check its '
                'adapter, the socket, the UPS. If nobody restarted it, look at its temperature and power supply.')),
    ('wan_port', ('A modem’s cable or power', 'danger',
                  'The port to an internet modem went down and up. Check that modem’s power (a weak adapter or a socket that moves) '
                  'and the cable and its plugs; try another port or cable.')),
    ('lan_port', ('A port on your own network (the AP chain)', 'warn',
                  'A port that feeds your access points went down and up. In a chain of routers / APs one restart or one loose cable '
                  'cuts everyone after it: check the power (PoE injectors, adapters) and cables of the first device on that port.')),
    ('dhcp', ('The modem took back the address', 'warn',
              'The modem stopped giving the router its address for a moment — the modem itself restarted or its DHCP renewed. '
              'If it happens often, restart the modem once, check its power, or ask the provider.')),
    ('pppoe', ('The provider’s PPPoE session dropped', 'warn', 'The provider ended the session. Ask them; check the line and its cable.')),
    ('loop', ('A loop in the network', 'danger',
              'The router saw its own packets come back — two cables (or a Wi-Fi bridge and a cable) close a circle. Remove the '
              'extra link, or turn on loop protection / RSTP on the bridge.')),
    ('switch', ('The router switched internet line', 'info',
                'One line failed and the router moved everyone to the other — connections restart, so people see a short drop. '
                'The blink is the line itself; see which line in the list.')),
    ('doctor', ('Internet Doctor repaired a line', 'info',
                'The Doctor renewed a line or gave it a new MAC because the internet did not get through it. The blink comes from that '
                'line; the repair ends it.')),
    ('cpu', ('The router was overloaded', 'warn',
             'The router’s CPU was at 90 % or more when the internet stopped. Look at Traffic for heavy users, lower fair-usage '
             'limits, or upgrade the router.')),
    ('logout', ('Many customers signed out at once', 'warn', 'Something removed many sessions in the same minute — see Why people drop.')),
    ('isp', ('The provider itself', 'info',
             'Nothing changed on your router or cables: the modem stayed connected and the internet behind it stopped for a few '
             'seconds (Starlink and mobile lines do this when the signal drops — obstructions, weather, a busy cell). With two '
             'lines, the Internet lines page can switch automatically; tell the provider the times listed here.')),
])


def _gws(text):
    """The gateways in a 'cpu 12% 192.168.1.1%ether1 10.0.0.1%ether2' text."""
    return {t for t in str(text or '').split() if re.match(r'^[\w.:-]+%[\w.-]+$', t) or re.match(r'^\d+\.\d+\.\d+\.\d+$', t)}


def _secs_of_day(text):
    m = TIME_RE.search(str(text or ''))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _hist_rows(hist):
    out = []
    for part in str(hist or '').split(';'):
        bits = part.split('|')
        if len(bits) < 2 or not bits[0].strip():
            continue
        try:
            secs = int(bits[1])
        except ValueError:
            continue
        ctx = bits[2] if len(bits) > 2 else ''
        cpu = re.search(r'cpu (\d+)%', ctx)
        out.append({'at': bits[0].strip()[:30], 'secs': secs, 'before': ctx.replace('routes ', '').strip()[:120],
                    'after': (bits[3] if len(bits) > 3 else '').strip()[:120], 'cpu': int(cpu.group(1)) if cpu else None})
    return out


def _events(entries, wans):
    """Log lines → events [{'at', 't', 'key', 'text'}]."""
    ev, logouts = [], {}
    for when, topics, msg in entries:
        m = str(msg or '')
        t = _secs_of_day(when)
        low = m.lower()
        key = text = None
        hit = re.search(r'^(\S+) link (down|up)', m)
        if hit:
            iface = hit.group(1)
            key = 'wan_port' if iface in wans else 'lan_port'
            text = f'{iface} link {hit.group(2)}' + (' (internet line)' if iface in wans else '')
            if hit.group(2) == 'up':
                key = key + '_up'
        elif 'lost ip' in low:
            key, text = 'dhcp', m[:120]
        elif 'pppoe' in low and ('disconnected' in low or 'terminating' in low):
            key, text = 'pppoe', m[:120]
        elif 'without proper shutdown' in low or 'power outage' in low:
            key, text = 'reboot', 'Router restarted after a power cut'
        elif 'rebooted' in low:
            key, text = 'reboot', 'Router restarted'
        elif m.startswith('TapTap Watch: started'):
            up = re.search(r'uptime ([^)]*)\)', m)
            from .router_schedules import uptime_seconds
            secs = uptime_seconds(up.group(1)) if up else None
            if secs is not None and secs < 300:              # started with the router, not installed on a running one
                key, text = 'reboot', f'Router restarted (up {up.group(1)} when the watch started)'
            else:
                continue
        elif 'loop' in low and ('bridge' in low or 'own address' in low or 'detected' in low):
            key, text = 'loop', m[:120]
        elif 'internet route changed' in low or 'taken out of use' in low or 'back in use' in low or 'put back in use' in low:
            key, text = 'switch', m.replace('TapTap Doctor: ', 'Doctor: ')[:140]
        elif m.startswith('TapTap Doctor:') and ('renewing' in low or 'new mac' in low or 'original mac' in low):
            key, text = 'doctor', m.replace('TapTap Doctor: ', 'Doctor: ')[:140]
        elif 'logged out' in low and 'hotspot' in str(topics).lower():
            if t is not None:
                logouts.setdefault(t // 60, []).append(when)
            continue
        if key:
            ev.append({'at': str(when)[:30], 't': t, 'key': key, 'text': text})
    for minute, rows in logouts.items():
        if len(rows) >= 5:
            ev.append({'at': str(rows[0])[:30], 't': minute * 60, 'key': 'logout', 'text': f'{len(rows)} customers signed out in the same minute'})
    ev.sort(key=lambda e: e['t'] if e['t'] is not None else -1)
    return ev


def blinks_result(entries, hist, facts, watch=''):
    """Everything above → a verdict: blinks, their causes ranked, the timeline, what to do."""
    wans = set(facts.get('wans') or [])
    events = _events(entries, wans)
    blinks = _hist_rows(hist)
    if not blinks:                                            # an older history, or only the log has them
        for when, topics, msg in entries:
            m = re.search(r'TapTap Watch: internet was out ~(\d+)s from (\S+) \((.*?) -> routes (.*)\)', str(msg))
            if m:
                cpu = re.search(r'cpu (\d+)%', m.group(3))
                blinks.insert(0, {'at': m.group(2), 'secs': int(m.group(1)), 'before': m.group(3).replace('routes ', '')[:120],
                                  'after': m.group(4)[:120], 'cpu': int(cpu.group(1)) if cpu else None})
    counts = {}
    for b in blinks:
        start = _secs_of_day(b['at'])
        near = []
        if start is not None:
            lo, hi = start - 75, start + b['secs'] + 75
            near = [e for e in events if e['t'] is not None and lo <= e['t'] <= hi and not e['key'].endswith('_up')]
        keys = [e['key'] for e in near]
        g0, g1 = _gws(b.get('before')), _gws(b.get('after'))
        if g0 and g1 and g0 != g1 and 'switch' not in keys:
            keys.append('switch')
        if b.get('cpu') is not None and b['cpu'] >= 90:
            keys.append('cpu')
        order = list(CAUSES)
        cause = min(keys, key=order.index) if keys else 'isp'
        b['cause'] = cause
        b['cause_label'] = CAUSES[cause][0]
        b['seen'] = [e['text'] for e in near][:4]
        counts[cause] = counts.get(cause, 0) + 1
    # things in the log even without a measured blink (before the watch was on, or longer than the log)
    flaps = {}
    for e in events:
        if e['key'] in ('wan_port', 'lan_port'):
            iface = e['text'].split(' ')[0]
            flaps[iface] = flaps.get(iface, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], list(CAUSES).index(kv[0])))
    causes = [{'key': k, 'label': CAUSES[k][0], 'count': n, 'level': CAUSES[k][1], 'hint': CAUSES[k][2]} for k, n in ranked]
    for k in ('reboot', 'loop', 'wan_port', 'lan_port', 'dhcp', 'pppoe'):
        n = sum(1 for e in events if e['key'] == k)
        if n and k not in counts:
            causes.append({'key': k, 'label': CAUSES[k][0], 'count': 0, 'in_log': n, 'level': CAUSES[k][1], 'hint': CAUSES[k][2]})
    total = sum(b['secs'] for b in blinks)
    down_now = int(facts.get('down_now') or 0)
    if down_now >= 60 and not blinks:
        title, level = 'The watch cannot reach the internet from this router', 'bad'
        note = ('Either the internet is down right now, or this provider blocks the test addresses. Run the Internet check '
                'at the top of the page.')
    elif blinks:
        top = causes[0]
        title = f'{len(blinks)} blink{"s" if len(blinks) != 1 else ""} recorded ({total} s in all) — mostly: {top["label"].lower()}'
        level = 'bad' if top['level'] == 'danger' else 'warn'
        note = top['hint']
    elif any(c.get('in_log') for c in causes):
        c = next(c for c in causes if c.get('in_log'))
        title, level = f'No blink measured yet, but the log shows: {c["label"].lower()} ({c["in_log"]}×)', 'warn'
        note = c['hint']
    else:
        title, level = ('Watching for blinks now' if watch == 'new' else 'No blink recorded'), 'ok'
        note = ('TapTap just started a small watch on this router: every short drop is noted from now on, with what happened at that '
                'moment. Run this again after the next blink.' if watch == 'new' else
                'The internet has not stopped since the watch started (it checks every 5 seconds). Run this again after the next blink.')
    timeline = [{'at': e['at'], 'key': e['key'].replace('_up', ''), 'text': e['text']} for e in events][-40:][::-1]
    return {'title': title, 'level': level, 'note': note, 'blinks': blinks[:30], 'count': len(blinks), 'total_secs': total,
            'longest': max((b['secs'] for b in blinks), default=0), 'causes': causes, 'timeline': timeline,
            'flaps': [{'interface': k, 'count': v, 'wan': k in wans} for k, v in sorted(flaps.items(), key=lambda kv: -kv[1])],
            'watch': watch or 'on', 'uptime': facts.get('uptime', ''), 'cpu': facts.get('cpu', ''), 'wans': sorted(wans)}
