"""Phones always find the login page — their "is there internet?" test never gets through without a login.

What people saw
---------------
"When a customer's voucher finishes and they want to log in again, the login page sometimes refuses to come."

A phone decides whether to show its login sheet with a tiny test request (Android: connectivitycheck.gstatic.com/
generate_204, Apple: captive.apple.com, Windows: msftconnecttest.com …). If that test gets an answer from the
internet, the phone believes it is online and never shows a login page — the customer just sees nothing loading.
Free-access websites (Settings › Free websites) include their sub-domains: allowing "google.com", "gstatic.com" or
"apple.com" for free let those tests through without a login.

What this does
--------------
Puts "requires login" (walled-garden action=deny) rules for every known connection test at the TOP of the router's
walled garden, above any free-access rule, so a phone without a valid voucher always gets its login sheet. Where a
test shares its host with a real site (www.google.com/generate_204), only the test path is held back: the site
itself stays free. Applied with every portal install, after free-access changes, and once to every router by the
live housekeeping task. TapTap manages only rows whose comment starts with ``TapTap-probe``.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger('taptap.probe')

VERSION = 1
COMMENT = 'TapTap-probe'
# (host, path pattern) — an empty path holds back the whole host (it exists only for connection tests)
PROBES = [
    ('connectivitycheck.gstatic.com', ''), ('connectivitycheck.android.com', ''), ('clients1.google.com', '*generate_204*'),
    ('clients3.google.com', '*generate_204*'), ('www.google.com', '*generate_204*'), ('www.google.com', '*gen_204*'),
    ('play.googleapis.com', '*generate_204*'), ('www.gstatic.com', '*generate_204*'), ('captive.apple.com', ''),
    ('www.apple.com', '*library/test/success.html*'), ('www.msftconnecttest.com', ''), ('ipv6.msftconnecttest.com', ''),
    ('www.msftncsi.com', ''), ('detectportal.firefox.com', ''), ('connectivity-check.ubuntu.com', ''), ('nmcheck.gnome.org', ''),
    ('network-test.debian.org', ''), ('connect.rom.miui.com', ''), ('connectivitycheck.platform.hicloud.com', ''),
    ('conn1.oppomobile.com', ''), ('conn2.oppomobile.com', ''), ('wifi.vivo.com.cn', ''), ('www.qualcomm.cn', '*generate_204*'),
    ('cp.cloudflare.com', '*generate_204*'),
]
# Free-access entries that would let a test through (the owner is told; the guard keeps the tests out anyway)
TEST_DOMAINS = ('google.com', 'gstatic.com', 'googleapis.com', 'apple.com', 'msftconnecttest.com', 'msftncsi.com',
                'firefox.com', 'ubuntu.com', 'gnome.org', 'miui.com', 'hicloud.com', 'oppomobile.com', 'vivo.com.cn',
                'qualcomm.cn', 'cloudflare.com', 'android.com')


def script():
    """RouterOS: replace TapTap's connection-test rules, each placed above every other walled-garden rule.
    Full menu paths only, so it can be appended to any other script without changing its menu context."""
    items = ';'.join(f'"{h}|{p}"' for h, p in PROBES)
    wg = '/ip hotspot walled-garden'
    add = wg + ' add action=deny dst-host=$h comment="' + COMMENT + ': phone connection test (keeps the login page working)"'
    return (':do { ' + wg + ' remove [find where comment~"^' + COMMENT + '"] } on-error={}; '
            ':foreach e in={' + items + '} do={ :local i [:find $e "|"]; :local h [:pick $e 0 $i]; :local pa [:pick $e ($i + 1) [:len $e]]; '
            ':local f ([' + wg + ' find]->0); :do { '
            ':if ([:len $pa] > 0) do={ :if ([:typeof $f] = "id") do={ ' + add + ' path=$pa place-before=$f } else={ ' + add + ' path=$pa } } '
            'else={ :if ([:typeof $f] = "id") do={ ' + add + ' place-before=$f } else={ ' + add + ' } } '
            '} on-error={ :log warning ("TapTap: could not add the connection-test rule for " . $h) } }; '
            ':log info "TapTap: phone connection tests kept behind the login page"')


def covers_test(host, include_subdomains=True):
    """Would a free-access entry for ``host`` let a phone's connection test through? (for a friendly note)"""
    host = str(host or '').lower().strip('.')
    if any(host == h for h, _ in PROBES):
        return True
    return include_subdomains and any(d == host or d.endswith('.' + host) for d in TEST_DOMAINS)


def apply(router, user=None):
    """Put the rules on one router now (Direct API: run the script; TapTap Link: a command). (ok, message)"""
    try:
        from .linkops import uses_link
        if uses_link(router):
            from . import agent as link
            link.queue(router, 'probe_guard', {'v': VERSION}, label='Keep phone connection tests behind the login page', user=user, minutes=24 * 60)
            return True, f'{router.name}: queued through TapTap Link'
        from .portal_deploy import _run_script
        _run_script(router, script())
        return True, f'{router.name}: applied'
    except Exception as exc:                                   # noqa: BLE001 — offline router, no hotspot…
        return False, f'{router.name}: {exc}'


def ensure_all(limit=20):
    """Housekeeping: every router gets the rules once per VERSION (retried daily if it failed)."""
    from .models import Router
    done = 0
    for r in Router.objects.select_related('business').filter(status='Online').order_by('pk'):
        if done >= limit:
            break
        key = f'tt:probe-guard:{r.pk}:v{VERSION}'
        if cache.get(key):
            continue
        try:
            if not r.business.has_access:
                continue
        except Exception:
            continue
        ok, msg = apply(r)
        cache.set(key, 1, 30 * 86400 if ok else 86400)
        done += 1
        logger.info('probe guard %s', msg)
    return done


def install():
    """Register the TapTap Link command (core/agent SAFE_KINDS / command_body)."""
    from . import agent as link
    link.SAFE_KINDS.add('probe_guard')
    link.DEFAULT_EXPIRY.setdefault('probe_guard', 24 * 60)
    original = link.command_body
    if getattr(original, '_probe_guard', False):
        return

    def command_body(cmd):
        return script() if cmd.kind == 'probe_guard' else original(cmd)
    command_body._probe_guard = True
    link.command_body = command_body
