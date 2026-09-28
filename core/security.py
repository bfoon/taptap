"""TapTap Security Center audit engine.

Works from data TapTap already stores (config snapshot, discovery tables,
vouchers, sync jobs) so the page renders instantly and never blocks on a
router. "Rescan" refreshes a router's snapshot in one bounded request.

Every finding has a stable ``key`` (so it can be acknowledged), a severity,
plain-language impact, evidence, a manual RouterOS command and — only when
it is provably safe for TapTap's own access — a one-click ``fix`` key.
"""
import ipaddress
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from .routeros_analysis import g, truthy, parse_version

SEVERITY_WEIGHT = {'critical': 25, 'high': 12, 'medium': 6, 'low': 2, 'info': 0}
SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info']
RISKY_SERVICES = {
    'telnet': ('high', 'Telnet sends the admin password in clear text.', 'svc-telnet'),
    'ftp': ('high', 'FTP sends credentials in clear text and exposes the router file system.', 'svc-ftp'),
    'www': ('medium', 'WebFig over plain HTTP exposes the admin login.', None),
    'winbox': ('medium', 'Winbox is the most attacked RouterOS service when reachable from the Internet.', None),
    'api': ('medium', 'The plain API exposes credentials to anyone on the path.', None),
    'ssh': ('low', 'SSH is safe with strong passwords but is brute-forced constantly if open.', None),
    'api-ssl': ('low', 'API-SSL is encrypted; restrict it to the TapTap server address.', None),
    'www-ssl': ('low', 'WebFig over HTTPS; restrict to management addresses.', None),
}


def _rows(snap, label):
    if not snap or not snap.sections:
        return []
    return (snap.sections.get(label) or {}).get('rows') or []


def _is_public(host):
    try:
        return ipaddress.ip_address(str(host).split(':')[0]).is_global
    except ValueError:
        return True  # DNS name — assume reachable over the Internet


def grade(score):
    return 'A' if score >= 90 else 'B' if score >= 75 else 'C' if score >= 60 else 'D' if score >= 40 else 'F'


class Audit:
    def __init__(self):
        self.findings = []

    def add(self, key, severity, category, title, impact, router=None, evidence=None, cli='', fix=None, link=None):
        self.findings.append({
            'key': key, 'severity': severity, 'category': category, 'title': title, 'impact': impact,
            'router_id': router.id if router else None, 'router_name': router.name if router else 'All routers',
            'evidence': [str(x) for x in (evidence or [])][:12], 'evidence_more': max(0, len(evidence or []) - 12),
            'cli': cli, 'fix': fix, 'link': link,
        })


def _wan_interfaces(router, snap):
    names = {l.get('interface') for l in ((snap.load_balancing if snap else {}) or {}).get('wan_links', []) if l.get('interface')}
    names |= set(router.interface_roles.filter(role='wan').values_list('interface_name', flat=True))
    return {n for n in names if n}


def _has_input_protection(filter_rows, wan_ifaces):
    """True when chain=input ends in a drop that covers the WAN side."""
    for row in filter_rows:
        if truthy(g(row, 'disabled', default='no')) or str(g(row, 'chain')) != 'input':
            continue
        if str(g(row, 'action')) not in {'drop', 'reject', 'tarpit'}:
            continue
        in_iface = str(g(row, 'in-interface')).lstrip('!')
        in_list = str(g(row, 'in-interface-list'))
        src = str(g(row, 'src-address')) or str(g(row, 'src-address-list'))
        state = str(g(row, 'connection-state'))
        negated = str(g(row, 'in-interface-list')).startswith('!') or str(g(row, 'in-interface')).startswith('!')
        if negated:  # "drop everything not coming from LAN" (default v7 config)
            return True
        if in_iface and in_iface in wan_ifaces:
            return True
        if in_list and in_list.upper() in {'WAN', 'INTERNET', 'ISP'}:
            return True
        if not in_iface and not in_list and not src and (not state or 'new' in state):
            return True  # blanket "drop everything else" at the end of input
    return False


def audit_router(audit, router, now):
    snap = None
    try:
        snap = router.config_snapshot
    except Exception:
        pass
    has_config = bool(snap and snap.sections and _rows(snap, 'IP services'))
    if not has_config:
        audit.add(f'r{router.id}:no-snapshot', 'info', 'Coverage', 'Router configuration not scanned yet',
                  'The hardening audit needs a configuration snapshot. Press Rescan (or run a Full Sync).', router)
        return
    wan_ifaces = _wan_interfaces(router, snap)
    filters = _rows(snap, 'Firewall filter')
    protected = _has_input_protection(filters, wan_ifaces)

    # ---- firewall input protection ----
    if not protected:
        audit.add(f'r{router.id}:input-open', 'critical', 'Router hardening',
                  'Router management is not firewalled from the Internet',
                  'No drop rule protects the input chain on the WAN side, so every enabled service '
                  '(Winbox, API, DNS, WebFig) is reachable by attackers worldwide.', router,
                  evidence=[f'WAN interfaces: {", ".join(sorted(wan_ifaces)) or "not detected"}',
                            f'{sum(1 for r in filters if g(r, "chain") == "input")} input rule(s) found'],
                  cli=('/ip firewall filter\n'
                       'add chain=input connection-state=established,related action=accept comment="TapTap: established"\n'
                       'add chain=input src-address=<TAPTAP_SERVER_IP> action=accept comment="TapTap: management"\n'
                       'add chain=input in-interface-list=!LAN action=drop comment="TapTap: drop WAN input"'),
                  link='router_control')

    # ---- IP services ----
    for row in _rows(snap, 'IP services'):
        name = str(g(row, 'name'))
        if name not in RISKY_SERVICES or truthy(g(row, 'disabled', default='no')):
            continue
        restricted = bool(str(g(row, 'address')).strip())
        severity, impact, fix = RISKY_SERVICES[name]
        if name in {'telnet', 'ftp'}:
            audit.add(f'r{router.id}:svc-{name}', severity, 'Router hardening', f'{name.upper()} service is enabled',
                      impact, router, evidence=[f'port {g(row, "port")}', 'restricted to ' + (g(row, 'address') or 'anyone')],
                      cli=f'/ip service disable {name}', fix=fix)
        elif not restricted:
            sev = severity if not protected else 'low'
            audit.add(f'r{router.id}:svc-{name}-open', sev, 'Router hardening',
                      f'{name} is open to any address', impact + (' The input firewall reduces the risk.' if protected else ''),
                      router, evidence=[f'port {g(row, "port")}', 'Available From: (empty)'],
                      cli=f'/ip service set {name} address=<YOUR_LAN>/24,<TAPTAP_SERVER_IP>/32')

    # ---- DNS open resolver ----
    dns = _rows(snap, 'DNS')
    if dns and truthy(g(dns[0], 'allow-remote-requests', default='no')) and not protected:
        audit.add(f'r{router.id}:dns-open', 'high', 'Router hardening', 'Open DNS resolver on the WAN',
                  'Remote DNS requests are allowed with no WAN firewall. Attackers use such routers for DDoS '
                  'amplification and your ISP link fills with junk traffic.', router,
                  cli='/ip firewall filter add chain=input in-interface-list=WAN protocol=udp dst-port=53 action=drop\n'
                      '/ip firewall filter add chain=input in-interface-list=WAN protocol=tcp dst-port=53 action=drop')

    # ---- RouterOS version ----
    resource = _rows(snap, 'System resources')
    version_text = str(g(resource[0], 'version')) if resource else ''
    version = parse_version(version_text)
    if version != (0, 0, 0):
        if version < (6, 42, 1):
            audit.add(f'r{router.id}:ros-critical', 'critical', 'Firmware', f'RouterOS {version_text} has a known remote exploit',
                      'Versions before 6.42.1 allow password theft through Winbox (CVE-2018-14847). Upgrade immediately.',
                      router, cli='/system package update install')
        elif version < (6, 49, 0):
            audit.add(f'r{router.id}:ros-old', 'high', 'Firmware', f'RouterOS {version_text} is outdated',
                      'Many security fixes landed in 6.49.x and v7. Upgrade to the latest long-term or stable release.',
                      router, cli='/system package update set channel=long-term\n/system package update install')
        elif version[0] == 6:
            audit.add(f'r{router.id}:ros-v6', 'low', 'Firmware', f'RouterOS {version_text} (v6 branch)',
                      'v6 only receives critical fixes. Plan a move to RouterOS v7 for WireGuard, newer Wi-Fi and fixes.', router)

    # ---- users ----
    users = _rows(snap, 'Users')
    if any(str(g(u, 'name')) == 'admin' and not truthy(g(u, 'disabled', default='no')) for u in users):
        audit.add(f'r{router.id}:user-admin', 'medium', 'Access control', 'Default "admin" account is active',
                  'The default username is the first one attackers try. Create a personal full-rights user, then disable admin.',
                  router, cli='/user add name=<you> group=full password=<strong>\n/user disable admin')
    if str(router.username).lower() == 'admin':
        audit.add(f'r{router.id}:api-user-admin', 'medium', 'Access control', 'TapTap connects with the admin account',
                  'If the TapTap database is ever exposed, the router is fully compromised. Use a dedicated API user.',
                  router, cli='/user group add name=taptap policy=read,write,api,test,sensitive,!ftp,!ssh,!telnet,!winbox,!web,!reboot,!policy\n'
                              '/user add name=taptap group=taptap password=<strong> address=<TAPTAP_SERVER_IP>/32')

    # ---- transport of TapTap credentials ----
    if router.connection_mode != 'agent' and router.ip_address and not router.use_ssl and _is_public(router.ip_address):
        audit.add(f'r{router.id}:api-cleartext', 'high', 'Access control', 'TapTap talks to this router over the Internet without encryption',
                  'The plain API (8728) is used over a public address, so the router password crosses the Internet readable. '
                  'Use api-ssl (8729) or connect through a VPN.', router,
                  evidence=[f'{router.ip_address}:{router.api_port}'],
                  cli='/certificate add name=api common-name=router days-valid=3650\n/certificate sign api\n'
                      '/ip service set api-ssl certificate=api disabled=no')

    # ---- extra services ----
    def enabled(label, key='enabled'):
        rows = _rows(snap, label)
        return bool(rows) and truthy(g(rows[0], key, default='no'))
    if enabled('SOCKS'):
        audit.add(f'r{router.id}:ip-socks', 'high', 'Router hardening', 'SOCKS proxy is enabled',
                  'An enabled SOCKS proxy is a classic sign of compromise and lets outsiders tunnel through your line.', router,
                  cli='/ip socks set enabled=no', fix='ip-socks')
    if enabled('Web proxy') and not protected:
        audit.add(f'r{router.id}:ip-proxy-open', 'high', 'Router hardening', 'Web proxy is enabled without a WAN firewall',
                  'Open proxies are abused for spam and attacks, burning your bandwidth.', router,
                  cli='/ip proxy set enabled=no', fix='ip-proxy-open')
    if enabled('UPnP'):
        audit.add(f'r{router.id}:ip-upnp', 'medium', 'Router hardening', 'UPnP is enabled',
                  'Any customer device on the hotspot could open ports on your router.', router,
                  cli='/ip upnp set enabled=no', fix='ip-upnp')
    if enabled('Bandwidth server'):
        audit.add(f'r{router.id}:bw-server', 'low', 'Router hardening', 'Bandwidth-test server is enabled',
                  'It can be used to flood the router and saturate the uplink.', router,
                  cli='/tool bandwidth-server set enabled=no', fix='bw-server')
    for community in _rows(snap, 'SNMP communities'):
        if str(g(community, 'name')) == 'public' and not truthy(g(community, 'disabled', default='no')) and enabled('SNMP'):
            audit.add(f'r{router.id}:snmp-public', 'medium', 'Router hardening', 'SNMP uses the default "public" community',
                      'Anyone can read interface, client and route data.', router,
                      cli='/snmp community set [find name=public] addresses=<MONITOR_IP>/32 name=<secret>')
    mac_winbox = _rows(snap, 'MAC Winbox')
    if mac_winbox and str(g(mac_winbox[0], 'allowed-interface-list', 'allowed-interface-list')).lower() == 'all':
        audit.add(f'r{router.id}:mac-winbox', 'medium', 'Router hardening', 'MAC-Winbox is allowed on all interfaces',
                  'Hotspot customers on the same Layer-2 can reach Winbox by MAC address, bypassing IP firewalling.', router,
                  cli='/tool mac-server mac-winbox set allowed-interface-list=LAN')
    discovery = _rows(snap, 'Neighbor discovery settings')
    if discovery and str(g(discovery[0], 'discover-interface-list')).lower() == 'all':
        audit.add(f'r{router.id}:discovery-all', 'low', 'Router hardening', 'Neighbor discovery is broadcast on every interface',
                  'The router advertises its model and version to the ISP side. Limit it to LAN — TapTap topology only needs LAN.', router,
                  cli='/ip neighbor discovery-settings set discover-interface-list=LAN')

    # ---- hotspot abuse signals ----
    bypassed = [r for r in router.synced_ip_bindings.filter(binding_type='bypassed', disabled=False, is_present=True)]
    uncommented = [b for b in bypassed if not (b.comment or '').strip() or b.comment.strip().lower() in {'taptap'}]
    if len(bypassed) >= 1:
        sev = 'medium' if len(uncommented) >= 3 else 'low' if uncommented else 'info'
        audit.add(f'r{router.id}:bypassed', sev, 'Hotspot abuse', f'{len(bypassed)} device(s) bypass the hotspot for free',
                  'Bypassed IP bindings get unlimited Internet with no voucher. Make sure every one is intentional.', router,
                  evidence=[f'{b.mac_address or b.address} — {b.comment or "no comment"}' for b in bypassed], link='ip_bindings')
    unlimited = router.hotspot_users.filter(is_present=True, disabled=False, limit_uptime__in=['', '0s', '0'], source='mikrotik').exclude(username__in=['admin', 'default-trial'])\
        .exclude(username__in=router.business.vouchers.filter(duration_minutes=0, expires_at__isnull=True).values('code'))  # unlimited plans on purpose
    count = unlimited.count()
    if count:
        audit.add(f'r{router.id}:unlimited-users', 'medium' if count > 5 else 'low', 'Revenue', f'{count} hotspot user(s) never expire',
                  'MikroTik-side users without limit-uptime give free unlimited access and are not sold through TapTap.', router,
                  evidence=list(unlimited.values_list('username', flat=True)[:12]))


def audit_business(business):
    audit = Audit()
    now = timezone.now()
    routers = list(business.routers.all().order_by('name'))
    if not routers:
        audit.add('no-routers', 'info', 'Coverage', 'No routers added yet', 'Add a MikroTik to start monitoring.', link='routers')
    for router in routers:
        audit_router(audit, router, now)

        # ---- availability & sync health ----
        if router.status != 'Online':
            audit.add(f'r{router.id}:offline', 'high', 'Availability', f'{router.name} is unreachable',
                      router.last_error or 'TapTap could not reach the RouterOS API.', router,
                      evidence=[f'last check {timezone.localtime(router.last_tested_at):%d %b %H:%M}' if router.last_tested_at else 'never checked'])
        try:
            last_ok = router.sync_jobs.filter(status='success').order_by('-finished_at').first()
            last_job = router.sync_jobs.order_by('-created_at').first()
        except Exception:
            last_ok = last_job = None
        if last_job and last_job.status == 'failed':
            audit.add(f'r{router.id}:sync-failed', 'medium', 'Sync health', 'Last synchronization failed',
                      (last_job.error or 'Unknown error')[:300], router, link='routers')
        elif not last_ok or (last_ok.finished_at and last_ok.finished_at < now - timedelta(hours=24)):
            audit.add(f'r{router.id}:sync-stale', 'low', 'Sync health', 'No successful sync in the last 24 hours',
                      'Voucher changes made in TapTap may not have reached the router.', router, link='routers')

        # ---- sync drift: revenue leaks ----
        disabled_codes = set(business.vouchers.filter(router=router, source='taptap', status__in=['disabled', 'expired']).values_list('code', flat=True))
        leaking = list(router.hotspot_users.filter(is_present=True, disabled=False, username__in=disabled_codes).values_list('username', flat=True)[:50]) if disabled_codes else []
        if leaking:
            audit.add(f'r{router.id}:drift-disabled', 'high', 'Revenue', f'{len(leaking)} disabled voucher(s) still work on the router',
                      'These codes are disabled or expired in TapTap but still active in RouterOS — customers can keep using them free.',
                      router, evidence=leaking, link='routers')
        errors = business.vouchers.filter(router=router, mikrotik_sync_status='Error').count()
        if errors:
            audit.add(f'r{router.id}:voucher-sync-errors', 'medium', 'Revenue', f'{errors} voucher(s) failed to publish',
                      'Customers who bought these codes cannot log in until the next successful sync.', router, link='vouchers')

        # ---- voucher sharing from live hotspot sessions ----
        sessions = defaultdict(set)
        for dev in router.devices.filter(is_online=True, sources__contains='hotspot-active').exclude(hostname=''):
            if dev.mac_address:
                sessions[dev.hostname.upper()].add(dev.mac_address)
        if sessions:
            limits = dict(business.vouchers.filter(code__in=list(sessions.keys())).values_list('code', 'max_devices'))
            shared = [(code, len(macs), limits.get(code, 1)) for code, macs in sessions.items() if len(macs) > (limits.get(code) or 1)]
            if shared:
                audit.add(f'r{router.id}:voucher-sharing', 'medium', 'Hotspot abuse', f'{len(shared)} voucher(s) used on more devices than paid for',
                          'A code is logged in on more devices than its plan allows — usually a shared or resold voucher.', router,
                          evidence=[f'{c}: {n} devices (plan allows {lim})' for c, n, lim in shared], link='active_users')

        # ---- unknown routers on the LAN (rogue / customer routers) ----
        managed_ips = {r.ip_address for r in routers if r.ip_address}
        try:
            snap = router.config_snapshot
        except Exception:
            snap = None
        wan = _wan_interfaces(router, snap)
        rogue = [n for n in router.neighbors.filter(is_online=True, device_kind='router') if n.address not in managed_ips and n.interface_name not in wan]
        if rogue:
            audit.add(f'r{router.id}:unmanaged-routers', 'low', 'Network', f'{len(rogue)} unmanaged router(s) on the LAN',
                      'Routers you did not add to TapTap are advertising on customer ports. They may be rogue DHCP servers or resold connections.',
                      router, evidence=[f'{n.identity or n.board} {n.address} on {n.interface_name}' for n in rogue], link='topology')
    # ---- device signatures: one voucher, several physical devices ----
    try:
        from .ads import shared_vouchers
        shared = shared_vouchers(business, limit=40)
    except Exception:
        shared = []
    if shared:
        audit.add('sig:voucher-sharing', 'medium', 'Hotspot abuse', f'{len(shared)} voucher(s) used on more devices than paid for',
                  'Device signatures from your login pages show the same code on different phones or laptops — even when MAC addresses change. '
                  'Usually a shared or resold voucher.', None,
                  evidence=[f'{code}: {len(sigs)} devices (plan allows {allowed})' for code, sigs, allowed in shared], link='devices')
    return audit.findings


def summarize(findings, acked_keys):
    active = [f for f in findings if f['key'] not in acked_keys]
    acked = [f for f in findings if f['key'] in acked_keys]
    active.sort(key=lambda f: (SEVERITY_ORDER.index(f['severity']), f['router_name'], f['title']))
    counts = {s: sum(1 for f in active if f['severity'] == s) for s in SEVERITY_ORDER}
    score = max(0, 100 - sum(SEVERITY_WEIGHT[f['severity']] for f in active))
    per_router = defaultdict(lambda: {'score': 100, 'counts': defaultdict(int)})
    for f in active:
        if f['router_id']:
            pr = per_router[f['router_id']]
            pr['score'] = max(0, pr['score'] - SEVERITY_WEIGHT[f['severity']])
            pr['counts'][f['severity']] += 1
    categories = sorted({f['category'] for f in findings})
    return {'active': active, 'acked': acked, 'counts': counts, 'score': score, 'grade': grade(score),
            'per_router': {k: {'score': v['score'], 'grade': grade(v['score']), 'counts': dict(v['counts'])} for k, v in per_router.items()},
            'categories': categories}
