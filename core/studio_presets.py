"""Starter templates for the Portal Studio and the Voucher Design Studio.

Presets are plain JSON-able dicts. A saved page/design copies its preset once,
after which the owner edits their own copy — updating a preset never changes a
customer's live page.
"""
import copy

# key: (label, Google Fonts family query or '', CSS stack)
FONTS = {
    'system':   ('System',             '', 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif'),
    'outfit':   ('Outfit',             'Outfit:wght@400;600;800', '"Outfit",system-ui,sans-serif'),
    'sora':     ('Sora',               'Sora:wght@400;600;800', '"Sora",system-ui,sans-serif'),
    'grotesk':  ('Space Grotesk',      'Space+Grotesk:wght@400;600;700', '"Space Grotesk",system-ui,sans-serif'),
    'bricolage':('Bricolage Grotesque','Bricolage+Grotesque:opsz,wght@12..96,400;12..96,700;12..96,800', '"Bricolage Grotesque",system-ui,sans-serif'),
    'syne':     ('Syne',               'Syne:wght@500;700;800', '"Syne",system-ui,sans-serif'),
    'archivo':  ('Archivo Black',      'Archivo+Black', '"Archivo Black","Arial Black",sans-serif'),
    'fraunces': ('Fraunces',           'Fraunces:opsz,wght@9..144,400;9..144,700', '"Fraunces",Georgia,serif'),
    'dmserif':  ('DM Serif Display',   'DM+Serif+Display', '"DM Serif Display",Georgia,serif'),
    'nunito':   ('Nunito',             'Nunito:wght@400;700;900', '"Nunito",system-ui,sans-serif'),
    'rubik':    ('Rubik',              'Rubik:wght@400;600;800', '"Rubik",system-ui,sans-serif'),
    'mono':     ('JetBrains Mono',     'JetBrains+Mono:wght@400;700;800', '"JetBrains Mono",ui-monospace,monospace'),
    'caveat':   ('Caveat',             'Caveat:wght@500;700', '"Caveat","Comic Sans MS",cursive'),
    'bebas':    ('Bebas Neue',         'Bebas+Neue', '"Bebas Neue",Impact,sans-serif'),
}

_uid = 0


def _b(type_, **props):
    global _uid
    _uid += 1
    return {'id': f'{type_}{_uid}', 'type': type_, 'hidden': False, **props}


def _theme(**kw):
    base = {
        'layout': 'card', 'align': 'center', 'width': 420, 'radius': 22, 'shadow': 'soft', 'animation': 'rise',
        'font': 'outfit', 'heading_font': 'outfit',
        'bg': {'type': 'gradient', 'color1': '#1769e0', 'color2': '#0b2237', 'angle': 150, 'image': '', 'pattern': 'none', 'pattern_opacity': .12, 'overlay': .35},
        'surface': '#ffffff', 'surface_opacity': 1, 'text': '#102033', 'muted': '#65758a', 'accent': '#1769e0', 'accent_text': '#ffffff',
        'input_bg': '#f3f6fa', 'border': '#e3e9f1',
    }
    for k, v in kw.items():
        if k == 'bg': base['bg'] = {**base['bg'], **v}
        else: base[k] = v
    return base


# ───────────────────────────── Portal: login pages ─────────────────────────────
def _login_templates():
    T = {}
    T['atlantic'] = {
        'label': 'Atlantic Sunset', 'kind': 'login', 'venue': 'Beach bars · Kololi strip',
        'theme': _theme(font='outfit', heading_font='fraunces',
                        bg={'type': 'gradient', 'color1': '#ff8a5b', 'color2': '#1b2a6b', 'angle': 180, 'pattern': 'waves', 'pattern_opacity': .18},
                        accent='#ff6a3d', text='#1a1f3d', muted='#6b6f8c', radius=28, surface_opacity=.94),
        'blocks': [
            _b('logo', mode='image', size=64, shape='circle'),
            _b('heading', title='Sun’s out, Wi-Fi’s on', subtitle='Enter your voucher to get online.', size='lg'),
            _b('voucher', label='Voucher code', placeholder='e.g. K7Q2 M9XP', button='Get online', style='single', show_hint=True),
            _b('plans', title='Grab a voucher at the bar', style='chips', show_devices=False, highlight=''),
            _b('contact', phone='', whatsapp='', email='', hours='Open daily 10:00 – late'),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['market'] = {
        'label': 'Serrekunda Market', 'kind': 'login', 'venue': 'Shops · markets · kiosks',
        'theme': _theme(font='rubik', heading_font='archivo', layout='poster', align='left',
                        bg={'type': 'solid', 'color1': '#ffd23f', 'pattern': 'kente', 'pattern_opacity': .9},
                        surface='#ffffff', accent='#d7263d', text='#111111', muted='#4a4a4a', radius=6, shadow='hard'),
        'blocks': [
            _b('heading', title='FAST WI-FI HERE', subtitle='Buy a voucher at the counter. Type the code. Done.', size='xl'),
            _b('plans', title='Today’s prices', style='list', show_devices=True, highlight=''),
            _b('voucher', label='Your code', placeholder='8 characters', button='Connect', style='boxes', show_hint=False),
            _b('payment', title='Pay by mobile money', methods=[{'name': 'Wave', 'detail': 'Send to the shop number'}, {'name': 'QMoney', 'detail': 'Show the SMS at the counter'}], note='Voucher is given after payment.'),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['attaya'] = {
        'label': 'Attaya Café', 'kind': 'login', 'venue': 'Cafés · tea spots · restaurants',
        'theme': _theme(layout='split', font='nunito', heading_font='dmserif', align='left',
                        bg={'type': 'gradient', 'color1': '#1f3b2d', 'color2': '#0f1f18', 'angle': 160, 'pattern': 'leaves', 'pattern_opacity': .14},
                        surface='#f7fbf6', accent='#2f9e6a', text='#15261d', muted='#5b7063', radius=18, input_bg='#e9f3ec', border='#d6e6da'),
        'blocks': [
            _b('logo', mode='image', size=56, shape='rounded'),
            _b('heading', title='Pull up a chair.', subtitle='Your Wi-Fi code is printed on your receipt.', size='lg'),
            _b('voucher', label='Wi-Fi code', placeholder='From your receipt', button='Start browsing', style='single', show_hint=True),
            _b('notice', text='Free 30 minutes with every pot of attaya — ask your server.', tone='promo', icon='cup-hot'),
            _b('social', facebook='', instagram='', tiktok='', whatsapp=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['campus'] = {
        'label': 'Campus Grid', 'kind': 'login', 'venue': 'Schools · study centres · hostels',
        'theme': _theme(font='grotesk', heading_font='grotesk', width=460,
                        bg={'type': 'solid', 'color1': '#eef2ff', 'pattern': 'grid', 'pattern_opacity': .5},
                        accent='#3b3bff', text='#12123a', muted='#5d5f86', radius=14, shadow='hard', input_bg='#f5f6ff', border='#12123a'),
        'blocks': [
            _b('heading', title='Study mode: on.', subtitle='Log in with the voucher from the front desk.', size='lg'),
            _b('voucher', label='Voucher', placeholder='XXXXXXXX', button='Log in', style='boxes', show_hint=True),
            _b('plans', title='Student bundles', style='cards', show_devices=True, highlight='Weekly'),
            _b('steps', title='How it works', items=['Buy a voucher at the desk', 'Type the 8-character code', 'Browse until your time runs out']),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['night'] = {
        'label': 'Night Signal', 'kind': 'login', 'venue': 'Lounges · clubs · game centres',
        'theme': _theme(font='syne', heading_font='syne',
                        bg={'type': 'gradient', 'color1': '#1a0633', 'color2': '#05010d', 'angle': 200, 'pattern': 'dots', 'pattern_opacity': .25},
                        surface='#140a24', surface_opacity=.82, accent='#ff2fb3', accent_text='#ffffff', text='#f3e9ff', muted='#a992c9',
                        radius=20, shadow='glow', input_bg='#221338', border='#3a2360'),
        'blocks': [
            _b('logo', mode='image', size=58, shape='circle'),
            _b('heading', title='Plug in.', subtitle='Drop your code and stay connected all night.', size='xl'),
            _b('voucher', label='Code', placeholder='• • • • • • • •', button='Go live', style='single', show_hint=False),
            _b('plans', title='Night passes', style='chips', show_devices=False, highlight=''),
            _b('social', facebook='', instagram='', tiktok='', whatsapp=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['guesthouse'] = {
        'label': 'Guesthouse Welcome', 'kind': 'login', 'venue': 'Hotels · lodges · Airbnbs',
        'theme': _theme(font='nunito', heading_font='fraunces', layout='full',
                        bg={'type': 'gradient', 'color1': '#0e4d64', 'color2': '#137177', 'angle': 135, 'pattern': 'palms', 'pattern_opacity': .12, 'overlay': .2},
                        surface='#ffffff', surface_opacity=.18, accent='#f2c14e', accent_text='#1b1b1b', text='#ffffff', muted='#d8eef0',
                        radius=24, shadow='none', input_bg='rgba(255,255,255,.14)', border='rgba(255,255,255,.35)'),
        'blocks': [
            _b('logo', mode='image', size=70, shape='circle'),
            _b('heading', title='Welcome, make yourself at home', subtitle='Your Wi-Fi voucher is in your welcome pack.', size='lg'),
            _b('voucher', label='Guest voucher', placeholder='Voucher code', button='Connect my device', style='single', show_hint=True),
            _b('notice', text='Need more time? Reception can extend your voucher any time.', tone='info', icon='bell'),
            _b('contact', phone='', whatsapp='', email='', hours='Reception 24/7'),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['mono'] = {
        'label': 'Just the Code', 'kind': 'login', 'venue': 'Anywhere — quickest to use',
        'theme': _theme(font='mono', heading_font='mono', width=380,
                        bg={'type': 'solid', 'color1': '#ffffff', 'pattern': 'none'},
                        surface='#ffffff', accent='#000000', accent_text='#ffffff', text='#000000', muted='#666666',
                        radius=0, shadow='none', animation='fade', input_bg='#ffffff', border='#000000'),
        'blocks': [
            _b('heading', title='Wi-Fi', subtitle='Enter voucher.', size='xl'),
            _b('voucher', label='', placeholder='________', button='Connect', style='single', show_hint=False),
            _b('footer', text='TapTap'),
        ]}
    T['ticket'] = {
        'label': 'Garage Ticket', 'kind': 'login', 'venue': 'Bus garages · ferry terminals · transport',
        'theme': _theme(layout='ticket', font='rubik', heading_font='bebas',
                        bg={'type': 'solid', 'color1': '#1d1d1f', 'pattern': 'stripes', 'pattern_opacity': .08},
                        surface='#fff6e0', accent='#e4572e', text='#1d1d1f', muted='#6a5f4a', radius=14, input_bg='#ffffff', border='#d8c9a3'),
        'blocks': [
            _b('heading', title='WI-FI PASS', subtitle='Valid on this hotspot only', size='xl'),
            _b('voucher', label='Pass number', placeholder='Enter pass number', button='Board online', style='boxes', show_hint=True),
            _b('plans', title='Fares', style='list', show_devices=False, highlight=''),
            _b('footer', text='Keep your pass until your trip ends · TapTap'),
        ]}
    T['palm'] = {
        'label': 'Palm Grove', 'kind': 'login', 'venue': 'Gardens · eco-lodges · events',
        'theme': _theme(font='outfit', heading_font='bricolage',
                        bg={'type': 'gradient', 'color1': '#7cc47f', 'color2': '#1f6f4a', 'angle': 170, 'pattern': 'palms', 'pattern_opacity': .2},
                        accent='#1f6f4a', text='#123524', muted='#5d7a69', radius=26, input_bg='#eef7f0', border='#d3e7d9'),
        'blocks': [
            _b('logo', mode='image', size=60, shape='rounded'),
            _b('heading', title='Relax. You’re connected.', subtitle='Use your voucher to join the network.', size='lg'),
            _b('voucher', label='Voucher', placeholder='Voucher code', button='Join', style='single', show_hint=True),
            _b('image', src='', alt='Promotion', radius=16, link=''),
            _b('plans', title='Passes', style='cards', show_devices=True, highlight=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['stadium'] = {
        'label': 'Match Day', 'kind': 'login', 'venue': 'Viewing centres · sports bars',
        'theme': _theme(font='rubik', heading_font='bebas', width=440,
                        bg={'type': 'gradient', 'color1': '#0d5c2e', 'color2': '#083a1d', 'angle': 180, 'pattern': 'pitch', 'pattern_opacity': .35},
                        surface='#0b1d12', surface_opacity=.9, accent='#ffe600', accent_text='#0b1d12', text='#ffffff', muted='#a7c7b1',
                        radius=10, shadow='strong', input_bg='#13301f', border='#245a3a'),
        'blocks': [
            _b('heading', title='KICK-OFF IN 3…2…1', subtitle='Enter your voucher and never miss a goal.', size='xl'),
            _b('voucher', label='Voucher', placeholder='CODE', button='Connect', style='boxes', show_hint=False),
            _b('plans', title='Match passes', style='chips', show_devices=False, highlight=''),
            _b('notice', text='Big match tonight — get a 24h pass before kick-off.', tone='promo', icon='trophy'),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['wallet'] = {
        'label': 'Pay & Connect', 'kind': 'login', 'venue': 'Mobile-money agents · self-service',
        'theme': _theme(layout='sheet', font='sora', heading_font='sora',
                        bg={'type': 'gradient', 'color1': '#1dc8ff', 'color2': '#6a4dff', 'angle': 140, 'pattern': 'dots', 'pattern_opacity': .15},
                        accent='#6a4dff', text='#15123b', muted='#6c6a8e', radius=26, input_bg='#f1f0ff', border='#e2e0ff'),
        'blocks': [
            _b('heading', title='Get online in a minute', subtitle='Pay with mobile money, get your code by SMS.', size='md'),
            _b('plans', title='Choose a plan', style='cards', show_devices=True, highlight='24 Hours'),
            _b('payment', title='How to pay', methods=[{'name': 'Wave', 'detail': 'Send to 000 0000'}, {'name': 'QMoney', 'detail': 'Send to 000 0000'}, {'name': 'Afrimoney', 'detail': 'Send to 000 0000'}], note='Send the plan price and your code arrives by SMS.'),
            _b('voucher', label='Already have a code?', placeholder='Voucher code', button='Connect', style='single', show_hint=True),
            _b('contact', phone='', whatsapp='', email='', hours=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['river'] = {
        'label': 'River Bands', 'kind': 'login', 'venue': 'Community centres · national pride',
        'theme': _theme(font='outfit', heading_font='bricolage', layout='bands',
                        bg={'type': 'solid', 'color1': '#ffffff', 'pattern': 'none'},
                        accent='#0c1c8c', text='#10131f', muted='#5a6072', radius=16, input_bg='#f3f5fb', border='#dde2ee', shadow='none'),
        'blocks': [
            _b('logo', mode='image', size=56, shape='circle'),
            _b('heading', title='Community Wi-Fi', subtitle='Smiling Coast, always connected.', size='lg'),
            _b('voucher', label='Voucher', placeholder='Voucher code', button='Connect', style='single', show_hint=True),
            _b('plans', title='Prices', style='list', show_devices=True, highlight=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['spotlight'] = {
        'label': 'Sponsored Spotlight', 'kind': 'login', 'venue': 'Earn from local advertisers',
        'theme': _theme(font='sora', heading_font='sora', width=440,
                        bg={'type': 'gradient', 'color1': '#0f2a4a', 'color2': '#071524', 'angle': 170, 'pattern': 'dots', 'pattern_opacity': .18},
                        accent='#ffb020', accent_text='#1a1300', radius=20),
        'blocks': [
            _b('logo', mode='image', size=56, shape='rounded'),
            _b('heading', title='{business} Wi-Fi', subtitle='Type your voucher to connect.', size='lg'),
            _b('voucher', label='Voucher code', placeholder='8 characters', button='Connect', style='single', show_hint=True),
            _b('ads', style='carousel', label='Sponsored', rotate=6, skip=5),
            _b('plans', title='Prices', style='chips', show_devices=False, highlight=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['freebie'] = {
        'label': 'Free Taster', 'kind': 'login', 'venue': 'Let people try before they buy',
        'theme': _theme(font='nunito', heading_font='bricolage',
                        bg={'type': 'gradient', 'color1': '#16a34a', 'color2': '#064e3b', 'angle': 160, 'pattern': 'leaves', 'pattern_opacity': .14},
                        accent='#16a34a', text='#0f2418', muted='#4d6b58', radius=22, input_bg='#eefaf2', border='#cfe9d8'),
        'blocks': [
            _b('heading', title='Try it free, then stay online', subtitle='5 free minutes on us. Like it? Grab a voucher.', size='lg'),
            _b('trial', text='Start my free 5 minutes', note='One free trial per phone per day.'),
            _b('voucher', label='Have a voucher?', placeholder='Voucher code', button='Connect', style='single', show_hint=True),
            _b('plans', title='Vouchers', style='cards', show_devices=True, highlight=''),
            _b('faq', title='Questions', items=[{'name': 'Where do I buy a voucher?', 'detail': 'At the counter or from our agents nearby.'},
                                                {'name': 'Can I share it?', 'detail': 'A voucher works on the number of devices printed on it.'}]),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['tabaski'] = {
        'label': 'Festive Greetings', 'kind': 'login', 'venue': 'Tabaski · Koriteh · Christmas · New Year',
        'theme': _theme(font='outfit', heading_font='dmserif', layout='full',
                        bg={'type': 'gradient', 'color1': '#5b1a7a', 'color2': '#1a0b2e', 'angle': 145, 'pattern': 'dots', 'pattern_opacity': .2},
                        surface='#fffaf2', accent='#c8961e', accent_text='#1a0b2e', text='#2a1438', muted='#7a6488', radius=24, shadow='glow'),
        'blocks': [
            _b('logo', mode='image', size=62, shape='circle'),
            _b('heading', title='Season’s greetings from {business}', subtitle='Stay close to family with fast Wi-Fi.', size='lg'),
            _b('ticker', items=['Festive offer: extra time on every weekly pass', 'Share the joy — gift a voucher'], icon='gift'),
            _b('voucher', label='Voucher code', placeholder='Voucher code', button='Connect', style='single', show_hint=True),
            _b('ads', style='card', label='Festive offer', rotate=6, skip=5),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['kiosk'] = {
        'label': 'Self-Serve Kiosk', 'kind': 'login', 'venue': 'Unattended sites · buy by mobile money',
        'theme': _theme(font='grotesk', heading_font='grotesk', align='left',
                        bg={'type': 'solid', 'color1': '#0b1220', 'pattern': 'grid', 'pattern_opacity': .12},
                        surface='#111a2e', accent='#22d3ee', accent_text='#06202a', text='#e7f0ff', muted='#93a4c3', radius=14, input_bg='#18243d', border='#26375a'),
        'blocks': [
            _b('heading', title='No attendant? No problem.', subtitle='Pay by mobile money, then type your code.', size='md'),
            _b('steps', title='3 steps', items=['Pick a plan below', 'Pay by Wave, QMoney or Afrimoney', 'Type the code from your SMS']),
            _b('plans', title='Plans', style='list', show_devices=True, highlight=''),
            _b('payment', title='Pay to', methods=[{'name': 'Wave', 'detail': 'Send to 000 0000'}, {'name': 'QMoney', 'detail': 'Send to 000 0000'}], note='Your code arrives by SMS within a minute.'),
            _b('voucher', label='Your code', placeholder='Code from SMS', button='Connect', style='boxes', show_hint=False),
            _b('contact', phone='', whatsapp='', email='', hours='Help 8:00 – 22:00'),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['brand'] = {
        'label': 'Brand First', 'kind': 'login', 'venue': 'Your logo front and centre',
        'theme': _theme(font='outfit', heading_font='sora', width=420,
                        bg={'type': 'gradient', 'color1': '#eef3fb', 'color2': '#dde7f5', 'angle': 180, 'pattern': 'none', 'pattern_opacity': 0},
                        accent='#1769e0', radius=24),
        'blocks': [
            _b('logo', mode='image', size=112, shape='rounded'),
            _b('heading', title='Welcome to {business}', subtitle='Enter your voucher code to get online.', size='lg'),
            _b('voucher', label='Voucher code', placeholder='Enter code', button='Connect', style='boxes', length=8, show_hint=True),
            _b('plans', title='Prices', style='chips', show_devices=False, highlight=''),
            _b('contact', title='Need help?', show_phone=True),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['spinwin'] = {
        'label': 'Spin & Win', 'kind': 'login', 'venue': 'Promote your Bonanza',
        'theme': _theme(font='nunito', heading_font='bricolage', width=440,
                        bg={'type': 'gradient', 'color1': '#7c2d12', 'color2': '#1c0a02', 'angle': 165, 'pattern': 'dots', 'pattern_opacity': .16},
                        accent='#f59e0b', accent_text='#1a1300', radius=22),
        'blocks': [
            _b('logo', mode='image', size=64, shape='circle'),
            _b('heading', title='{business} Wi-Fi', subtitle='Connect, then spin the wheel to win free Wi-Fi and prizes.', size='lg'),
            _b('voucher', label='Voucher code', placeholder='Enter code', button='Connect', style='single', show_hint=True),
            _b('bonanza', text='Spin & win', note='Every voucher from selected plans gets a spin.'),
            _b('plans', title='Vouchers', style='cards', show_devices=True, highlight=''),
            _b('footer', text='Powered by TapTap'),
        ]}
    T['plain'] = {
        'label': 'Plain & Fast', 'kind': 'login', 'venue': 'Loads instantly on weak phones',
        'theme': _theme(font='system', heading_font='system', width=380,
                        bg={'type': 'solid', 'color1': '#ffffff', 'color2': '#ffffff', 'angle': 180, 'pattern': 'none', 'pattern_opacity': 0},
                        accent='#111827', radius=12),
        'blocks': [
            _b('logo', mode='image', size=48, shape='rounded'),
            _b('heading', title='{business}', subtitle='Type your voucher code.', size='md'),
            _b('voucher', label='Voucher code', placeholder='Code', button='Connect', style='single', show_hint=False),
            _b('footer', text='Help: {phone}'),
        ]}
    return T


# ───────────────────────────── Portal: redirect / status pages ─────────────────────────────
def _redirect_templates():
    T = {}
    T['ring'] = {
        'label': 'Countdown Ring', 'kind': 'redirect', 'venue': 'Clear, calm hand-off',
        'theme': _theme(font='outfit', heading_font='outfit', bg={'type': 'gradient', 'color1': '#1769e0', 'color2': '#0b2237', 'angle': 150, 'pattern': 'waves', 'pattern_opacity': .12}),
        'settings': {'redirect_url': '', 'redirect_delay': 5},
        'blocks': [
            _b('countdown', text='You’re online! Taking you on in', style='ring'),
            _b('heading', title='Connected', subtitle='Enjoy your browsing.', size='md'),
            _b('button', text='Continue now', url='', style='solid'),
        ]}
    T['promo'] = {
        'label': 'Thank You + Offer', 'kind': 'redirect', 'venue': 'Promote a product while they wait',
        'theme': _theme(font='nunito', heading_font='bricolage', bg={'type': 'gradient', 'color1': '#ffb347', 'color2': '#ff5e62', 'angle': 135, 'pattern': 'dots', 'pattern_opacity': .15},
                        accent='#ff5e62', radius=24),
        'settings': {'redirect_url': '', 'redirect_delay': 8},
        'blocks': [
            _b('heading', title='You’re connected — thank you!', subtitle='Here’s something for your next visit.', size='md'),
            _b('image', src='', alt='Offer', radius=18, link=''),
            _b('notice', text='Show this screen for 10% off your next voucher.', tone='promo', icon='gift'),
            _b('countdown', text='Continuing in', style='bar'),
            _b('button', text='Continue', url='', style='solid'),
        ]}
    T['session'] = {
        'label': 'Session Card', 'kind': 'redirect', 'venue': 'Show time left and device info',
        'theme': _theme(font='grotesk', heading_font='grotesk', bg={'type': 'solid', 'color1': '#0f172a', 'pattern': 'grid', 'pattern_opacity': .15},
                        surface='#111c33', accent='#38bdf8', accent_text='#06121f', text='#e7f0ff', muted='#8ea3c2', input_bg='#16233f', border='#23365c'),
        'settings': {'redirect_url': '', 'redirect_delay': 10},
        'blocks': [
            _b('heading', title='Session started', subtitle='Keep this page to check your time.', size='md'),
            _b('session', show=['plan', 'time_left', 'uptime', 'ip', 'mac', 'data']),
            _b('countdown', text='Opening your page in', style='number'),
            _b('button', text='Log out', url='{logout}', style='outline'),
        ]}
    T['social'] = {
        'label': 'Follow Us', 'kind': 'redirect', 'venue': 'Grow your socials',
        'theme': _theme(font='sora', heading_font='sora', bg={'type': 'gradient', 'color1': '#6a4dff', 'color2': '#1dc8ff', 'angle': 160}, accent='#6a4dff'),
        'settings': {'redirect_url': '', 'redirect_delay': 12},
        'blocks': [
            _b('logo', mode='image', size=64, shape='circle'),
            _b('heading', title='You’re in!', subtitle='Follow us for promos and free-time giveaways.', size='md'),
            _b('social', facebook='', instagram='', tiktok='', whatsapp=''),
            _b('countdown', text='Continuing in', style='bar'),
        ]}
    T['bounce'] = {
        'label': 'Instant Go', 'kind': 'redirect', 'venue': 'No fuss, one-second hand-off',
        'theme': _theme(font='system', heading_font='system', bg={'type': 'solid', 'color1': '#ffffff'}, radius=0, shadow='none', animation='fade'),
        'settings': {'redirect_url': '', 'redirect_delay': 1},
        'blocks': [_b('countdown', text='Connected', style='spinner')]}
    T['guest'] = {
        'label': 'Guest Welcome', 'kind': 'redirect', 'venue': 'Hotels: menu, services, reception',
        'theme': _theme(font='nunito', heading_font='fraunces', layout='full',
                        bg={'type': 'gradient', 'color1': '#0e4d64', 'color2': '#137177', 'angle': 135, 'pattern': 'palms', 'pattern_opacity': .12},
                        surface='#ffffff', surface_opacity=.16, accent='#f2c14e', accent_text='#1b1b1b', text='#ffffff', muted='#d8eef0', shadow='none'),
        'settings': {'redirect_url': '', 'redirect_delay': 15},
        'blocks': [
            _b('heading', title='Enjoy your stay', subtitle='You’re connected. Here’s what we can do for you.', size='lg'),
            _b('button', text='Room service menu', url='', style='outline'),
            _b('button', text='Book a tour', url='', style='outline'),
            _b('contact', phone='', whatsapp='', email='', hours='Reception 24/7'),
            _b('countdown', text='Continuing in', style='bar'),
        ]}
    T['status_card'] = {
        'label': 'Live Status', 'kind': 'status', 'venue': 'Router status.html page',
        'theme': _theme(font='outfit', heading_font='outfit', bg={'type': 'gradient', 'color1': '#10263d', 'color2': '#1769e0', 'angle': 200, 'pattern': 'dots', 'pattern_opacity': .12}),
        'settings': {'redirect_url': '', 'redirect_delay': 0},
        'blocks': [
            _b('heading', title='You’re online', subtitle='Your session at a glance.', size='md'),
            _b('session', show=['plan', 'time_left', 'uptime', 'data', 'ip']),
            _b('plans', title='Need more time?', style='chips', show_devices=False, highlight=''),
            _b('button', text='Log out', url='{logout}', style='outline'),
        ]}
    T['sponsor'] = {
        'label': 'Sponsor Moment', 'kind': 'redirect', 'venue': 'Full-screen advert with a skip button',
        'theme': _theme(font='sora', heading_font='sora', bg={'type': 'gradient', 'color1': '#0f2a4a', 'color2': '#071524'}, accent='#ffb020', accent_text='#1a1300'),
        'blocks': [
            _b('ads', style='interstitial', label='A word from our sponsor', rotate=6, skip=5),
            _b('heading', title='You’re connected', subtitle='Thanks for choosing {business}.', size='md'),
            _b('countdown', text='Continuing in', style='bar'),
            _b('button', text='Continue', url='', style='solid'),
        ]}
    T['adwall'] = {
        'label': 'Offers Wall', 'kind': 'redirect', 'venue': 'Rotate several advertisers after login',
        'theme': _theme(font='rubik', heading_font='archivo', bg={'type': 'solid', 'color1': '#fff4d6', 'pattern': 'stripes', 'pattern_opacity': .08},
                        accent='#e4572e', text='#1d1d1f', muted='#6a5f4a', radius=16),
        'blocks': [
            _b('heading', title='You’re online!', subtitle='Deals from businesses near you:', size='md'),
            _b('ads', style='carousel', label='', rotate=5, skip=5),
            _b('ticker', items=['Advertise here — ask {business} at the counter'], icon='bell'),
            _b('countdown', text='Opening your page in', style='number'),
        ]}
    T['status_promo'] = {
        'label': 'Status + Offer', 'kind': 'status', 'venue': 'Session info with a sponsor card',
        'theme': _theme(font='outfit', bg={'type': 'gradient', 'color1': '#1769e0', 'color2': '#0b2237'}),
        'blocks': [
            _b('heading', title='You’re online', subtitle='Your session at a glance.', size='md'),
            _b('session', show=['plan', 'time_left', 'uptime', 'data']),
            _b('ads', style='banner', label='Sponsored', rotate=6, skip=5),
            _b('plans', title='Need more time?', style='chips', show_devices=False, highlight=''),
            _b('button', text='Log out', url='{logout}', style='outline'),
        ]}
    return T


PORTAL_TEMPLATES = {**_login_templates(), **_redirect_templates()}


def portal_template(key, kind=None):
    t = PORTAL_TEMPLATES.get(key)
    if not t or (kind and t['kind'] != kind):
        t = next(v for v in PORTAL_TEMPLATES.values() if v['kind'] == (kind or 'login'))
    cfg = {'theme': copy.deepcopy(t['theme']), 'blocks': copy.deepcopy(t['blocks']),
           'settings': copy.deepcopy(t.get('settings', {'redirect_url': '', 'redirect_delay': 5}))}
    return cfg


def portal_gallery():
    return [{'key': k, 'label': v['label'], 'kind': v['kind'], 'venue': v['venue'], 'config': portal_template(k)} for k, v in PORTAL_TEMPLATES.items()]


# ───────────────────────────── Voucher card designs ─────────────────────────────
def _el(type_, x, y, w, h, **kw):
    global _uid
    _uid += 1
    base = {'id': f'e{_uid}', 'type': type_, 'x': x, 'y': y, 'w': w, 'h': h, 'opacity': 1, 'locked': False}
    if type_ in {'text', 'code'}:
        base.update({'text': '', 'size': 9, 'weight': 600, 'color': '#102033', 'align': 'left', 'font': '', 'spacing': 0, 'upper': False, 'italic': False, 'bg': '', 'radius': 0})
    if type_ == 'code':
        base.update({'text': '{code}', 'size': 16, 'weight': 800, 'spacing': 2, 'align': 'center', 'group': 4, 'bg': '', 'border': '#102033', 'border_width': 0, 'radius': 2})
    if type_ == 'qr':
        base.update({'content': 'login', 'color': '#102033', 'bg': '#ffffff', 'margin': 1})
    if type_ in {'rect', 'ellipse'}:
        base.update({'fill': '#1769e0', 'stroke': '', 'stroke_width': 0, 'radius': 0})
    if type_ == 'line':
        base.update({'stroke': '#102033', 'stroke_width': .3, 'dash': 'solid'})
    if type_ == 'logo':
        base.update({'shape': 'rounded', 'fallback': 'initials', 'color': '#ffffff', 'bg': '#1769e0'})
    if type_ == 'icon':
        base.update({'icon': 'wifi', 'color': '#1769e0'})
    base.update(kw)
    return base


def _card(**kw):
    base = {'size': {'w': 85, 'h': 54, 'preset': 'card'}, 'font': 'outfit',
            'bg': {'type': 'solid', 'color1': '#ffffff', 'color2': '#e9f2ff', 'angle': 135, 'image': '', 'pattern': 'none', 'pattern_opacity': .12},
            'border': {'width': .25, 'color': '#d6dee8', 'radius': 3, 'style': 'solid'},
            'page': {'paper': 'A4', 'margin': 6, 'gap': 3, 'cut_marks': True},
            'qr_mode': 'login', 'elements': []}
    for k, v in kw.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


def _voucher_templates():
    T = {}
    T['classic'] = {'label': 'Classic Blue', 'note': 'Clean, business-card size',
        'config': _card(elements=[
            _el('rect', 0, 0, 85, 14, fill='#1769e0'),
            _el('logo', 4, 2.5, 9, 9, shape='circle', bg='#ffffff', color='#1769e0'),
            _el('text', 15, 3, 45, 5, text='{business}', size=10, weight=800, color='#ffffff'),
            _el('text', 15, 8, 45, 4, text='Wi-Fi: {ssid}', size=6.5, weight=500, color='#d7e8ff'),
            _el('text', 58, 3.2, 24, 8, text='{currency}{price}', size=14, weight=800, color='#ffffff', align='right'),
            _el('text', 4, 17, 50, 4, text='{plan} · {duration} · {devices}', size=7, weight=600, color='#465a70'),
            _el('code', 4, 23, 52, 11, size=17, border='#1769e0', border_width=.4, radius=2, color='#102033'),
            _el('qr', 60, 17, 21, 21),
            _el('text', 4, 37, 52, 8, text='Connect to {ssid}, open any page and type the code.', size=5.8, weight=500, color='#65758a'),
            _el('line', 4, 46.5, 77, .1, stroke='#d6dee8', stroke_width=.25, dash='dashed'),
            _el('text', 4, 48, 40, 4, text='#{serial}', size=5.5, weight=600, color='#8a9aab'),
            _el('text', 41, 48, 40, 4, text='Help: {phone}', size=5.5, weight=600, color='#8a9aab', align='right'),
        ])}
    T['sunset'] = {'label': 'Kololi Sunset', 'note': 'Warm gradient, beach-bar feel',
        'config': _card(font='bricolage', bg={'type': 'gradient', 'color1': '#ff8a5b', 'color2': '#6b2e8a', 'angle': 135, 'pattern': 'waves', 'pattern_opacity': .18},
                        border={'width': 0, 'radius': 4}, elements=[
            _el('text', 5, 4, 55, 6, text='{business}', size=11, weight=800, color='#ffffff'),
            _el('text', 5, 10, 55, 4, text='{plan} Wi-Fi pass', size=7, weight=500, color='#ffe6da'),
            _el('rect', 5, 18, 50, 14, fill='#ffffff', radius=2.5, opacity=.95),
            _el('code', 6, 19.5, 48, 11, size=17, color='#6b2e8a', border_width=0),
            _el('qr', 61, 5, 20, 20, color='#6b2e8a'),
            _el('text', 58, 27, 25, 7, text='{currency}{price}', size=15, weight=800, color='#ffffff', align='center'),
            _el('text', 5, 36, 52, 4, text='{duration} · up to {devices}', size=6.5, weight=600, color='#ffffff'),
            _el('text', 5, 42, 75, 8, text='Join “{ssid}”, open your browser, enter the code.', size=6, weight=500, color='#ffe6da'),
        ])}
    T['kente'] = {'label': 'Market Kente', 'note': 'Bold stripes, loud and friendly',
        'config': _card(font='archivo', bg={'type': 'solid', 'color1': '#ffffff'}, border={'width': .6, 'color': '#111111', 'radius': 1.5}, elements=[
            _el('rect', 0, 0, 7, 54, fill='#d7263d'), _el('rect', 7, 0, 3, 54, fill='#ffd23f'), _el('rect', 10, 0, 2, 54, fill='#1b998b'),
            _el('text', 15, 4, 44, 7, text='{business}', size=11, weight=800, color='#111111', upper=True),
            _el('text', 15, 11, 44, 4, text='{plan}', size=7.5, weight=600, color='#d7263d', upper=True),
            _el('text', 60, 3.5, 22, 9, text='{currency}{price}', size=16, weight=800, color='#111111', align='right'),
            _el('code', 15, 19, 66, 12, size=19, color='#111111', border='#111111', border_width=.6, radius=0, spacing=3),
            _el('text', 15, 34, 44, 4, text='{duration} · {devices}', size=6.5, weight=600, color='#111111'),
            _el('text', 15, 40, 44, 10, text='Wi-Fi “{ssid}” — type the code on the login page.', size=5.8, weight=500, color='#444444'),
            _el('qr', 64, 34, 17, 17, color='#111111'),
        ])}
    T['night'] = {'label': 'Neon Night', 'note': 'Dark card with glowing code',
        'config': _card(font='syne', bg={'type': 'gradient', 'color1': '#1a0633', 'color2': '#05010d', 'angle': 200, 'pattern': 'dots', 'pattern_opacity': .25},
                        border={'width': .4, 'color': '#ff2fb3', 'radius': 4}, elements=[
            _el('icon', 5, 4.5, 6, 6, icon='wifi', color='#ff2fb3'),
            _el('text', 13, 4.5, 45, 6, text='{business}', size=10, weight=800, color='#f3e9ff'),
            _el('text', 58, 4.5, 23, 6, text='{plan}', size=7, weight=700, color='#ff2fb3', align='right', upper=True),
            _el('code', 5, 16, 75, 14, size=21, color='#ffffff', border='#ff2fb3', border_width=.4, radius=3, bg='rgba(255,47,179,.12)', spacing=4),
            _el('text', 5, 35, 50, 5, text='{duration} · {devices} · {currency}{price}', size=7, weight=600, color='#c9b3ea'),
            _el('text', 5, 42, 55, 8, text='Join {ssid} and enter the code.', size=6, weight=500, color='#a992c9'),
            _el('qr', 62, 33, 18, 18, color='#1a0633', bg='#f3e9ff'),
        ])}
    T['receipt'] = {'label': 'Thermal Receipt', 'note': '58 mm roll printers (POS)',
        'config': _card(font='mono', size={'w': 54, 'h': 78, 'preset': 'thermal58'}, page={'paper': 'thermal58', 'margin': 2, 'gap': 4, 'cut_marks': False},
                        bg={'type': 'solid', 'color1': '#ffffff'}, border={'width': 0, 'radius': 0}, elements=[
            _el('text', 2, 2, 50, 6, text='{business}', size=10, weight=800, color='#000000', align='center', upper=True),
            _el('text', 2, 8, 50, 4, text='WI-FI VOUCHER', size=7, weight=700, color='#000000', align='center', spacing=1),
            _el('line', 2, 13, 50, .1, stroke='#000000', stroke_width=.3, dash='dashed'),
            _el('text', 2, 15, 50, 4, text='{plan} / {duration}', size=7, weight=600, color='#000000', align='center'),
            _el('code', 2, 20, 50, 11, size=17, color='#000000', border='#000000', border_width=.5, radius=0),
            _el('qr', 15, 33, 24, 24, color='#000000'),
            _el('text', 2, 58, 50, 4, text='SSID: {ssid}', size=6.5, weight=700, color='#000000', align='center'),
            _el('text', 2, 62, 50, 4, text='Price {currency}{price} · {devices}', size=6.5, weight=600, color='#000000', align='center'),
            _el('line', 2, 67, 50, .1, stroke='#000000', stroke_width=.3, dash='dashed'),
            _el('text', 2, 69, 50, 4, text='#{serial} · {created}', size=5.5, weight=500, color='#000000', align='center'),
            _el('text', 2, 73, 50, 4, text='Help {phone}', size=5.5, weight=500, color='#000000', align='center'),
        ])}
    T['compact'] = {'label': 'Pocket Slip', 'note': '36 per A4 sheet — cheap to print',
        'config': _card(font='grotesk', size={'w': 48, 'h': 28, 'preset': 'compact'}, page={'paper': 'A4', 'margin': 6, 'gap': 1.5, 'cut_marks': True},
                        border={'width': .25, 'color': '#9aa8b8', 'radius': 1.5, 'style': 'dashed'}, elements=[
            _el('text', 2.5, 2, 30, 4, text='{business}', size=6.5, weight=700, color='#102033'),
            _el('text', 30, 2, 15.5, 4, text='{currency}{price}', size=7, weight=800, color='#1769e0', align='right'),
            _el('code', 2.5, 8, 43, 8.5, size=13, color='#102033', border_width=0, bg='#eef4ff', radius=1.2),
            _el('text', 2.5, 18.5, 43, 3.5, text='{plan} · {duration} · {devices}', size=5.2, weight=600, color='#465a70'),
            _el('text', 2.5, 22.5, 43, 3.5, text='Wi-Fi: {ssid}', size=5, weight=500, color='#8a9aab'),
        ])}
    T['ticket'] = {'label': 'Garage Ticket', 'note': 'Tear-off stub with serial',
        'config': _card(font='rubik', size={'w': 90, 'h': 45, 'preset': 'ticket'}, bg={'type': 'solid', 'color1': '#fff6e0'},
                        border={'width': .3, 'color': '#d8c9a3', 'radius': 2.5}, elements=[
            _el('rect', 0, 0, 22, 45, fill='#e4572e'),
            _el('text', 2, 4, 18, 6, text='WI-FI', size=12, weight=800, color='#ffffff', align='center', font='bebas'),
            _el('text', 2, 11, 18, 4, text='PASS', size=8, weight=700, color='#ffe0d6', align='center', spacing=2),
            _el('text', 2, 30, 18, 4, text='No.', size=5.5, weight=600, color='#ffe0d6', align='center'),
            _el('text', 2, 34, 18, 5, text='{serial}', size=8, weight=800, color='#ffffff', align='center'),
            _el('line', 22, 2, .1, 41, stroke='#b59e6b', stroke_width=.35, dash='dotted'),
            _el('text', 26, 4, 40, 6, text='{business}', size=10, weight=800, color='#1d1d1f', font='bebas', spacing=1),
            _el('text', 26, 10, 40, 4, text='{plan} · {duration}', size=6.5, weight=600, color='#6a5f4a'),
            _el('code', 26, 16, 44, 11, size=16, color='#1d1d1f', border='#1d1d1f', border_width=.4, radius=1),
            _el('text', 26, 30, 44, 9, text='Connect to {ssid} · {currency}{price}', size=6, weight=600, color='#6a5f4a'),
            _el('qr', 72, 4, 15, 15, color='#1d1d1f', bg='#fff6e0'),
            _el('text', 71, 20, 17, 4, text='SCAN', size=5.5, weight=700, color='#6a5f4a', align='center', spacing=1),
        ])}
    T['palm'] = {'label': 'Palm Leaf', 'note': 'Fresh green, hospitality',
        'config': _card(font='outfit', bg={'type': 'gradient', 'color1': '#eaf7ee', 'color2': '#c7ead3', 'angle': 160, 'pattern': 'palms', 'pattern_opacity': .25},
                        border={'width': 0, 'radius': 4}, elements=[
            _el('logo', 5, 5, 10, 10, shape='rounded', bg='#1f6f4a'),
            _el('text', 17, 5, 45, 5, text='{business}', size=10, weight=800, color='#123524'),
            _el('text', 17, 10.5, 45, 4, text='Guest Wi-Fi · {plan}', size=6.5, weight=500, color='#3d6b52'),
            _el('code', 5, 20, 52, 12, size=17, color='#123524', bg='#ffffff', border_width=0, radius=2.5),
            _el('qr', 61, 18, 20, 20, color='#123524'),
            _el('text', 5, 36, 52, 4, text='{duration} · {devices} · {currency}{price}', size=6.5, weight=600, color='#1f6f4a'),
            _el('text', 5, 42, 76, 8, text='Join “{ssid}”. The login page opens by itself — just type your code.', size=5.8, weight=500, color='#3d6b52'),
        ])}
    T['minimal'] = {'label': 'Paper Minimal', 'note': 'Black on white, prints anywhere',
        'config': _card(font='grotesk', bg={'type': 'solid', 'color1': '#ffffff'}, border={'width': .3, 'color': '#000000', 'radius': 0}, elements=[
            _el('text', 5, 5, 50, 5, text='{business}', size=9, weight=700, color='#000000'),
            _el('text', 55, 5, 25, 5, text='{plan}', size=7, weight=500, color='#000000', align='right'),
            _el('code', 5, 17, 75, 14, size=24, color='#000000', border_width=0, spacing=5, weight=700),
            _el('line', 5, 34, 75, .1, stroke='#000000', stroke_width=.3),
            _el('text', 5, 37, 60, 4, text='{ssid} · {duration} · {currency}{price}', size=6.5, weight=500, color='#000000'),
            _el('text', 5, 43, 60, 4, text='#{serial}', size=6, weight=500, color='#666666'),
        ])}
    T['stadium'] = {'label': 'Match Pass', 'note': 'Scoreboard style for viewing centres',
        'config': _card(font='rubik', bg={'type': 'gradient', 'color1': '#0d5c2e', 'color2': '#083a1d', 'angle': 180, 'pattern': 'pitch', 'pattern_opacity': .4},
                        border={'width': 0, 'radius': 3}, elements=[
            _el('text', 5, 3.5, 50, 7, text='{business}', size=12, weight=800, color='#ffffff', font='bebas', spacing=1),
            _el('rect', 58, 3.5, 23, 8, fill='#ffe600', radius=1),
            _el('text', 58, 4.5, 23, 6, text='{currency}{price}', size=11, weight=800, color='#0b1d12', align='center', font='bebas'),
            _el('code', 5, 16, 52, 13, size=20, color='#ffe600', bg='#0b1d12', border_width=0, radius=1.5, font='bebas', spacing=3),
            _el('qr', 61, 15, 20, 20, color='#0b1d12', bg='#ffffff'),
            _el('text', 5, 33, 52, 5, text='{plan} · {duration}', size=8, weight=700, color='#ffffff', font='bebas', spacing=1),
            _el('text', 5, 41, 76, 8, text='Join {ssid} · enter code · enjoy the match', size=6, weight=500, color='#a7c7b1'),
        ])}
    T['river'] = {'label': 'River Bands', 'note': 'Red, blue and green bands',
        'config': _card(font='outfit', bg={'type': 'solid', 'color1': '#ffffff'}, border={'width': .25, 'color': '#dde2ee', 'radius': 2.5}, elements=[
            _el('rect', 0, 0, 85, 4, fill='#ce1126'), _el('rect', 0, 4, 85, .8, fill='#ffffff'), _el('rect', 0, 4.8, 85, 2.4, fill='#0c1c8c'),
            _el('rect', 0, 7.2, 85, .8, fill='#ffffff'), _el('rect', 0, 8, 85, 4, fill='#3a7728'),
            _el('text', 5, 15, 50, 5, text='{business}', size=10, weight=800, color='#10131f'),
            _el('text', 5, 20.5, 50, 4, text='{plan} · {duration} · {devices}', size=6.5, weight=600, color='#5a6072'),
            _el('code', 5, 27, 52, 11, size=17, color='#0c1c8c', border='#0c1c8c', border_width=.4, radius=2),
            _el('qr', 61, 16, 20, 20, color='#10131f'),
            _el('text', 60, 37, 22, 5, text='{currency}{price}', size=12, weight=800, color='#ce1126', align='center'),
            _el('text', 5, 42, 52, 8, text='Wi-Fi “{ssid}” · help {phone}', size=6, weight=500, color='#5a6072'),
        ])}
    T['square'] = {'label': 'Sticker Square', 'note': 'Square sticker with big QR',
        'config': _card(font='sora', size={'w': 60, 'h': 60, 'preset': 'square'}, bg={'type': 'gradient', 'color1': '#6a4dff', 'color2': '#1dc8ff', 'angle': 145},
                        border={'width': 0, 'radius': 6}, elements=[
            _el('text', 4, 4, 52, 5, text='{business}', size=9, weight=800, color='#ffffff', align='center'),
            _el('rect', 14, 11, 32, 32, fill='#ffffff', radius=3),
            _el('qr', 16, 13, 28, 28, color='#15123b'),
            _el('code', 4, 45, 52, 8, size=13, color='#ffffff', border_width=0, spacing=2),
            _el('text', 4, 53.5, 52, 4, text='{plan} · {currency}{price}', size=6, weight=600, color='#e8e4ff', align='center'),
        ])}
    # ── Dense / tiny formats: more vouchers per sheet ──
    T['micro'] = {'label': 'Micro Slip', 'note': '65 per A4 — smallest readable card',
        'config': _card(font='grotesk', size={'w': 38, 'h': 21, 'preset': 'micro'}, page={'paper': 'A4', 'margin': 5, 'gap': 1, 'cut_marks': True},
                        border={'width': .2, 'color': '#9aa8b8', 'radius': 1, 'style': 'dashed'}, elements=[
            _el('text', 1.5, 1.2, 24, 3.2, text='{business}', size=5.2, weight=700, color='#102033'),
            _el('text', 24, 1.2, 12.5, 3.2, text='{currency}{price}', size=5.8, weight=800, color='#1769e0', align='right'),
            _el('code', 1.5, 5.2, 35, 7.5, size=11, color='#102033', border_width=0, bg='#eef4ff', radius=1, spacing=1.5),
            _el('text', 1.5, 13.6, 35, 3, text='{plan} · {duration}', size=4.6, weight=600, color='#465a70'),
            _el('text', 1.5, 16.8, 35, 3, text='Wi-Fi: {ssid}', size=4.3, weight=500, color='#8a9aab'),
        ])}
    T['strip'] = {'label': 'Code Strip', 'note': '69 per A4 — one line per voucher, cut into strips',
        'config': _card(font='mono', size={'w': 64, 'h': 11, 'preset': 'strip'}, page={'paper': 'A4', 'margin': 6, 'gap': 1, 'cut_marks': True},
                        border={'width': .2, 'color': '#b8c2cc', 'radius': 0, 'style': 'dashed'}, elements=[
            _el('icon', 1.2, 2.5, 6, 6, icon='scissors', color='#9aa8b8'),
            _el('code', 8, 1.5, 32, 8, size=12, color='#000000', border_width=0, spacing=1.5, group=4),
            _el('text', 41, 1.3, 21.5, 4, text='{plan}', size=5.2, weight=700, color='#000000', align='right'),
            _el('text', 41, 5.6, 21.5, 4, text='{currency}{price} · {ssid}', size=4.6, weight=500, color='#555555', align='right'),
        ])}
    T['miniqr'] = {'label': 'Mini QR', 'note': '42 per A4 — scan to log in, code underneath',
        'config': _card(font='grotesk', size={'w': 30, 'h': 38, 'preset': 'miniqr'}, page={'paper': 'A4', 'margin': 6, 'gap': 1.5, 'cut_marks': True},
                        border={'width': .2, 'color': '#c9d3de', 'radius': 1.5}, elements=[
            _el('text', 1, 1, 28, 3.2, text='{business}', size=5, weight=800, color='#102033', align='center'),
            _el('qr', 5, 4.6, 20, 20),
            _el('code', 1, 25.5, 28, 6, size=9, color='#102033', border_width=0, spacing=.8, group=4),
            _el('text', 1, 32, 28, 4.5, text='{plan} · {currency}{price}', size=4.6, weight=600, color='#465a70', align='center'),
        ])}
    T['label21'] = {'label': 'Sticker Labels (L7160)', 'note': '21 per A4 on standard 63.5×38.1 mm label sheets',
        'config': _card(font='outfit', size={'w': 63.5, 'h': 38.1, 'preset': 'l7160'}, page={'paper': 'A4', 'margin': 7, 'gap': 2.5, 'cut_marks': False},
                        border={'width': 0, 'radius': 2}, elements=[
            _el('rect', 0, 0, 63.5, 9, fill='#1769e0'),
            _el('text', 3, 1.8, 40, 5.5, text='{business}', size=8.5, weight=800, color='#ffffff'),
            _el('text', 42, 1.8, 18.5, 5.5, text='{currency}{price}', size=9, weight=800, color='#ffffff', align='right'),
            _el('code', 3, 12, 38, 10, size=14, color='#102033', border='#1769e0', border_width=.35, radius=1.5),
            _el('qr', 44, 11, 16.5, 16.5),
            _el('text', 3, 24, 38, 4, text='{plan} · {duration}', size=6, weight=600, color='#465a70'),
            _el('text', 3, 29.5, 57, 6, text='Join {ssid}, open any page and type the code.', size=5.2, weight=500, color='#65758a'),
        ])}
    T['scratch'] = {'label': 'Scratch Card', 'note': 'Silver code panel like airtime cards',
        'config': _card(font='rubik', bg={'type': 'gradient', 'color1': '#0b3d91', 'color2': '#061a3a', 'angle': 125, 'pattern': 'circuit', 'pattern_opacity': .15},
                        border={'width': 0, 'radius': 3}, elements=[
            _el('icon', 4, 4, 7, 7, icon='wifi', color='#ffd23f'),
            _el('text', 13, 4, 45, 7, text='{business}', size=10, weight=800, color='#ffffff'),
            _el('text', 55, 3.5, 26, 8, text='{currency}{price}', size=15, weight=800, color='#ffd23f', align='right'),
            _el('rect', 4, 16, 77, 15, fill='#c9ced6', radius=2, stroke='#eef1f5', stroke_width=.4),
            _el('text', 4, 16.6, 77, 3.4, text='SCRATCH GENTLY TO REVEAL', size=4.6, weight=700, color='#6b7280', align='center', spacing=1),
            _el('code', 6, 20, 73, 10, size=17, color='#111827', border_width=0, spacing=3),
            _el('text', 4, 35, 50, 5, text='{plan} · {duration} · {devices}', size=6.5, weight=600, color='#dbe7ff'),
            _el('text', 4, 42, 60, 8, text='Join {ssid}, open your browser and enter the code. Help {phone}', size=5.5, weight=500, color='#9fb4dc'),
            _el('text', 62, 45, 19, 5, text='#{serial}', size=5, weight=600, color='#9fb4dc', align='right'),
        ])}
    T['pos'] = {'label': 'Shop Barcode', 'note': 'Barcode for till scanners + code for customers',
        'config': _card(font='grotesk', size={'w': 70, 'h': 40, 'preset': 'pos'}, border={'width': .3, 'color': '#102033', 'radius': 1.5}, elements=[
            _el('text', 3, 2.5, 44, 5, text='{business}', size=8, weight=800, color='#102033'),
            _el('text', 46, 2.5, 21, 5, text='{currency}{price}', size=9, weight=800, color='#102033', align='right'),
            _el('text', 3, 7.5, 64, 4, text='{plan} · {duration} · {devices}', size=5.8, weight=600, color='#465a70'),
            _el('code', 3, 12.5, 64, 9, size=15, color='#102033', border_width=0, bg='#f1f5f9', radius=1),
            _el('barcode', 3, 23, 64, 12, text='{code}', color='#000000', bg='#ffffff', show_text=False),
            _el('text', 3, 35.5, 64, 3.5, text='Wi-Fi {ssid} · #{serial}', size=4.8, weight=500, color='#8a9aab', align='center'),
        ])}
    T['sponsored'] = {'label': 'Sponsored Card', 'note': 'Business card with a paid advert strip',
        'config': _card(elements=[
            _el('logo', 4, 3, 9, 9, shape='circle'),
            _el('text', 15, 3.2, 44, 5, text='{business}', size=9.5, weight=800, color='#102033'),
            _el('text', 15, 8, 44, 4, text='{plan} · {duration}', size=6.5, weight=600, color='#465a70'),
            _el('text', 58, 3, 23, 8, text='{currency}{price}', size=14, weight=800, color='#1769e0', align='right'),
            _el('code', 4, 15, 52, 11, size=17, border='#1769e0', border_width=.4, radius=2, color='#102033'),
            _el('qr', 60, 13, 21, 21),
            _el('text', 4, 28, 52, 6, text='Join {ssid} and type the code.', size=5.8, weight=500, color='#65758a'),
            _el('advert', 3, 38, 79, 13, show='both', size=7, color='#102033', bg='#fff6df', radius=1.5, label='Sponsored'),
        ])}
    # ── more templates ──
    T['boarding'] = {'label': 'Boarding Pass', 'note': 'Airline-style ticket with a tear-off stub',
        'config': _card(font='grotesk', size={'w': 90, 'h': 45, 'preset': 'ticket'}, border={'width': 0, 'radius': 3},
                        bg={'type': 'solid', 'color1': '#ffffff'}, elements=[
            _el('rect', 0, 0, 62, 11, fill='#0b2a4a'),
            _el('icon', 3.5, 2.5, 6, 6, icon='wifi', color='#7cc4ff'),
            _el('text', 11, 2.2, 36, 4, text='{business}', size=8, weight=700, color='#ffffff'),
            _el('text', 11, 6.3, 36, 3.5, text='WI-FI BOARDING PASS', size=4.8, weight=700, color='#7cc4ff', spacing=1),
            _el('text', 44, 3, 16, 6, text='{currency}{price}', size=10, weight=800, color='#ffffff', align='right'),
            _el('text', 4, 13.5, 26, 3, text='PLAN', size=4.2, weight=700, color='#8a9aab', spacing=1),
            _el('text', 4, 16.5, 30, 4.5, text='{plan}', size=7.5, weight=700, color='#0b2a4a'),
            _el('text', 33, 13.5, 26, 3, text='VALID FOR', size=4.2, weight=700, color='#8a9aab', spacing=1),
            _el('text', 33, 16.5, 28, 4.5, text='{duration}', size=7.5, weight=700, color='#0b2a4a'),
            _el('code', 4, 23, 55, 10, size=16, color='#0b2a4a', border='#0b2a4a', border_width=.35, radius=1.5),
            _el('text', 4, 35, 55, 7, text='Gate: {ssid} · open any page and type the code.', size=5.2, weight=500, color='#65758a'),
            _el('line', 63, 2, .1, 41, stroke='#b8c4d2', stroke_width=.3, dash='dashed'),
            _el('qr', 67, 6, 19, 19, color='#0b2a4a'),
            _el('text', 64, 27, 25, 4, text='SCAN TO BOARD', size=4.6, weight=800, color='#0b2a4a', align='center', spacing=.6),
            _el('text', 64, 33, 25, 4, text='#{serial}', size=5, weight=600, color='#8a9aab', align='center'),
            _el('text', 64, 38, 25, 4, text='{devices}', size=5, weight=600, color='#8a9aab', align='center'),
        ])}
    T['gold'] = {'label': 'Gold VIP', 'note': 'Black and gold for premium plans',
        'config': _card(font='fraunces', bg={'type': 'gradient', 'color1': '#1a1a1a', 'color2': '#000000', 'angle': 160, 'pattern': 'grid', 'pattern_opacity': .06},
                        border={'width': .5, 'color': '#c9a24a', 'radius': 3}, elements=[
            _el('text', 5, 4, 50, 5, text='{business}', size=9, weight=700, color='#e6c373', spacing=.5),
            _el('text', 5, 9.5, 50, 4, text='VIP WI-FI', size=5.5, weight=700, color='#9c8150', spacing=2),
            _el('text', 55, 4, 25, 9, text='{currency}{price}', size=15, weight=700, color='#e6c373', align='right'),
            _el('line', 5, 16, 75, .1, stroke='#c9a24a', stroke_width=.2),
            _el('code', 5, 20, 52, 11, size=17, color='#f5e6c0', border='#c9a24a', border_width=.3, radius=1),
            _el('qr', 61, 19, 19, 19, color='#1a1a1a', bg='#f5e6c0', margin=1),
            _el('text', 5, 34, 52, 4, text='{plan} · {duration} · {devices}', size=6, weight=600, color='#cdb27a'),
            _el('text', 5, 41, 52, 8, text='Join {ssid}, open any page and enter your code.', size=5.5, weight=400, color='#9c8150', italic=True),
            _el('text', 58, 45, 23, 4, text='#{serial}', size=5, weight=600, color='#9c8150', align='right'),
        ])}
    T['campus'] = {'label': 'Campus Pass', 'note': 'Student ID look for schools and hostels',
        'config': _card(font='rubik', border={'width': .25, 'color': '#cfe3d6', 'radius': 3}, elements=[
            _el('rect', 0, 0, 85, 12, fill='#1f7a4c'),
            _el('logo', 4, 1.5, 9, 9, shape='rounded', bg='#ffffff', color='#1f7a4c'),
            _el('text', 15, 2.2, 45, 4.5, text='{business}', size=8.5, weight=800, color='#ffffff'),
            _el('text', 15, 6.7, 45, 3.5, text='STUDENT WI-FI PASS', size=5, weight=700, color='#c8f0d9', spacing=1),
            _el('text', 60, 2.5, 21, 7, text='{currency}{price}', size=12, weight=800, color='#ffffff', align='right'),
            _el('rect', 4, 15, 20, 24, fill='#eef7f1', radius=1.5),
            _el('icon', 8, 19, 12, 12, icon='devices', color='#1f7a4c'),
            _el('text', 4, 33, 20, 4, text='{devices}', size=5, weight=700, color='#1f7a4c', align='center'),
            _el('text', 27, 15, 32, 4, text='{plan} · {duration}', size=6.5, weight=700, color='#23473a'),
            _el('code', 27, 20.5, 34, 10, size=13, color='#102033', border='#1f7a4c', border_width=.35, radius=1.5),
            _el('text', 27, 32, 34, 7, text='Log in on {ssid}. Keep this card — do not share it.', size=5, weight=500, color='#65758a'),
            _el('qr', 63, 15, 18, 18, color='#1f7a4c'),
            _el('text', 63, 34, 18, 4, text='#{serial}', size=5, weight=600, color='#8a9aab', align='center'),
            _el('rect', 0, 44, 85, 10, fill='#f4faf6'),
            _el('text', 4, 46.3, 77, 5, text='Help: {phone}', size=5.5, weight=600, color='#1f7a4c', align='center'),
        ])}
    T['spinwin'] = {'label': 'Spin & Win', 'note': 'Promote your Bonanza on every card',
        'config': _card(font='nunito', bg={'type': 'gradient', 'color1': '#f59e0b', 'color2': '#c2410c', 'angle': 135, 'pattern': 'dots', 'pattern_opacity': .16},
                        border={'width': 0, 'radius': 4}, elements=[
            _el('ellipse', 62, -8, 30, 30, fill='#ffffff', opacity=.14),
            _el('icon', 70, 3, 9, 9, icon='gift', color='#ffffff'),
            _el('text', 5, 4, 55, 5, text='{business}', size=9, weight=900, color='#ffffff'),
            _el('text', 5, 9.5, 55, 5, text='SPIN & WIN FREE WI-FI', size=6.5, weight=900, color='#fff3c4', spacing=.6),
            _el('rect', 5, 17, 52, 14, fill='#ffffff', radius=3, opacity=.96),
            _el('code', 6, 18.5, 50, 11, size=16, color='#9a3412', border_width=0),
            _el('qr', 61, 16, 20, 20, color='#9a3412'),
            _el('text', 5, 34, 52, 4.5, text='{plan} · {duration} · {currency}{price}', size=6.5, weight=800, color='#ffffff'),
            _el('text', 5, 40, 76, 9, text='Connect with your code, then enter it on our Bonanza page to spin for prizes!', size=5.6, weight=700, color='#fff3c4'),
        ])}
    T['chalk'] = {'label': 'Chalkboard', 'note': 'Café blackboard, handwritten feel',
        'config': _card(font='caveat', bg={'type': 'solid', 'color1': '#23352b', 'pattern': 'grid', 'pattern_opacity': .05},
                        border={'width': 1.2, 'color': '#8b5e34', 'radius': 2}, elements=[
            _el('text', 5, 3.5, 55, 7, text='{business}', size=14, weight=700, color='#f4f1e8'),
            _el('text', 5, 11, 55, 5, text="Today's Wi-Fi: {plan}", size=9, weight=500, color='#d9d3c3'),
            _el('text', 57, 3.5, 23, 9, text='{currency}{price}', size=17, weight=700, color='#ffd966', align='right'),
            _el('code', 5, 19, 52, 12, size=17, color='#ffffff', border='#f4f1e8', border_width=.3, radius=1, font='mono'),
            _el('qr', 61, 17, 19, 19, color='#23352b', bg='#f4f1e8'),
            _el('text', 5, 34, 52, 5, text='{duration} · {devices}', size=9, weight=500, color='#d9d3c3'),
            _el('text', 5, 41, 75, 8, text='Join {ssid}, open a page, type the code. Enjoy your coffee!', size=8.5, weight=500, color='#f4f1e8'),
        ])}
    T['poster'] = {'label': 'Counter Card A7', 'note': 'Tall card with big QR and 3 steps',
        'config': _card(font='sora', size={'w': 74, 'h': 105, 'preset': 'a7'}, border={'width': .3, 'color': '#d6dee8', 'radius': 3}, elements=[
            _el('rect', 0, 0, 74, 22, fill='#1769e0'),
            _el('logo', 29, 3, 16, 16, shape='circle', bg='#ffffff', color='#1769e0'),
            _el('text', 4, 24, 66, 6, text='{business}', size=12, weight=800, color='#102033', align='center'),
            _el('text', 4, 30.5, 66, 5, text='{plan} · {duration} · {currency}{price}', size=7, weight=600, color='#465a70', align='center'),
            _el('qr', 20, 37, 34, 34),
            _el('code', 8, 73.5, 58, 11, size=17, color='#102033', border='#1769e0', border_width=.4, radius=2),
            _el('text', 6, 87, 62, 4, text='1. Join {ssid}', size=6.5, weight=700, color='#102033'),
            _el('text', 6, 91.5, 62, 4, text='2. Open any web page (or scan the QR)', size=6.5, weight=700, color='#102033'),
            _el('text', 6, 96, 62, 4, text='3. Type the code and tap Connect', size=6.5, weight=700, color='#102033'),
            _el('text', 6, 100.5, 62, 3.5, text='Help {phone} · #{serial}', size=5, weight=600, color='#8a9aab', align='center'),
        ])}
    T['duo'] = {'label': 'Two-Tone', 'note': 'Big price panel on the left',
        'config': _card(font='syne', border={'width': 0, 'radius': 3}, elements=[
            _el('rect', 0, 0, 28, 54, fill='#7c3aed'),
            _el('text', 2, 6, 24, 5, text='{plan}', size=7, weight=700, color='#e9dcff', align='center'),
            _el('text', 1, 14, 26, 12, text='{currency}{price}', size=19, weight=800, color='#ffffff', align='center'),
            _el('text', 2, 29, 24, 5, text='{duration}', size=7, weight=700, color='#e9dcff', align='center'),
            _el('icon', 9.5, 38, 9, 9, icon='wifi', color='#ffffff'),
            _el('text', 32, 4, 49, 5, text='{business}', size=9, weight=800, color='#2e1065'),
            _el('text', 32, 9.5, 49, 4, text='Wi-Fi: {ssid}', size=6, weight=600, color='#6b5a8e'),
            _el('code', 32, 16, 49, 11, size=15, color='#2e1065', border='#7c3aed', border_width=.4, radius=2),
            _el('qr', 32, 30, 18, 18, color='#2e1065'),
            _el('text', 52, 30, 30, 14, text='Scan or open any page, then type the code.', size=5.6, weight=600, color='#6b5a8e'),
            _el('text', 52, 46, 29, 4, text='#{serial}', size=5, weight=600, color='#9c8fb8', align='right'),
        ])}
    T['label7160'] = {'label': 'Avery L7160 Label', 'note': '21 sticky labels per A4 (63.5×38.1)',
        'config': _card(font='outfit', size={'w': 63.5, 'h': 38.1, 'preset': 'l7160'}, border={'width': 0, 'radius': 2},
                        page={'paper': 'A4', 'margin': 7, 'gap': 2.5, 'cut_marks': False}, elements=[
            _el('text', 3, 2.5, 38, 4.5, text='{business}', size=7.5, weight=800, color='#102033'),
            _el('text', 3, 7, 38, 3.5, text='{plan} · {duration}', size=5.5, weight=600, color='#465a70'),
            _el('text', 40, 2.5, 20.5, 5, text='{currency}{price}', size=9, weight=800, color='#1769e0', align='right'),
            _el('code', 3, 12, 38, 9.5, size=13, color='#102033', border='#1769e0', border_width=.35, radius=1.5),
            _el('qr', 44, 11, 16, 16),
            _el('text', 3, 23.5, 38, 7, text='Join {ssid}, open a page, type the code.', size=5, weight=500, color='#65758a'),
            _el('text', 3, 32, 57.5, 3.5, text='#{serial} · Help {phone}', size=4.5, weight=600, color='#8a9aab'),
        ])}
    T['festive'] = {'label': 'Festive Green', 'note': 'Tabaski · Koriteh · Christmas specials',
        'config': _card(font='dmserif', bg={'type': 'gradient', 'color1': '#0f5132', 'color2': '#052e1c', 'angle': 150, 'pattern': 'kente', 'pattern_opacity': .08},
                        border={'width': .5, 'color': '#d4af37', 'radius': 3}, elements=[
            _el('icon', 4, 3.5, 7, 7, icon='star', color='#d4af37'),
            _el('text', 12.5, 3.5, 46, 5.5, text='{business}', size=10, weight=400, color='#f7f1dc'),
            _el('text', 12.5, 9, 46, 4, text='Season’s greetings — enjoy free-flowing Wi-Fi', size=5.5, weight=400, color='#d9cfa8', italic=True),
            _el('text', 58, 3.5, 23, 9, text='{currency}{price}', size=14, weight=700, color='#d4af37', align='right'),
            _el('rect', 4, 17, 53, 14, fill='#f7f1dc', radius=2),
            _el('code', 5, 18.5, 51, 11, size=16, color='#0f5132', border_width=0, font='mono'),
            _el('qr', 61, 16, 20, 20, color='#0f5132', bg='#f7f1dc'),
            _el('text', 4, 35, 53, 4, text='{plan} · {duration} · {devices}', size=6.5, weight=400, color='#f7f1dc'),
            _el('text', 4, 41, 77, 8, text='Join {ssid}, open any page and type your code. Help {phone}', size=5.8, weight=400, color='#d9cfa8'),
        ])}
    return T


VOUCHER_TEMPLATES = _voucher_templates()
CARD_SIZES = {'card': ('Business card 85×54', 85, 54), 'compact': ('Pocket slip 48×28', 48, 28), 'ticket': ('Ticket 90×45', 90, 45),
              'square': ('Square 60×60', 60, 60), 'a7': ('A7 portrait 74×105', 74, 105), 'thermal58': ('58 mm receipt', 54, 78),
              'thermal80': ('80 mm receipt', 72, 90), 'micro': ('Micro slip 38×21', 38, 21), 'strip': ('Code strip 64×11', 64, 11),
              'miniqr': ('Mini QR 30×38', 30, 38), 'l7160': ('Label L7160 63.5×38.1', 63.5, 38.1), 'pos': ('Shop barcode 70×40', 70, 40)}
PAPERS = {'A4': (210, 297), 'Letter': (216, 279), 'A5': (148, 210), 'thermal58': (58, 0), 'thermal80': (80, 0)}
VOUCHER_TOKENS = [('business', 'Business name'), ('plan', 'Plan'), ('price', 'Price'), ('currency', 'Currency'), ('duration', 'Duration'),
                  ('devices', 'Devices'), ('speed', 'Speed'), ('data', 'Data cap'), ('code', 'Voucher code'), ('serial', 'Serial no.'),
                  ('ssid', 'Wi-Fi name'), ('login_url', 'Login address'), ('phone', 'Help phone'), ('batch', 'Batch'), ('created', 'Printed date')]


def voucher_template(key):
    t = VOUCHER_TEMPLATES.get(key) or VOUCHER_TEMPLATES['classic']
    return copy.deepcopy(t['config'])


def voucher_gallery():
    return [{'key': k, 'label': v['label'], 'note': v['note'], 'config': copy.deepcopy(v['config'])} for k, v in VOUCHER_TEMPLATES.items()]
