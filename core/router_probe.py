"""Probe a router TapTap does not manage (mostly TP-Link in access point mode) — no password.

TapTap cannot reach 192.168.x.x behind a customer's MikroTik, but the MikroTik can. So the probe
asks the MikroTik (direct API) to:

* **ping** the router — is it alive, how fast does it answer;
* **fetch its web page** (``/tool fetch http://<ip>/``) — the login page usually names the maker
  and, on many TP-Link firmwares, the model (``TL-WR840N``, ``Archer C6``…).

and adds what TapTap already knows from the MikroTik tables:

* **same box** — one TP-Link can show up with two neighbouring MACs (e.g. its LAN and a Wi-Fi side).
  Seen on the same port they are one unit, not two routers (only suggestions are merged);
* **its own DHCP still on** — connected LAN-to-LAN, a TP-Link must not hand out addresses. Customers
  on its port with addresses the MikroTik never gave (192.168.0.x…) mean one still does;
* **IP conflict** — several devices answering on one address (typically TP-Links all left on
  192.168.0.1). In access point mode that breaks their web pages and must be fixed on site;
* **no route** — the MikroTik has no address in the router's network, so nothing can reach it.

Routers connected through TapTap Link get the table checks only: Link commands cannot bring a
web page back to TapTap.
"""
from __future__ import annotations

import ipaddress
import re

from django.utils import timezone

from .net_vendors import brand_of
from .site_routers import collect, mac_norm, physical_port, router_networks

MAKERS = [('TP-Link', re.compile(r'tp-?link|tplinkwifi|tplinklogin|tplinkrepeater|tplinkmodem', re.I)),
          ('Mercusys', re.compile(r'mercusys|mwlogin', re.I)), ('Tenda', re.compile(r'tenda', re.I)),
          ('Ubiquiti', re.compile(r'ubiquiti|airos|unifi', re.I)), ('MikroTik', re.compile(r'mikrotik|routeros|webfig', re.I)),
          ('Netis', re.compile(r'netis', re.I)), ('Cudy', re.compile(r'cudy', re.I)), ('Huawei', re.compile(r'huawei', re.I)),
          ('ZTE', re.compile(r'\bzte\b', re.I)), ('D-Link', re.compile(r'd-link|dlink', re.I))]
MODEL_RE = re.compile(r'\b(TL-[A-Z]{2,4}\d{2,4}[A-Z]{0,3}|Archer[ _-]?[A-Z]{0,3}\d{1,4}[A-Z]{0,3}|Deco[ _-]?[A-Z]{0,2}\d{1,3}|'
                      r'EAP\d{3}(?:-[A-Za-z]+)?|RE\d{3}[A-Z]?|MR\d{3,4}|CPE\d{3}|WA\d{3,4}[A-Z]{0,3}|MW\d{3}[A-Z]{0,2}|'
                      r'AC\d{1,2}|AX\d{2,4}|N\d{3}RT|F\d{3}|HG\d{4}[A-Z]?)\b')
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)


def same_unit(a, b):
    """Two MACs of one box: same first five bytes, last byte 1 apart.
    Kept tight on purpose: two units from one batch can be only a few addresses apart."""
    a, b = mac_norm(a).split(':'), mac_norm(b).split(':')
    if len(a) != 6 or len(b) != 6 or a[:5] != b[:5]:
        return False
    try:
        return a != b and abs(int(a[5], 16) - int(b[5], 16)) <= 1
    except ValueError:
        return False


def read_page(html):
    """(title, maker, model) from a web page."""
    html = str(html or '')
    m = TITLE_RE.search(html)
    title = re.sub(r'\s+', ' ', m.group(1)).strip()[:120] if m else ''
    maker = next((name for name, rx in MAKERS if rx.search(html)), '')
    mm = MODEL_RE.search(title) or MODEL_RE.search(html)
    model = re.sub(r'[ _-]+', ' ', mm.group(1)).replace('TL ', 'TL-').strip() if mm else ''
    if model and not maker and model.upper().startswith(('TL-', 'ARCHER', 'DECO', 'EAP', 'RE', 'MR')):
        maker = 'TP-Link'
    return title, maker, model


def _table_checks(business, e):
    from .models import RouterDevice
    out = {'same_unit': [], 'ip_conflict': []}
    if not e['mac']:
        return out
    devs = RouterDevice.objects.filter(router__business=business).exclude(mac_address='')
    for d in devs:
        m = mac_norm(d.mac_address)
        if m == e['mac']:
            continue
        if same_unit(m, e['mac']) and d.router_id == e['router_id'] and physical_port(d) == e['port']:
            out['same_unit'].append(m)
        if e['ip'] and d.ip_address == e['ip'] and d.is_online and m not in out['same_unit']:
            out['ip_conflict'].append({'mac': m, 'brand': brand_of(m), 'port': physical_port(d), 'hostname': d.hostname})
    out['same_unit'] = sorted(set(out['same_unit']))
    return out


def probe(business, key, user=None):
    """Probe one router from the Detail list. Returns the result (and saves it on confirmed routers)."""
    from .models import SiteRouter
    from .voucher_history import channel
    e = next((x for x in collect(business) if x['key'] == key), None)
    if not e:
        raise ValueError('That router is no longer in the list.')
    res = {'at': timezone.now().isoformat(), 'ip': e['ip'], 'mac': e['mac'], 'live': False, 'reachable': None,
           'rtt_ms': None, 'title': '', 'maker': '', 'model': '', 'web': '', 'notes': []}
    res.update(_table_checks(business, e))
    notes = res['notes']
    if res['same_unit']:
        notes.append(f'{len(res["same_unit"]) + 1} MAC addresses belong to this one box — shown as one router.')
    if res['ip_conflict']:
        notes.append(f'IP conflict: {len(res["ip_conflict"]) + 1} devices answer on {e["ip"]}. In access point mode give each TP-Link '
                     'its own address (DHCP from the MikroTik with a static lease) or its web page and this probe will hit the wrong box.')
    if e.get('foreign_dhcp'):
        notes.append(f'{e["foreign_dhcp"]} customers on {e["port"]} have addresses the MikroTik never gave '
                     f'({", ".join(e["foreign_sample"])}…). A router on this port still runs its own DHCP server: '
                     'in its settings turn DHCP off (LAN-to-LAN routers must never hand out addresses), then reconnect those phones.')
        res['foreign_dhcp'] = e['foreign_dhcp']
    router = business.routers.filter(pk=e['router_id']).first() if e['router_id'] else None
    if not e['ip']:
        notes.append('No IP address known yet, so it cannot be checked live. Run “Discover all now” or add its IP.')
    elif not router:
        notes.append('Not linked to a MikroTik yet, so it cannot be checked live.')
    elif channel(router) == 'TapTap Link':
        notes.append(f'{router.name} is connected through TapTap Link, which cannot bring a web page back — only the table checks ran.')
    else:
        nets = router_networks(router)
        try:
            addr = ipaddress.ip_address(e['ip'])
        except ValueError:
            addr = None
        if nets is not None and addr is not None and not any(addr in n for n in nets):
            notes.append(f'{router.name} has no address in {e["ip"]}’s network, so it cannot reach it. Either give the TP-Link an '
                         f'address in the MikroTik’s LAN (best: DHCP) or add an address in that network to the MikroTik.')
        _live(router, e['ip'], res)
    saved = business.site_routers.filter(pk=e['id']).first() if e['id'] else None
    if saved:
        fields = ['probe', 'probed_at']
        saved.probe, saved.probed_at = res, timezone.now()
        if res['model'] and not saved.model:
            saved.model = res['model'][:80]; fields.append('model')
        if res['maker'] and not saved.brand:
            saved.brand = res['maker'][:40]; fields.append('brand')
        SiteRouter.objects.filter(pk=saved.pk).update(**{f: getattr(saved, f) for f in fields})
    return res


def _live(router, ip, res):
    from .mikrotik import MikroTikService
    notes = res['notes']
    try:
        svc = MikroTikService(router).connect()
    except Exception as exc:
        notes.append(f'Could not reach {router.name}: {exc}')
        return
    res['live'] = True
    try:
        try:
            rows = svc.resource('/').call('ping', {'address': ip, 'count': '3'}) or []
            last = rows[-1] if rows else {}
            received = int(str(last.get('received', 0)) or 0)
            res['reachable'] = received > 0
            avg = str(last.get('avg-rtt', '') or '')
            ms = re.match(r'(?:(\d+)ms)?(?:(\d+)us)?', avg)
            if received and ms and (ms.group(1) or ms.group(2)):
                res['rtt_ms'] = round(int(ms.group(1) or 0) + int(ms.group(2) or 0) / 1000, 1)
            notes.append(f'Answers ping in {res["rtt_ms"]} ms.' if received else 'Does not answer ping from the MikroTik.')
        except Exception as exc:
            notes.append(f'Ping failed: {exc}')
        try:
            rows = svc.resource('/tool').call('fetch', {'url': f'http://{ip}/', 'output': 'user', 'duration': '6s', 'idle-timeout': '4s'}) or []
            data = next((r.get('data') for r in reversed(rows) if r.get('data')), '')
            if data:
                res['title'], res['maker'], res['model'] = read_page(data)
                res['web'] = 'ok'
                seen = ' '.join(x for x in (res['maker'], res['model']) if x)
                notes.append(f'Web page answers{": " + seen if seen else ""}' + (f' (title “{res["title"]}”).' if res['title'] else '.'))
                if res['maker'] and not res['model']:
                    notes.append('The page does not show the model before login — set it by hand in Edit.')
            else:
                res['web'] = 'empty'
                notes.append('Web page answered but was empty.')
        except Exception as exc:
            res['web'] = str(exc)[:160]
            notes.append(f'Web page did not answer: {res["web"]}')
    finally:
        svc.close()
