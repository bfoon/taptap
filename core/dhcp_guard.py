"""Internet lines never get stuck on "renewing..." — the router's own DHCP talk with its modems always gets through.

What people saw
---------------
"ether5 stays on renewing... for hours, even after a reboot." The line had its address and worked, but TapTap and
the Internet Doctor treated it as not connected: the Doctor took its routes out of use, and "Share the load" left it
out, so it carried 0 % of the traffic.

Why a renewal is never answered
-------------------------------
A lease is first obtained with broadcasts, which skip the firewall. Half-way through the lease, RouterOS renews
it with an ordinary unicast packet to the modem (udp 68 → 67), and the modem answers by unicast too. On a router
with several lines, that renewal goes through every firewall table:
  * mangle — a line mark (TapTap's per-line marks, or a hand-made one such as "starlink2") gives the packet a
    routing table whose route can point out of ANOTHER line: the renewal leaves through the wrong port and
    nobody answers it. Once the Doctor switches the line's own routes off, its table sends everything to the
    other line, so it never recovers — not even after a reboot;
  * nat — the router's own packet is masqueraded behind the other line's address;
  * filter — the modem's unicast answer meets "drop everything from the WAN" before connection tracking
    knows it as a reply.

The fix
-------
A few rules at the TOP of each table, for the router's own DHCP client traffic only (udp 68 → 67 out, 67 → 68 in):
no marks, no NAT, and the modem's answers accepted. Old tracked connections that still carry a wrong mark are
dropped, and every client stuck on renewing or rebinding is asked to renew at once. Rows carry the comment
``TapTap DHCP``; nothing else is touched, and the rules change nothing for customers' traffic.

Applied: once to every router where TapTap runs the internet lines, at once when a line is seen stuck on renewing
(Internet lines page), and on demand. Direct API: a short script; TapTap Link: a command.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger('taptap.dhcp_guard')

VERSION = 1
COMMENT = 'TapTap DHCP'
KIND = 'dhcp_guard'
STUCK = ('renew', 'rebind')

# (menu, fields) — each one is placed above every other (non-dynamic) row of its table
RULES = (
    ('/ip firewall mangle', 'chain=prerouting protocol=udp src-port=67 dst-port=68 action=accept',
     'answers from the modems keep no line mark'),
    ('/ip firewall mangle', 'chain=output protocol=udp src-port=68 dst-port=67 action=accept',
     'renewals leave through their own port'),
    ('/ip firewall nat', 'chain=srcnat protocol=udp src-port=68 dst-port=67 src-address-type=local action=accept',
     'renewals keep their own address'),
    ('/ip firewall filter', 'chain=input protocol=udp src-port=67 dst-port=68 action=accept',
     'answers from the modems get in'),
)


def is_stuck(status):
    return str(status or '').strip().lower().startswith(STUCK)


def script():
    """RouterOS script: the guard rules on top of their tables, stale DHCP connections gone, stuck clients renewed.
    Full menu paths only, so it can run on its own or appended to another script."""
    out = []
    for menu in sorted({m for m, _f, _n in RULES}):
        out.append(f':do {{ {menu} remove [find where comment~"^{COMMENT}"] }} on-error={{}}')
    for menu, fields, note in RULES:
        add = f'{menu} add {fields} comment="{COMMENT}: {note}"'
        out.append(f':do {{ :local f [{menu} find where dynamic=no]; '
                   f':if ([:len $f] > 0) do={{ {add} place-before=[:pick $f 0] }} else={{ {add} }} '
                   f'}} on-error={{ :log warning "TapTap: could not add a DHCP rule in {menu}" }}')
    # tracked DHCP connections from before keep their old mark / NAT: drop them, the next packet starts clean
    out.append(':do { /ip firewall connection remove [find where protocol="udp" and (dst-address~":67\\$" or dst-address~":68\\$")] } '
               'on-error={}')
    out.append(':foreach c in=[/ip dhcp-client find where disabled=no] do={ :local st [:tostr [/ip dhcp-client get $c status]]; '
               ':local stuck false; :if ([:pick $st 0 5] = "renew") do={ :set stuck true }; :if ([:pick $st 0 6] = "rebind") do={ :set stuck true }; '
               ':if ($stuck) do={ :do { /ip dhcp-client renew $c } on-error={}; '
               ':log warning ("TapTap: DHCP on " . [/ip dhcp-client get $c interface] . " was stuck on " . $st . " - renewing again") } }')
    out.append(':log info "TapTap: the router\'s own DHCP traffic with its modems is never marked, NATed or blocked"')
    return '; '.join(out)


def apply(router, user=None):
    """Put the guard on one router now. (ok, message)"""
    try:
        from .linkops import uses_link
        if uses_link(router):
            from . import agent as link
            link.queue(router, KIND, {'v': VERSION}, label='Keep the internet lines’ DHCP renewals working', user=user, minutes=24 * 60)
            return True, f'{router.name}: queued through TapTap Link'
        from .portal_deploy import _run_script
        _run_script(router, script())
        return True, f'{router.name}: applied'
    except Exception as exc:                                   # noqa: BLE001 — offline router, no permission…
        return False, f'{router.name}: {exc}'


def nudge(router, line_list, user=None):
    """A line is stuck on renewing / rebinding: apply the guard now (at most once an hour per router)."""
    stuck = [l for l in line_list or [] if l.get('type') == 'dhcp' and not l.get('client_off') and is_stuck(l.get('status'))]
    if not stuck or not cache.add(f'tt:dhcp-guard:nudge:{router.pk}', 1, 3600):
        return None
    ok, msg = apply(router, user)
    if ok:
        cache.set(f'tt:dhcp-guard:{router.pk}:v{VERSION}', 1, 30 * 86400)
    logger.warning('dhcp guard for stuck %s: %s', ', '.join(l['interface'] for l in stuck), msg)
    return ok


def _wanted(router):
    """Routers where TapTap runs the internet lines (its marks and tables are what a renewal can fall into)."""
    try:
        from .models import WanSetup
        return WanSetup.objects.filter(router=router, status__in=('active', 'pending')).exists()
    except Exception:
        return False


def ensure_all(limit=20):
    """Housekeeping: every router whose lines TapTap runs gets the guard once per VERSION (retried daily)."""
    from .models import Router
    done = 0
    for r in Router.objects.select_related('business').filter(status='Online').order_by('pk'):
        if done >= limit:
            break
        key = f'tt:dhcp-guard:{r.pk}:v{VERSION}'
        if cache.get(key) or not _wanted(r):
            continue
        try:
            if not r.business.has_access:
                continue
        except Exception:
            continue
        ok, msg = apply(r)
        cache.set(key, 1, 30 * 86400 if ok else 86400)
        done += 1
        logger.info('dhcp guard %s', msg)
    return done


def install():
    """Register the TapTap Link command (core/agent SAFE_KINDS / command_body)."""
    from . import agent as link
    link.SAFE_KINDS.add(KIND)
    link.DEFAULT_EXPIRY.setdefault(KIND, 24 * 60)
    original = link.command_body
    if getattr(original, '_dhcp_guard', False):
        return

    def command_body(cmd):
        return script() if cmd.kind == KIND else original(cmd)
    command_body._dhcp_guard = True
    link.command_body = command_body
