"""Config Studio's helper: plain words in, tiles out.

"Give the guests on ether5 10 Mbps and keep them away from my printers" becomes tiles on the board. The helper can
only add, change or remove *tiles* — it never writes RouterOS commands, and every tile it proposes goes through the
same checks as one made by hand (config_studio_tiles). Nothing it does reaches the router: the board still has to be
tested, backed up and deployed by a person.

It uses the AI that Fit uses (core/fit_ai: Gemini, then Groq). Without an AI key, or when the AI is busy, a small
built-in phrase reader understands the common requests, so the box always does something useful.
"""
from __future__ import annotations

import json
import logging
import re

from . import config_studio_tiles as tiles

logger = logging.getLogger('taptap.studio')

IFACE_RE = re.compile(r'\b((?:ether|sfp-sfpplus|sfp|wlan|wifi|vlan|bond|lte|combo)\d+|bridge[\w-]*|pppoe-[\w-]+)\b', re.I)
IP_RE = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?\b')
MAC_RE = re.compile(r'\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b')
SPEED_WORD = re.compile(r'\b(\d+(?:\.\d)?)\s*(k|kb|kbps|m|mb|mbps|meg|megs|g|gb|gbps)\b', re.I)
HOST_WORD = re.compile(r'\b((?:[a-z0-9-]+\.)+(?:com|net|org|gm|sn|io|tv|co|app|me|info|ng|uk|fr)(?:\.[a-z]{2})?)\b', re.I)


class HelperBusy(Exception):
    pass


def ready():
    try:
        from . import fit_ai
        return fit_ai.ready()
    except Exception:
        return False


def _catalogue_text():
    lines = []
    for key, meta in tiles.TILES.items():
        fields = ', '.join(f'{f["name"]}{"?" if f.get("optional") else ""}'
                           + (f'[{"|".join(f["options"])}]' if f.get('options') and f['type'] == 'choice' else '')
                           + (f'={f["default"]}' if f.get('default') else '') for f in meta['fields'])
        lines.append(f'- {key}: {meta["sentence"]}  ({fields})  — {meta["why"]}')
    return '\n'.join(lines)


def _router_text(vr):
    if vr is None:
        return 'The router has not been read yet; assume ether1 is the internet port and ether2–ether5 are free.'
    wans = vr.wan_interfaces()
    lines = ['Ports and groups: ' + ', '.join(
        f'{n}{" (internet)" if n in wans else ""}{" in " + vr.bridge_of(n) if vr.bridge_of(n) else ""}' for n in vr.interfaces()[:40])]
    lines.append('Addresses: ' + (', '.join(f'{a} on {i}' for a, i, _ in vr.addresses()[:20]) or 'none'))
    hs = [f'{h.get("name")} on {h.get("interface")}' for h in vr.live('/ip/hotspot')]
    if hs:
        lines.append('Hotspots: ' + ', '.join(hs))
    return '\n'.join(lines)


def system_prompt(router, board, vr, findings):
    board_text = '\n'.join(f'- id {t["id"]} ({t["type"]}{", switched off" if t.get("off") else ""}): {tiles.describe(t)}' for t in board) or '(empty)'
    issues = '\n'.join(f'- {f["level"]}: {f["text"]}' + (f' [tile {f["tile"]}]' if f.get('tile') else '') for f in findings[:12]) or '(none)'
    return f"""You are the helper inside TapTap Config Studio, where a person builds a MikroTik router configuration out of tiles.
You change the BOARD only. You never write RouterOS commands and you never claim something was applied to the router.

Tiles you may use (type: sentence (fields, ? = optional, =default) — purpose):
{_catalogue_text()}

The router "{router.name}", as it would be with the current board:
{_router_text(vr)}

The board now:
{board_text}

What the virtual router says about the board:
{issues}

Answer with ONE JSON object and nothing else:
{{"say": "one or two short friendly sentences in the language the person wrote in, saying what you did and what to check",
  "add": [{{"type": "<tile type>", "params": {{"<field>": "<value>"}}}}],
  "change": [{{"id": "<tile id>", "params": {{"<field>": "<value>"}}}}],
  "remove": ["<tile id>"]}}

Rules: use only the tile types and field names above. Use ports that exist on the router. Prefer few tiles. Networks are written
as router-address/size (192.168.50.1/24) and must not overlap an existing one. Speeds look like 5M or 512k. Never invent a MAC
address, a password or a public IP: if one is needed and was not given, leave that field empty and ask for it in "say".
If the request is a question, answer it in "say" and leave the lists empty. If it is not about this router, say so briefly."""


def _json_of(text):
    text = str(text or '').strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('no JSON')
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError('not an object')
    return data


def merge(board, data):
    """Apply {add, change, remove} to a board. Returns (board, ids touched)."""
    board = [dict(t, params=dict(t['params'])) for t in tiles.clean_board(board)]
    touched = []
    remove = {str(x) for x in (data.get('remove') or []) if isinstance(x, (str, int))}
    board = [t for t in board if t['id'] not in remove]
    by_id = {t['id']: t for t in board}
    for ch in (data.get('change') or [])[:20]:
        t = by_id.get(str((ch or {}).get('id')))
        if t and isinstance(ch.get('params'), dict):
            secrets = tiles.secret_fields(t['type'])
            t['params'].update({k: v for k, v in ch['params'].items() if not (k in secrets and not str(v).strip())})
            touched.append(t['id'])
    n = len(board)
    for add in (data.get('add') or [])[:12]:
        if isinstance(add, dict) and add.get('type') in tiles.TILES and len(board) < tiles.MAX_TILES:
            n += 1
            tid = f'ai{n}'
            while tid in by_id:
                n += 1
                tid = f'ai{n}'
            given = add.get('params') if isinstance(add.get('params'), dict) else {}
            shown = {f['name']: f['default'] for f in tiles.TILES[add['type']]['fields'] if f.get('default')}     # blanks show their usual value
            tile = {'id': tid, 'type': add['type'], 'params': {**shown, **{k: v for k, v in given.items() if str(v).strip()}}}
            board.append(tile)
            by_id[tid] = tile
            touched.append(tid)
    return tiles.clean_board(board), touched


def ask(user, router, text, board, vr, findings, ctx):
    """{'say', 'tiles', 'touched', 'engine'}"""
    data, engine = None, 'Phrase reader'
    if ready():
        from . import fit_ai
        if not fit_ai.rate_ok(user):
            raise HelperBusy('That is a lot of questions in a short time. Wait a minute and ask again.')
        try:
            response = fit_ai.complete([{'role': 'system', 'content': system_prompt(router, board, vr, findings)},
                                        {'role': 'user', 'content': text}], None)
            data = _json_of(response['choices'][0]['message'].get('content'))
            engine = response.get('_fit_engine') or 'AI'
        except Exception as exc:              # the AI is down, over its limit, or did not answer in JSON
            logger.warning('Config Studio helper fell back to the phrase reader: %r', exc)
            data = None
    if data is None:
        data = phrase_reader(text, vr)
        if ready():
            data['say'] = 'The AI did not answer just now, so I read your sentence with the built-in reader. ' + data['say']
    new_board, touched = merge(board, data)
    notes = []
    for t in new_board:
        if t['id'] in touched:
            try:
                tiles.compile_tile(t, ctx)
            except tiles.TileError as exc:
                notes.append(str(exc))
    say = str(data.get('say') or ('Done.' if touched else 'I did not change the board.'))[:700]
    if notes:
        say += ' Still to fill in: ' + ' '.join(notes[:4])
    return {'say': say, 'tiles': new_board, 'touched': touched, 'engine': engine}


# ───────────────────────────── the built-in phrase reader ─────────────────────────────
def _speed(word):
    n, unit = word
    return f'{n}{"k" if unit.lower().startswith("k") else "G" if unit.lower().startswith("g") else "M"}'


def phrase_reader(text, vr=None):
    """A few dozen common requests, understood without any AI."""
    low = ' ' + text.lower() + ' '
    ifaces = [m.group(1) for m in IFACE_RE.finditer(text)]
    ips = IP_RE.findall(text)
    macs = [m.group(1) for m in MAC_RE.finditer(text)]
    speeds = [_speed(s) for s in SPEED_WORD.findall(text)]
    hosts = [h for h in HOST_WORD.findall(text) if not IP_RE.fullmatch(h)]
    wans = vr.wan_interfaces() if vr is not None else []
    wan = (wans or ['ether1'])[0]
    add, said = [], []

    def tile(kind, why, **params):
        add.append({'type': kind, 'params': {k: v for k, v in params.items() if v not in (None, '')}})
        said.append(why)

    def has(*words):
        return any(w in low for w in words)

    cidr = next((a + b for a, b in ips if b), '')
    plain = [a for a, b in ips if not b]
    port_no = re.search(r'\bport\s+(\d{1,5})\b', low)

    if has('pppoe'):
        user = re.search(r'user(?:name)?\s+(\S+)', text, re.I)
        tile('wan_pppoe', 'a PPPoE internet line (type the password on the tile)', port=(ifaces or [wan])[0], user=user.group(1) if user else '')
    elif has(' internet line', ' wan ', 'internet on', 'internet comes', 'uplink') and not has('block', 'guest'):
        tile('wan_dhcp', 'an internet line', port=(ifaces or [wan])[0])
    if has('guest', 'visitor'):
        tile('guest', 'a guest network kept away from yours', interface=(ifaces or ['ether5'])[-1], network=cidr, down=(speeds or ['10M'])[0],
             up=speeds[1] if len(speeds) > 1 else '')
    elif has('hotspot', 'login page', 'sign in', 'voucher'):
        if has('free', 'without', 'walled') and hosts:
            tile('walled_garden', 'free sites', sites=', '.join(hosts))
        elif has('skip', 'bypass', 'no login', 'without login') and macs:
            tile('bypass', 'a device that skips the login', mac=macs[0])
        else:
            tile('hotspot', 'a hotspot', interface=(ifaces or ['bridge-hotspot'])[-1], network=cidr)
    if has('block', 'ban ', 'forbid') and (macs or hosts) and not has('free', 'walled'):
        if macs:
            tile('block_device', 'a blocked device', mac=macs[0])
        else:
            tile('block_site', 'blocked sites', sites=', '.join(hosts))
    elif add:
        pass
    elif has('forward', 'open port', 'open a port', 'port forward', 'expose', 'camera', 'dvr') and port_no and plain:
        to_port = re.search(r'\bto\s+port\s+(\d{1,5})\b', low)
        tile('port_forward', 'an open port', protocol='udp' if ' udp' in low else 'tcp', port=port_no.group(1), to_ip=plain[-1],
             to_port=to_port.group(1) if to_port else '')
    elif has('limit', 'slow', 'throttle', 'speed', 'mbps', 'cap ') and speeds and (plain or cidr):
        tile('speed_limit', 'a speed limit', target=cidr or plain[0], down=speeds[0], up=speeds[1] if len(speeds) > 1 else speeds[0])
    elif has('reserve', 'always get', 'fixed address', 'static lease', 'same address') and macs and plain:
        tile('static_lease', 'a reserved address', mac=macs[0], ip=plain[0])
    elif has('vlan'):
        vid = re.search(r'vlan\s*(?:id\s*)?(\d{1,4})', low)
        tile('vlan', 'a VLAN', id=vid.group(1) if vid else '', parent=(ifaces or ['ether2'])[0])
    elif has('bridge', 'join', 'switch group', 'same network') and len(ifaces) >= 2:
        tile('bridge', 'a group of ports', ports=', '.join(dict.fromkeys(ifaces)))
    elif has('network', 'lan', 'dhcp') and (cidr or ifaces):
        tile('lan', 'a local network', interface=(ifaces or ['bridge-lan'])[0], network=cidr)
    if has('dns') and plain and not has('guest'):
        tile('dns', 'DNS servers', servers=', '.join(plain[:3]))
    if has('firewall', 'protect', 'secure', 'shield', 'harden', 'hack'):
        tile('fw_basic', 'a shield against logins from the internet', wan=wan)
    if has('telnet', 'ftp', 'close old', 'unused service'):
        tile('services', 'old services switched off', off=', '.join(s for s in ('telnet', 'ftp', 'www') if s in low) or 'telnet, ftp')
    if has('time zone', 'timezone', 'clock', 'ntp'):
        tile('clock', 'the clock')
    name = re.search(r'(?:call|name|rename)\s+(?:this\s+|the\s+)?router\s+(?:to\s+)?["“]?([\w.-]{2,40})', text, re.I)
    if name:
        tile('identity', 'the router name', name=name.group(1))
    off = re.search(r'(?:switch|turn)\s+(off|on)\s+((?:ether|sfp|wlan)\d+)|(?:disable|enable)\s+((?:ether|sfp|wlan)\d+)', low)
    if off:
        tile('port_state', 'a port switched', port=off.group(2) or off.group(3), state=off.group(1) or ('off' if 'disable' in off.group(0) else 'on'))
    if not add:
        return {'say': 'I could not turn that into tiles. Try something like “guest Wi-Fi on ether5 at 10 Mbps”, “block tiktok.com”, '
                       '“open port 8080 to 192.168.88.20” or “limit 192.168.88.50 to 2 Mbps” — or drag a tile from the shelf.',
                'add': [], 'change': [], 'remove': []}
    return {'say': 'I added ' + ', '.join(said) + '. Check the blanks, then look at the virtual router.', 'add': add, 'change': [], 'remove': []}
