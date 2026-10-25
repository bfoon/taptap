"""Easy login addresses — short names customers type on the Wi-Fi (taptap.wifi, buy.wifi, kairaba.wifi …).

How it works
------------
1. Every router gets a static DNS entry per address, pointing at its own hotspot address
   (/ip dns static … comment="TapTap login address"). Phones on the Wi-Fi ask the router for names
   before they are logged in (the hotspot redirects their DNS), so the name opens the hotspot.
2. The hotspot shows the TapTap login page, which knows the address the customer typed ($(link-orig)).
   If it is one of these addresses, the page forwards the phone to TapTap (/p/a/<key>/) with the
   router's login details, and TapTap sends it where the address points:
       online   the online voucher sign-in page (signs in through the router)
       store    the online voucher shop
       custom   another web page (its site is added to the walled garden)
       router   nothing to forward: the router's own login page simply opens
3. TapTap counts every open, so the owner sees which poster or sticker is used.

Names that do not work are refused or flagged before they reach a router:
  * a name ending in a number (taptap.1)  — browsers read it as a broken IP address and refuse it
  * .local                                 — iPhones, Macs and most Androids ask the local network
                                             (Bonjour), never the router
  * a real website (google.com)            — would hijack it for customers on the Wi-Fi
"""
from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

COMMENT = 'TapTap login address'
MAX_ADDRESSES = 12
LABEL_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')

# Endings that work well on every phone: not real internet endings, so nothing real is hijacked.
GOOD_ENDINGS = ('wifi', 'hotspot', 'login', 'zone', 'home')
# Real internet endings: only use a name you own (it hides the real site while customers are on your Wi-Fi).
REAL_ENDINGS = {
    'com', 'net', 'org', 'gm', 'io', 'app', 'info', 'co', 'edu', 'gov', 'biz', 'me', 'tv', 'uk', 'sn', 'ng', 'gh', 'sl',
    'africa', 'online', 'site', 'shop', 'store', 'xyz', 'dev', 'ai', 'us', 'fr', 'de',
}
BLOCKED = {'google.com', 'facebook.com', 'whatsapp.com', 'youtube.com', 'apple.com', 'microsoft.com', 'gstatic.com',
           'googleapis.com', 'instagram.com', 'tiktok.com', 'connectivitycheck.gstatic.com', 'captive.apple.com',
           'msftconnecttest.com', 'wave.com', 'qmoney.gm', 'afrimoney.gm'}


def _site_host():
    return urlsplit(getattr(settings, 'SITE_URL', '') or '').hostname or ''


def suggest(name, business=None):
    """Working alternatives for a name that does not work (taptap.1 → taptap1.wifi, taptap.one …)."""
    words = [w for w in re.split(r'[^a-z0-9]+', (name or '').lower()) if w]
    base = words[0] if words else ''
    if not base and business is not None:
        base = re.sub(r'[^a-z0-9]', '', business.business_name.lower())[:20]
    base = base or 'login'
    joined = ''.join(words)[:24] or base
    out = []
    for s in (f'{joined}.wifi', f'{base}.wifi', f'{base}.hotspot', f'{base}.login', f'go.{base}.wifi'):
        if s not in out and s != name and LABEL_RE.match(s.split('.')[0]):
            out.append(s)
    return out[:4]


def check_name(raw, business=None, exclude_pk=None):
    """Clean and judge a name. Returns {'name', 'ok', 'errors', 'warnings', 'devices', 'suggestions', 'typed'}.

    devices: which phones open it — {'android': bool, 'iphone': bool, 'laptop': bool}."""
    name = (raw or '').strip().lower()
    name = re.sub(r'^[a-z]+://', '', name).split('/')[0].split('?')[0].split(':')[0].strip('.')
    name = name[4:] if name.startswith('www.') and name.count('.') >= 2 else name
    errors, warnings = [], []
    devices = {'android': True, 'iphone': True, 'laptop': True}
    labels = name.split('.') if name else []
    if not name:
        errors.append('Type an address, for example taptap.wifi.')
    elif len(name) > 60:
        errors.append('Keep it under 60 characters — customers have to type it.')
    elif len(labels) < 2:
        errors.append(f'Add an ending with a dot, e.g. {name}.wifi — a single word is treated as a web search.')
    elif not all(LABEL_RE.match(lb) for lb in labels):
        errors.append('Use only letters, numbers and hyphens between the dots (no spaces, no underscores).')
    if not errors:
        last = labels[-1]
        if last.isdigit():
            errors.append(f'“{name}” ends in a number, so browsers read it as a broken IP address and refuse to open it.')
            devices = {'android': False, 'iphone': False, 'laptop': False}
        elif last == 'local':
            warnings.append('.local names are looked up on the local network (Bonjour), not at the router: they do not open on '
                            'iPhones, iPads and Macs, and fail on many Android phones. Fine only for Windows laptops.')
            devices = {'android': False, 'iphone': False, 'laptop': True}
        if name in BLOCKED or any(name.endswith('.' + b) for b in BLOCKED) or name == _site_host():
            errors.append(f'{name} is a real website phones need (or TapTap itself). Choose a name of your own.')
        elif last in REAL_ENDINGS and not errors:
            warnings.append(f'.{last} is a real internet ending. Only use a name you own: while customers are on your Wi-Fi '
                            f'it opens your page instead of the real {name}.')
        if business is not None and not errors:
            if name == (business.hotspot_dns_name or '').strip().lower():
                errors.append(f'{name} is already your main login address (Hotspot & Wi-Fi). Pick another one.')
            qs = business.login_addresses.filter(name=name)
            if exclude_pk:
                qs = qs.exclude(pk=exclude_pk)
            if qs.exists():
                errors.append(f'You already have {name}.')
        if not errors and labels[0] in ('http', 'https', 'www') and len(labels) == 2:
            warnings.append('Customers may get confused typing “www” or “http” as the name itself.')
    return {'name': name, 'ok': not errors, 'errors': errors, 'warnings': warnings, 'devices': devices,
            'suggestions': suggest(name, business) if (errors or warnings) else [], 'typed': f'{name}/' if name else ''}


# ───────────────────────────── where an address goes ─────────────────────────────
def destination_ready(addr):
    """('', None) when the destination can be used, else a short reason."""
    b = addr.business
    if addr.destination == 'online':
        from .portal_deploy import default_pages
        page = default_pages(b).get('login')
        if not page or not page.is_published or not page.slug:
            return 'Publish a login page in Portal Studio first.'
    elif addr.destination == 'store':
        try:
            store = b.online_storefront
        except Exception:
            store = None
        if not store or not store.enabled:
            return 'Switch on the online shop (Online sales) first.'
    elif addr.destination == 'custom':
        if not (addr.custom_url or '').startswith('https://'):
            return 'Enter an https:// address.'
    return ''


def target_url(addr, params):
    """Where /p/a/<key>/ sends the phone. `params` = router login details passed by the login page."""
    b = addr.business
    keep = {k: params.get(k) for k in ('link-login-only', 'link-orig', 'mac', 'ip', 'r', 'error') if params.get(k)}
    if addr.destination == 'custom' and addr.custom_url.startswith('https://'):
        return addr.custom_url
    if addr.destination == 'store':
        try:
            store = b.online_storefront
            if store.enabled:
                return reverse('online_buy_store', args=[store.public_slug])
        except Exception:
            pass
    if addr.destination in ('online', 'store'):
        from .portal_deploy import default_pages
        page = default_pages(b).get('login')
        if page and page.is_published and page.slug:
            return reverse('portal_online', args=[page.slug]) + ('?' + urlencode(keep) if keep else '')
    lo = keep.get('link-login-only', '')
    return lo if lo.startswith(('http://', 'https://')) else ''


def alias_map(business, base):
    """For the router login page: {typed name: TapTap forward URL} — addresses that forward somewhere."""
    if not base:
        return {}
    out = {}
    for a in business.login_addresses.filter(active=True).exclude(destination='router'):
        if not destination_ready(a):
            out[a.name] = f'{base}/p/a/{a.key}/'
    return out


def forward_snippet(business, base, router_id=None):
    """HTML/JS put at the top of the router login page: forwards a phone that typed one of the addresses,
    before the rest of the page loads. Router variables go through the DOM (they may contain quotes)."""
    m = alias_map(business, base)
    if not m:
        return ''
    import json
    data = json.dumps(m).replace('</', '<\\/')
    rid = f'<input type="hidden" name="r" value="{int(router_id)}">' if router_id else ''
    return ('<form id="tp-alias" method="get" hidden><input type="hidden" name="link-login-only" value="$(link-login-only)">'
            '<input type="hidden" name="link-orig" value="$(link-orig)"><input type="hidden" name="mac" value="$(mac)">'
            f'<input type="hidden" name="ip" value="$(ip)">{rid}</form>'
            '<script>(function(){try{var A=' + data + ',f=document.getElementById("tp-alias"),'
            'o=(f.elements["link-orig"].value||"").replace(/^\\s+|\\s+$/g,""),'
            'm=o.match(/^https?:\\/\\/([^\\/:?#]+)/i);if(!m)return;var h=m[1].toLowerCase().replace(/^www\\./,""),u=A[h];'
            'if(!u)return;document.documentElement.style.visibility="hidden";f.action=u;f.submit();}catch(e){}})();</script>')


# ───────────────────────────── routers ─────────────────────────────
def routeros_script(business):
    """Replace this business's login addresses on a router: one static DNS entry per active address,
    pointing at the router's hotspot address; walled garden for "another web page" sites."""
    from .agent import rs
    c = rs(COMMENT)
    names = [a.name for a in business.login_addresses.filter(active=True) if check_name(a.name)['ok']]
    hosts = sorted({urlsplit(a.custom_url).hostname for a in business.login_addresses.filter(active=True, destination='custom')
                    if a.custom_url.startswith('https://') and urlsplit(a.custom_url).hostname})
    lines = [':local hip ""',
             ':foreach s in=[/ip hotspot find where disabled=no] do={ :local i [/ip hotspot get $s interface]; '
             ':foreach a in=[/ip address find where interface=$i disabled=no] do={ :if ([:len $hip] = 0) do={ '
             ':local ad [/ip address get $a address]; :set hip [:pick $ad 0 [:find $ad "/"]] } } }',
             f':do {{ /ip dns static remove [find where comment={c}] }} on-error={{}}',
             f':do {{ /ip hotspot walled-garden remove [find where comment={c}] }} on-error={{}}',
             f':do {{ /ip hotspot walled-garden ip remove [find where comment={c}] }} on-error={{}}']
    if names:
        lines.append(':if ([:len $hip] = 0) do={ :error "TapTap: no hotspot address found on this router" }')
        for n in names:
            lines.append(f':do {{ /ip dns static add name={rs(n)} address=$hip ttl=5m comment={c} }} '
                         f'on-error={{ :log warning ("TapTap: could not add login address " . {rs(n)}) }}')
    for h in hosts:
        lines.append(f':do {{ /ip hotspot walled-garden add dst-host={rs(h)} comment={c} }} on-error={{}}')
        lines.append(f':do {{ /ip hotspot walled-garden ip add dst-host={rs(h)} action=accept comment={c} }} on-error={{}}')
    lines.append(f':log info ("TapTap login addresses: {len(names)} pointing at " . $hip)')
    return '; '.join(lines)


def link_body(cmd, url, check, nonce_value):
    """TapTap Link command 'login_addresses' — built from the current list when the router picks it up."""
    return '{ ' + routeros_script(cmd.router.business) + ' }'


def push(business, user=None, routers=None):
    """Send the addresses to every router. Returns [{'router', 'ok', 'text'}]."""
    from .portal_deploy import _run_script
    from .voucher_history import channel
    results = []
    script = routeros_script(business)
    for r in (routers if routers is not None else business.routers.order_by('name')):
        try:
            if channel(r) == 'TapTap Link':
                from .linkops import send
                send(r, 'login_addresses', {}, label='Update login addresses', user=user, minutes=60 * 24)
                results.append({'router': r.name, 'ok': True, 'text': 'sent — applied at the next check-in'})
            else:
                _run_script(r, script)
                results.append({'router': r.name, 'ok': True, 'text': 'updated'})
        except Exception as exc:  # noqa: BLE001
            results.append({'router': r.name, 'ok': False, 'text': str(exc)[:160]})
    return results


def _after_change(business, request):
    """Login page knows the addresses (forwarding), routers know the names (DNS)."""
    from .portal_deploy import schedule_redeploy
    schedule_redeploy(business)
    if business.routers.exists():
        res = push(business, request.user)
        bad = [x for x in res if not x['ok']]
        if bad:
            messages.warning(request, 'Saved. Some routers were not updated: ' +
                             '; '.join(f'{x["router"]}: {x["text"]}' for x in bad[:3]) + ' — press “Send to routers” to retry.')
            return
        messages.info(request, 'Routers updated: ' + ', '.join(f'{x["router"]} ({x["text"]})' for x in res[:4]) +
                      ('…' if len(res) > 4 else ''))


# ───────────────────────────── public forward ─────────────────────────────
def go(request, key):
    """/p/a/<key>/ — a phone typed an easy address; count it and send it on."""
    from .models import LoginAddress
    addr = LoginAddress.objects.select_related('business').filter(key=key, active=True).first()
    if not addr:
        raise Http404
    LoginAddress.objects.filter(pk=addr.pk).update(opens=F('opens') + 1, last_opened_at=timezone.now())
    url = target_url(addr, request.GET)
    if not url:
        return render(request, 'core/login_address_help.html', {'addr': addr, 'business': addr.business}, status=200)
    return HttpResponseRedirect(url)


# ───────────────────────────── settings ─────────────────────────────
def settings_ctx(business):
    rows = []
    for a in business.login_addresses.all():
        verdict = check_name(a.name, business, exclude_pk=a.pk)
        rows.append({'a': a, 'problem': destination_ready(a) if a.destination != 'router' else '',
                     'devices': verdict['devices'], 'warnings': verdict['warnings'] + verdict['errors']})
    base_name = re.sub(r'[^a-z0-9]', '', business.business_name.lower())[:18] or 'mywifi'
    taken = {r['a'].name for r in rows} | {(business.hotspot_dns_name or '').lower()}
    ideas = [n for n in (f'{base_name}.wifi', 'buy.wifi', 'pay.wifi', 'voucher.wifi', f'{base_name}.hotspot', 'go.wifi') if n not in taken][:5]
    try:
        store_on = bool(business.online_storefront.enabled)
    except Exception:
        store_on = False
    from .portal_deploy import default_pages
    page = default_pages(business).get('login')
    return {'addr_rows': rows, 'addr_ideas': ideas, 'addr_max': MAX_ADDRESSES, 'addr_destinations': _destinations(),
            'addr_store_on': store_on, 'addr_portal_ok': bool(page and page.is_published),
            'addr_routers': business.routers.count()}


def _destinations():
    from .models import LoginAddress
    help_ = {'online': 'TapTap’s online sign-in: the customer types the voucher, TapTap checks it and signs the phone in.',
             'router': 'The login page on the router itself — works even when your Internet is down.',
             'store': 'Your online shop: buy a voucher with mobile money and sign in.',
             'custom': 'Any https page — your menu, a promotion, a form. Its site opens before login.'}
    icons = {'online': 'bi-cloud-check', 'router': 'bi-router', 'store': 'bi-bag-check', 'custom': 'bi-link-45deg'}
    return [{'key': k, 'label': l, 'help': help_[k], 'icon': icons[k]} for k, l in LoginAddress.DESTINATIONS]


def _clean_form(request, business, instance=None):
    from .models import LoginAddress
    p = request.POST
    verdict = check_name(p.get('name'), business, exclude_pk=instance.pk if instance else None)
    dest = p.get('destination') if p.get('destination') in dict(LoginAddress.DESTINATIONS) else 'online'
    url = (p.get('custom_url') or '').strip()
    errors = list(verdict['errors'])
    if dest == 'custom':
        if not url.startswith('https://') or not urlsplit(url).hostname:
            errors.append('“Another web page” needs a full https:// address.')
    return verdict, dest, url[:300], (p.get('note') or '').strip()[:80], errors


@login_required
@require_POST
def add(request):
    from .models import LoginAddress
    business = request.user.business
    if business.login_addresses.count() >= MAX_ADDRESSES:
        messages.error(request, f'You can have up to {MAX_ADDRESSES} login addresses. Remove one first.')
        return redirect(reverse('settings') + '#addresses')
    verdict, dest, url, note, errors = _clean_form(request, business)
    if errors:
        messages.error(request, ' '.join(errors) + (f' Try: {", ".join(verdict["suggestions"])}.' if verdict['suggestions'] else ''))
        return redirect(reverse('settings') + '#addresses')
    a = LoginAddress.objects.create(business=business, name=verdict['name'], destination=dest, custom_url=url if dest == 'custom' else '', note=note)
    from .utils import log
    log(business, 'Login address added', f'{a.name} → {a.get_destination_display()}')
    msg = f'{a.name} added — customers type {a.name}/ on your Wi-Fi.'
    if verdict['warnings']:
        messages.warning(request, msg + ' Note: ' + verdict['warnings'][0])
    else:
        messages.success(request, msg)
    problem = destination_ready(a) if dest != 'router' else ''
    if problem:
        messages.warning(request, f'{a.name} opens the router login page until this is fixed: {problem}')
    _after_change(business, request)
    return redirect(reverse('settings') + '#addresses')


@login_required
@require_POST
def action(request, pk):
    business = request.user.business
    a = get_object_or_404(business.login_addresses, pk=pk)
    act = request.POST.get('action')
    from .utils import log
    if act == 'delete':
        name = a.name
        a.delete()
        log(business, 'Login address removed', name)
        messages.success(request, f'{name} removed. Routers forget it on the next update.')
    elif act == 'toggle':
        a.active = not a.active
        a.save(update_fields=['active'])
        messages.success(request, f'{a.name} {"switched on" if a.active else "paused"}.')
    elif act == 'edit':
        verdict, dest, url, note, errors = _clean_form(request, business, instance=a)
        if errors:
            messages.error(request, ' '.join(errors))
            return redirect(reverse('settings') + '#addresses')
        old = a.name
        a.name, a.destination, a.custom_url, a.note = verdict['name'], dest, url if dest == 'custom' else '', note
        a.save()
        log(business, 'Login address changed', f'{old} → {a.name} ({a.get_destination_display()})')
        messages.success(request, f'{a.name} saved.')
    elif act == 'reset_count':
        a.opens, a.last_opened_at = 0, None
        a.save(update_fields=['opens', 'last_opened_at'])
        messages.success(request, f'Open count for {a.name} reset.')
        return redirect(reverse('settings') + '#addresses')
    else:
        return redirect(reverse('settings') + '#addresses')
    _after_change(business, request)
    return redirect(reverse('settings') + '#addresses')


@login_required
def check(request):
    """Live check while typing: ?name=…"""
    return JsonResponse(check_name(request.GET.get('name'), request.user.business, exclude_pk=request.GET.get('pk') or None))


@login_required
@require_POST
def send_to_routers(request):
    business = request.user.business
    res = push(business, request.user)
    from .portal_deploy import schedule_redeploy
    schedule_redeploy(business)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': all(r['ok'] for r in res), 'results': res})
    if not res:
        messages.info(request, 'Add a router first.')
    for r in res:
        (messages.success if r['ok'] else messages.error)(request, f'{r["router"]}: {r["text"]}')
    return redirect(reverse('settings') + '#addresses')


@login_required
def sign(request, pk):
    """Printable A5 sign: “Open taptap.wifi on our Wi-Fi” with a QR code."""
    business = request.user.business
    a = get_object_or_404(business.login_addresses, pk=pk)
    return render(request, 'core/login_address_sign.html', {'a': a, 'business': business, 'url': f'http://{a.name}/'})


# ───────────────────────────── install ─────────────────────────────
def install():
    from django.urls import path
    from . import agent, permissions, urls
    agent.SAFE_KINDS.add('login_addresses')
    for name in ('login_address_add', 'login_address_action', 'login_address_check', 'login_address_push', 'login_address_sign'):
        permissions.URL_PERMS.setdefault(name, 'settings.manage')
    names = {getattr(p, 'name', None) for p in urls.urlpatterns}
    if 'login_address_add' in names:
        return
    urls.urlpatterns[0:0] = [
        path('p/a/<str:key>/', go, name='login_address_go'),
        path('settings/addresses/add/', add, name='login_address_add'),
        path('settings/addresses/check/', check, name='login_address_check'),
        path('settings/addresses/send/', send_to_routers, name='login_address_push'),
        path('settings/addresses/<int:pk>/', action, name='login_address_action'),
        path('settings/addresses/<int:pk>/sign/', sign, name='login_address_sign'),
    ]
