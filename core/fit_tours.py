"""Fit's "How to" — guided tours across TapTap (played by static/js/fit-tour.js).

Fit throws something at the next thing to press — a spear, an arrow, a light-gun beam, a water jet, a lightning
bolt, a paper plane, or it flies a little single-seat propeller plane there — the item ripples, a hand presses it,
and a tip explains what it does. The tour follows you from page to page until the job is done.

A step:
    page     url name of the page the step happens on (None = wherever the person is)
    match    regex for pages with an id in the path (a router's Link page, a voucher) — with ``go`` saying how to
             get there from ``page``
    target   what Fit points at:  "nav"               the menu item of ``page``
                                  "text:Rotate token" a button / link / tab whose text contains it
                                  "field:quantity"    a form field by name
                                  "sel:#troubleshoot" any CSS selector
                                  None                no single thing: the tip floats next to Fit
    title, text, tip

Tours are filtered per person: steps on pages their role cannot open are left out, and a tour whose main page they
cannot open is not offered. Every How-To guide of the Support page (core/help_guides.py) also becomes a tour, so
every feature with a guide can be shown.
"""
from __future__ import annotations

import re

from django.urls import NoReverseMatch, reverse

CATEGORIES = [
    ('routers', 'Routers & TapTap Link', '🛜'),
    ('vouchers', 'Vouchers & plans', '🎟️'),
    ('members', 'Members', '🪪'),
    ('network', 'Network & security', '🛡️'),
    ('portal', 'Login page & adverts', '🎨'),
    ('business', 'Money, team & business', '💼'),
    ('guides', 'More guides', '📘'),
]


def T(title, text, page=None, target='nav', tip='', match='', go=''):
    return {'title': title, 'text': text, 'page': page, 'target': target, 'tip': tip, 'match': match, 'go': go}


LINK_PAGE = r'^/routers/\d+/link/'
VOUCHER_PAGE = r'^/vouchers/\d+/$'
MEMBER_PAGE = r'^/members/\d+/'

TOURS = [
    # ───── routers & TapTap Link ─────
    {'slug': 'add-router', 'cat': 'routers', 'icon': '🛜', 'title': 'Add a router to the business', 'guide': 'register-mikrotik',
     'summary': 'Register a MikroTik with TapTap Link, paste one block into WinBox, watch it turn green.',
     'keywords': 'add new router mikrotik register connect link install',
     'steps': [
         T('Open MikroTik Control', 'Everything about your routers lives here.', 'routers'),
         T('Give it a name', 'A name you will recognise, e.g. **Main Hall** or **Kerr Sering mast**.', 'routers', 'field:router_name'),
         T('Create it', 'TapTap creates the router and its private TapTap Link token.', 'routers', 'text:Create Router & Generate Link',
           tip='No public IP, VPN or open port needed: the router calls TapTap.'),
         T('Copy the install block', 'Copy the install — **Short** just connects; **Full install** (the tab next to it) also sets up the clock, '
           'certificates and the TapTap Tunnel.', None, 'text:Copy short install', match=LINK_PAGE, go='text:TapTap Link',
           tip='The block holds this router’s secret token and is shown only right after you create the router or rotate its token. '
               'Lost it? Use Rotate token.'),
         T('Paste it into WinBox', 'In WinBox open **New Terminal**, paste the whole block and press **Enter**. Wait for the prompt to come back.',
           None, None, match=LINK_PAGE, tip='Paste all of it in one go — half a block is the most common reason a router never checks in.'),
         T('Watch it go green, then Sync', 'Within a minute the router shows **Online**. Press **Sync** once: plans, vouchers and devices come in.',
           'routers', 'text:Sync'),
     ]},
    {'slug': 'rotate-token', 'cat': 'routers', 'icon': '🔑', 'title': 'Rotate a router’s Link token',
     'summary': 'Lost the install block, or someone else saw it? Make a new token — the old one stops working.',
     'keywords': 'rotate token new token lost install block security link',
     'steps': [
         T('Open MikroTik Control', 'Find the router in the list.', 'routers'),
         T('Open its TapTap Link page', 'Each Link router has a **TapTap Link** button on its card.', 'routers', 'text:TapTap Link'),
         T('Rotate token', 'Press **Rotate token**. The old token stops working at once, so the router goes offline until you paste the new block.',
           None, 'text:Rotate token', match=LINK_PAGE, go='text:TapTap Link', tip='Do this when a staff member who saw the block leaves.'),
         T('Copy the new block', 'Copy the new install and paste it into the router’s **New Terminal** in WinBox.', None, 'text:Copy short install',
           match=LINK_PAGE, go='text:TapTap Link', tip='The new block appears on this page right after you rotate.'),
     ]},
    {'slug': 'troubleshoot-link', 'cat': 'routers', 'icon': '🩺', 'title': 'Troubleshoot a TapTap Link router', 'guide': 'troubleshoot-link',
     'summary': 'Router shows offline, or commands wait for ever: check-in, internet, the script, the tools.',
     'keywords': 'troubleshoot link offline not checking in waiting router problem fix',
     'steps': [
         T('Open MikroTik Control', 'A Link router’s card shows **Online** and how many seconds ago it last checked in.', 'routers'),
         T('Open its TapTap Link page', 'The live board here shows the last check-in, CPU, memory and what is waiting.', 'routers', 'text:TapTap Link'),
         T('When did it last check in?', '**Last seen** should be a few seconds. Minutes means the router cannot reach TapTap (its internet, DNS or clock).',
           None, 'sel:#lkSeen', match=LINK_PAGE, go='text:TapTap Link'),
         T('Read the troubleshooter', 'TapTap explains what it sees and what to do for each case.', None, 'sel:#troubleshoot', match=LINK_PAGE, go='text:TapTap Link'),
         T('Script broken on the router?', 'If the TapTap script was changed or deleted on the router, **Rotate token** gives a fresh install block: '
           'paste the **short install** into WinBox — it puts the connection back without touching anything else.',
           None, 'text:Rotate token', match=LINK_PAGE, go='text:TapTap Link'),
         T('Test from the router', 'In Network tools, switch to **From the router**.', 'network_tools', 'text:From the router'),
         T('Check the Internet', 'Tests gateway, ISP, DNS and websites from the router itself.', 'network_tools', 'text:Check the Internet',
           tip='A busy router is only “late”: tests still run at its next check-in.'),
     ]},
    {'slug': 'sync-router', 'cat': 'routers', 'icon': '🔄', 'title': 'Sync a router', 'guide': 'router-sync',
     'summary': 'Bring plans, vouchers and devices from the router, and send TapTap’s vouchers to it.',
     'keywords': 'sync full sync import router plans vouchers',
     'steps': [
         T('Open MikroTik Control', '', 'routers'),
         T('Sync one router', '**Sync** reads the router again and sends what TapTap has.', 'routers', 'text:Sync'),
         T('Or all of them', '**Full sync all routers** runs one after another.', 'routers', 'text:Full sync all routers'),
     ]},
    {'slug': 'router-schedule', 'cat': 'routers', 'icon': '⏰', 'title': 'Schedule a router restart or shutdown',
     'summary': 'Restart every night or power off at a set time — with warnings and notices.',
     'keywords': 'schedule restart reboot shutdown routine night',
     'steps': [
         T('Open MikroTik Control', '', 'routers'),
         T('Open Schedules', 'Each router card has **Schedules**.', 'routers', 'text:Schedules'),
         T('Add a schedule', 'Choose restart or shutdown, when, and who is told before and after.', None, 'text:Add a schedule'),
     ]},
    {'slug': 'network-tools', 'cat': 'network', 'icon': '🧰', 'title': 'Test the network from the router',
     'summary': 'Check the Internet, ping, traceroute, DNS, website, speed — and why customers drop.',
     'keywords': 'network tools ping traceroute dns speed test internet drop',
     'steps': [
         T('Open Network tools', '', 'network_tools'),
         T('From the router', 'Tests run on the MikroTik itself — exactly what your customers get. (**From this device** traces your own phone or laptop.)',
           'network_tools', 'text:From the router'),
         T('Check the Internet', 'One press tests the whole path: router → ISP → Internet → names → websites.', 'network_tools', 'text:Check the Internet'),
         T('Or pick a tool', 'Ping, Traceroute, DNS, Website, Speed…', 'network_tools', 'text:Ping'),
         T('Why people drop', 'Reads the router’s own log of sign-outs and tells you the reasons and the fix.', 'network_tools', 'text:Why people drop'),
     ]},
    # ───── vouchers & plans ─────
    {'slug': 'create-plan', 'cat': 'vouchers', 'icon': '🏷️', 'title': 'Create a plan', 'guide': 'create-plans',
     'summary': 'What you sell: a price, how long it lasts and how many devices.',
     'keywords': 'plan create price duration devices new plan',
     'steps': [
         T('Open Plans', '', 'plans'),
         T('Price', 'What a customer pays.', 'plans', 'field:price'),
         T('How long', 'Minutes, hours or days — counted from the first login.', 'plans', 'field:duration_value'),
         T('Devices', 'How many phones may use one voucher at the same time.', 'plans', 'field:max_devices'),
         T('Save', 'The plan goes to your routers as a hotspot profile.', 'plans', 'text:Save Plan'),
     ]},
    {'slug': 'generate-vouchers', 'cat': 'vouchers', 'icon': '🎟️', 'title': 'Generate and print vouchers', 'guide': 'generate-vouchers',
     'summary': 'Pick a plan and how many, choose the router, print.',
     'keywords': 'generate vouchers batch print create',
     'steps': [
         T('Open Generate Vouchers', '', 'generate_vouchers'),
         T('Choose the plan', '', 'generate_vouchers', 'field:plan'),
         T('How many', '', 'generate_vouchers', 'field:quantity'),
         T('Which router', 'They are sent to that router straight away.', 'generate_vouchers', 'field:router'),
         T('Who holds them', 'Your shop or an agent (the agent then owes the money).', 'generate_vouchers', 'field:owner'),
         T('Generate', 'You can print them right after with a card design.', 'generate_vouchers', 'text:Generate Vouchers'),
     ]},
    {'slug': 'one-voucher', 'cat': 'vouchers', 'icon': '🎫', 'title': 'Make one voucher for one customer',
     'summary': 'A single code, with the customer’s name and phone if you like.',
     'keywords': 'one voucher single customer',
     'steps': [
         T('Open Generate Vouchers', '', 'generate_vouchers'),
         T('One voucher for one person', '', 'generate_vouchers', 'text:One voucher for one person'),
     ]},
    {'slug': 'find-voucher', 'cat': 'vouchers', 'icon': '🔎', 'title': 'Find a voucher and fix it',
     'summary': 'Search a code, open it: add time, reset devices, freeze, Can’t connect?, send to the router first.',
     'keywords': 'find voucher search code add time reset devices freeze cannot connect fix',
     'steps': [
         T('Open Vouchers', '', 'vouchers'),
         T('Search', 'Type the code (or part of it).', 'vouchers', 'field:q'),
         T('Open the voucher', 'Click its code in the list.', 'vouchers', 'sel:table a[href^="/vouchers/"]'),
         T('Can’t connect?', 'Lists everything stopping it and **Fix it** sets it right on the router — then reads the router back.',
           None, "text:Can't connect?", match=VOUCHER_PAGE, go='sel:table a[href^="/vouchers/"]'),
         T('Add time', '', None, 'text:Add time', match=VOUCHER_PAGE, go='sel:table a[href^="/vouchers/"]'),
         T('More', 'Freeze, warn, **Reset devices**, **Send to the router first**, change code or router profile.', None, 'text:More',
           match=VOUCHER_PAGE, go='sel:table a[href^="/vouchers/"]'),
     ]},
    {'slug': 'batches', 'cat': 'vouchers', 'icon': '📦', 'title': 'Batches: see what was made and sold',
     'summary': 'Every batch with its sold, used and left — and its health on the router.',
     'keywords': 'batches batch sold left',
     'steps': [T('Open Batches', 'Open a batch to print it again, see each voucher or fix profiles.', 'batches')]},
    {'slug': 'voucher-bin', 'cat': 'vouchers', 'icon': '🗑️', 'title': 'Restore a deleted voucher',
     'summary': 'Deleted vouchers wait in the bin.', 'keywords': 'bin deleted restore',
     'steps': [T('Open the Bin', 'Restore puts it back exactly as it was.', 'voucher_bin')]},
    # ───── members ─────
    {'slug': 'add-member', 'cat': 'members', 'icon': '🪪', 'title': 'Add a member',
     'summary': 'A monthly customer with a username and password.', 'keywords': 'member add create monthly username password',
     'steps': [
         T('Open Members', '', 'members'),
         T('Create member', 'Choose a Member Plan, username and password; take the first payment.', 'members', 'text:Create member',
           tip='No Member Plan yet? Create one first — it is the price and length of a membership.'),
     ]},
    {'slug': 'renew-member', 'cat': 'members', 'icon': '🔁', 'title': 'Renew a member and record the payment',
     'summary': 'Renew, record cash and print the receipt in one go.', 'keywords': 'renew member payment receipt',
     'steps': [
         T('Open Members', '', 'members'),
         T('Renew', 'Each member row has **Renew**.', 'members', 'text:Renew'),
     ]},
    {'slug': 'member-plans', 'cat': 'members', 'icon': '📋', 'title': 'Create a Member Plan',
     'summary': 'The price, length and devices of a membership.', 'keywords': 'member plan create',
     'steps': [
         T('Open Members', '', 'members'),
         T('Create Member Plan', '', 'members', 'text:Create Member Plan'),
     ]},
    # ───── network & security ─────
    {'slug': 'live-sync', 'cat': 'network', 'icon': '⚡', 'title': 'Turn on live sync', 'guide': 'live-sync',
     'summary': 'Sales, used vouchers and online customers kept up to date every minute.',
     'keywords': 'live sync realtime enforcement',
     'steps': [
         T('Open Security', '', 'security'),
         T('Live sync', 'Switch it on and save.', 'security', 'field:live_sync'),
     ]},
    {'slug': 'fair-usage', 'cat': 'network', 'icon': '🐢', 'title': 'Slow down heavy users (fair usage)', 'guide': 'fair-usage',
     'summary': 'Slow a voucher in steps as it uses more data.', 'keywords': 'fair usage slow heavy data policy',
     'steps': [
         T('Open Security', '', 'security'),
         T('Fair usage', '', 'security', 'text:Fair usage'),
         T('New policy', 'Add the steps: after how much data, which speed.', 'security', 'text:New policy'),
     ]},
    {'slug': 'active-users', 'cat': 'network', 'icon': '👥', 'title': 'See who is online and disconnect someone',
     'summary': 'Every live session with its device, time and data.', 'keywords': 'active users online disconnect sessions',
     'steps': [T('Open Active Users', 'Each row has Disconnect.', 'active_users')]},
    {'slug': 'devices', 'cat': 'network', 'icon': '📱', 'title': 'Find a device and see its data', 'guide': 'device-data',
     'summary': 'Phones and laptops TapTap recognises, their vouchers and their data.', 'keywords': 'device find mac data',
     'steps': [T('Open Devices', 'Search by name, MAC or voucher.', 'devices')]},
    {'slug': 'ip-bindings', 'cat': 'network', 'icon': '🔓', 'title': 'Let a device in without a voucher (IP binding)',
     'summary': 'Bypass the login for a printer, a staff phone or a camera.', 'keywords': 'ip binding bypass device',
     'steps': [T('Open IP Binding', '', 'ip_bindings')]},
    {'slug': 'topology', 'cat': 'network', 'icon': '🗺️', 'title': 'See your network map', 'summary': 'Routers, access points and links on one map.',
     'keywords': 'topology map network', 'steps': [T('Open Topology', '', 'topology')]},
    # ───── portal ─────
    {'slug': 'publish-portal', 'cat': 'portal', 'icon': '🎨', 'title': 'Publish your login page', 'guide': 'publish-portal',
     'summary': 'Your brand on the page customers see.', 'keywords': 'portal login page publish studio',
     'steps': [
         T('Open Portal Studio', '', 'portal_studio'),
         T('Create or open a page', 'Press **New page** (or **Edit** on one you have): pick a template, add your logo and colours, publish.',
           'portal_studio', 'text:New page'),
         T('Put on all routers', 'Every router downloads your pages.', 'portal_studio', 'text:Put on all routers'),
     ]},
    {'slug': 'adverts', 'cat': 'portal', 'icon': '📣', 'title': 'Show adverts on the login page', 'guide': 'rotating-adverts',
     'summary': 'Rotating pictures — your own or paid by others.', 'keywords': 'advert ads pictures login',
     'steps': [T('Open Adverts', '', 'ads'), T('New advert', '', 'ads', 'text:New advert')]},
    {'slug': 'voucher-designs', 'cat': 'portal', 'icon': '🖨️', 'title': 'Design voucher cards', 'guide': 'print-vouchers',
     'summary': 'Your logo and colours on printed vouchers.', 'keywords': 'design print card voucher',
     'steps': [T('Open Voucher Designer', '', 'voucher_designs')]},
    # ───── business ─────
    {'slug': 'add-staff', 'cat': 'business', 'icon': '🧑‍💼', 'title': 'Give a staff member their own login', 'guide': 'team-accounts',
     'summary': 'Each person sees only what their role allows.', 'keywords': 'team staff add user login role',
     'steps': [
         T('Open Team', '', 'team'),
         T('Add team member', 'Name, email, role — they get their own password.', 'team', 'text:Add team member'),
     ]},
    {'slug': 'agents', 'cat': 'business', 'icon': '🤝', 'title': 'Add an agent and collect cash', 'guide': 'agents-cash',
     'summary': 'Agents resell your vouchers; TapTap tracks what they owe.', 'keywords': 'agent cash collect resell',
     'steps': [
         T('Open Finance', '', 'finance'),
         T('Agents & cash', '', 'finance', 'text:Agents & cash'),
         T('Add agent', '', 'finance', 'text:Add agent'),
         T('Collect', 'Record what an agent hands in.', 'finance', 'text:Collect'),
     ]},
    {'slug': 'expenses', 'cat': 'business', 'icon': '🧾', 'title': 'Record an expense and see profit',
     'summary': 'Bills and costs, and the profit after them.', 'keywords': 'expense profit cost bill',
     'steps': [T('Open Finance', '', 'finance'), T('Add expense', '', 'finance', 'text:Add expense'), T('Running profit', '', 'finance', 'text:Running profit')]},
    {'slug': 'daily-sales', 'cat': 'business', 'icon': '📈', 'title': 'Today’s sales', 'summary': 'Sales by hour, plan and seller.',
     'keywords': 'daily sales today', 'steps': [T('Open Daily sales', '', 'sales_daily')]},
    {'slug': 'reports', 'cat': 'business', 'icon': '📊', 'title': 'Reports and exports', 'summary': 'Revenue and vouchers by period; CSV and print.',
     'keywords': 'report revenue export csv', 'steps': [T('Open Reports', '', 'reports'), T('Export', '', 'reports', 'text:CSV')]},
    {'slug': 'brand', 'cat': 'business', 'icon': '🖌️', 'title': 'Your logo, colours and Wi-Fi name',
     'summary': 'Business profile, brand and hotspot settings.', 'keywords': 'settings logo brand colour wifi name',
     'steps': [
         T('Open Settings', '', 'settings'),
         T('Brand & appearance', '', 'settings', 'text:Brand & appearance'),
         T('Hotspot & Wi-Fi', 'The easy login address and the Wi-Fi name.', 'settings', 'text:Hotspot & Wi-Fi'),
     ]},
    {'slug': 'features', 'cat': 'business', 'icon': '🧩', 'title': 'Switch features on or off', 'summary': 'Show only what your business uses.',
     'keywords': 'features toggle modules', 'steps': [T('Open Features', '', 'business_features')]},
    {'slug': 'chat', 'cat': 'business', 'icon': '💬', 'title': 'Chat with your team and TapTap support', 'guide': 'chat-support',
     'summary': 'Messages, photos and calls.', 'keywords': 'chat message call support', 'steps': [T('Open Chat', '', 'chat_page')]},
    {'slug': 'bonanza', 'cat': 'business', 'icon': '🎡', 'title': 'Run a Bonanza (spin the wheel)', 'guide': 'bonanza',
     'summary': 'Prizes for customers who buy vouchers.', 'keywords': 'bonanza wheel prize',
     'steps': [T('Open Bonanza', '', 'bonanza_list')]},
]

# Pages whose menu item is the way in (main menu); everything else is reached from a page.
NAV_LABELS = {
    'routers': 'MikroTik Control', 'network_tools': 'Network tools', 'plans': 'Plans', 'generate_vouchers': 'Generate Vouchers',
    'vouchers': 'Vouchers', 'batches': 'Batches', 'voucher_bin': 'Bin', 'members': 'Members', 'security': 'Security',
    'active_users': 'Active Users', 'devices': 'Devices', 'ip_bindings': 'IP Binding', 'topology': 'Topology',
    'portal_studio': 'Portal Studio', 'ads': 'Adverts', 'voucher_designs': 'Voucher Designer', 'team': 'Team',
    'finance': 'Finance', 'sales_daily': 'Daily sales', 'reports': 'Reports', 'settings': 'Settings',
    'business_features': 'Features', 'chat_page': 'Chat', 'bonanza_list': 'Bonanza', 'dashboard': 'Overview',
    'support': 'Support', 'traffic': 'Traffic', 'alerts': 'Alerts', 'voucher_reports': 'Customer reports',
}
BY_SLUG = {t['slug']: t for t in TOURS}


def _url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


def _can(perms, role, name):
    if not name:
        return True
    if role in ('owner', None):
        return True
    from .permissions import allowed
    return bool(allowed(perms, name))


def _from_guides():
    """Every Support guide not already covered: one step per page it links to, the rest as tips."""
    from .help_guides import GUIDES
    covered = {t.get('guide') for t in TOURS}
    out = []
    for g in GUIDES:
        if g['slug'] in covered:
            continue
        steps = []
        for s in g['steps']:
            body = re.sub(r'\s+', ' ', s.get('body', ''))[:320]
            page = s['link'][0] if s.get('link') else None
            steps.append(T(s['title'], body, page, 'nav' if page else None, tip=s.get('tip') or ''))
        out.append({'slug': 'g-' + g['slug'], 'cat': 'guides', 'icon': '📘', 'title': g['title'], 'summary': g.get('summary', ''),
                    'keywords': g.get('keywords', ''), 'steps': steps, 'guide_url': _url_guide(g['slug'])})
    return out


def _url_guide(slug):
    try:
        return reverse('support_guide', args=[slug])
    except NoReverseMatch:
        return None


def all_tours():
    return TOURS + _from_guides()


def for_person(perms, role):
    """The tours this person may take, each step with its page address. Steps on pages they cannot open are dropped."""
    out = []
    for t in all_tours():
        steps = []
        for s in t['steps']:
            if s['page'] and not _can(perms, role, s['page']):
                continue
            url = _url(s['page']) if s['page'] else None
            if s['page'] and not url:
                continue
            steps.append({**s, 'url': url, 'label': NAV_LABELS.get(s['page'] or '', '')})
        pages = [s['page'] for s in t['steps'] if s['page']]
        if not steps or (pages and not _can(perms, role, pages[0])):
            continue
        out.append({'slug': t['slug'], 'cat': t['cat'], 'icon': t['icon'], 'title': t['title'], 'summary': t.get('summary', ''),
                    'keywords': t.get('keywords', ''), 'steps': steps, 'guide_url': t.get('guide_url')})
    return out


def search(perms, role, query, limit=3):
    """Tours that answer a question (for Fit's replies: a "Show me" card)."""
    words = [w for w in re.findall(r'[a-z0-9]+', str(query or '').lower()) if len(w) > 2]
    scored = []
    for t in for_person(perms, role):
        hay = ' '.join([t['title'], t['summary'], t['keywords']]).lower()
        s = sum(hay.count(w) for w in words) + sum(3 for w in words if w in t['title'].lower())
        if s:
            scored.append((s, t))
    scored.sort(key=lambda x: -x[0])
    return [{'slug': t['slug'], 'title': t['title'], 'icon': t['icon'], 'steps': len(t['steps'])} for _, t in scored[:limit]]
