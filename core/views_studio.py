"""Portal Studio (customer-facing hotspot pages) and Voucher Design Studio."""
import io
import json
import secrets
import zipfile
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse, JsonResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import PortalPage, VoucherDesign, Voucher, VoucherBatch
from .studio_presets import (
    portal_template, portal_gallery, PORTAL_TEMPLATES, FONTS, voucher_template, voucher_gallery,
    VOUCHER_TEMPLATES, CARD_SIZES, PAPERS, VOUCHER_TOKENS,
)
from .utils import log, portal_code_length
from .ads import ads_for, record_device

MAX_CONFIG_BYTES = 1_500_000
STATIC_DIR = Path(settings.BASE_DIR) / 'static'


def _b(request): return request.user.business


def duration_text(minutes):
    """Human duration from minutes: '30 minutes', '12 hours', '3 days', '1 month'."""
    from .durations import text
    return text(minutes)


def _money(v):
    v = float(v or 0)
    return f'{v:,.0f}' if v == int(v) else f'{v:,.2f}'


def business_ctx(business):
    return {'name': business.business_name, 'logo': business.logo_data or '', 'phone': business.support_phone or business.phone,
            'ssid': business.wifi_ssid or business.business_name, 'currency': business.currency or 'D',
            'login_url': business.hotspot_url or '', 'brand': business.brand_color or '#1769e0'}


def plans_ctx(business):
    # 'minutes' is exact; 'hours' is kept for portal designs saved before minute plans existed.
    return [{'name': p.name, 'price': float(p.price), 'minutes': p.duration_minutes, 'hours': p.duration_hours,
             'duration': p.duration_text, 'devices': p.max_devices, 'speed': p.speed_limit}
            for p in business.plans.filter(active=True).order_by('price', 'duration_minutes')]


def _safe_json(obj):
    """JSON safe to inline in <script> and inside a MikroTik template ($( would be treated as a router variable)."""
    return (json.dumps(obj, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('&', '\\u0026').replace('$(', '$\\u0028').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def _unique_slug(base):
    base = slugify(base)[:50] or 'wifi'
    for _ in range(20):
        s = f'{base}-{secrets.token_hex(2)}'
        if not PortalPage.objects.filter(slug=s).exists(): return s
    return f'{base}-{secrets.token_hex(6)}'


def _load_json_body(request):
    if len(request.body or b'') > MAX_CONFIG_BYTES:
        raise ValueError('This design is too large. Use smaller images (under ~300 KB each).')
    return json.loads(request.body or '{}')


# ═════════════════════════════ Portal Studio ═════════════════════════════
@login_required
def portal_studio(request):
    business = _b(request)
    if request.method == 'POST':
        kind = request.POST.get('kind', 'login'); kind = kind if kind in dict(PortalPage.KINDS) else 'login'
        key = request.POST.get('template') or ''
        cfg = portal_template(key, kind)
        tpl = PORTAL_TEMPLATES.get(key, {})
        name = request.POST.get('name', '').strip() or f'{tpl.get("label", "New")} {dict(PortalPage.KINDS)[kind].lower()}'
        first = not business.portal_pages.filter(kind=kind).exists()
        page = PortalPage.objects.create(business=business, name=name[:120], slug=_unique_slug(f'{business.business_name}-{kind}'),
                                         kind=kind, template_key=key, config=cfg, is_default=first, is_published=first)
        log(business, 'Portal Page Created', page.name)
        return redirect('portal_editor', pk=page.pk)
    pages = list(business.portal_pages.all())
    for p in pages: p.config_json = _safe_json(p.config)
    return render(request, 'core/studio/portal_list.html', {
        'pages': pages, 'gallery': portal_gallery(), 'business_ctx': business_ctx(business), 'plans_ctx': plans_ctx(business),
        'kinds': PortalPage.KINDS,
    })


@login_required
@xframe_options_sameorigin
def portal_editor(request, pk):
    business = _b(request); page = get_object_or_404(business.portal_pages, pk=pk)
    if request.method == 'POST':
        try:
            data = _load_json_body(request)
        except ValueError as e:
            return JsonResponse({'success': False, 'message': str(e)}, status=400)
        with transaction.atomic():
            if isinstance(data.get('config'), dict):
                page.config = data['config']
            if data.get('name'): page.name = str(data['name'])[:120]
            if 'is_published' in data: page.is_published = bool(data['is_published'])
            if data.get('is_default'):
                business.portal_pages.filter(kind=page.kind).exclude(pk=page.pk).update(is_default=False)
                page.is_default = True; page.is_published = True
            page.save()
        if page.is_default and page.is_published:
            from .portal_deploy import schedule_redeploy
            schedule_redeploy(business)   # keep the copy on the routers up to date
        return JsonResponse({'success': True, 'updated_at': timezone.localtime(page.updated_at).strftime('%H:%M:%S'),
                             'is_published': page.is_published, 'is_default': page.is_default})
    public_url = request.build_absolute_uri(f'/p/{page.slug}/')
    return render(request, 'core/studio/portal_editor.html', {
        'page': page, 'config': page.config or portal_template(page.template_key, page.kind),
        'gallery': [g for g in portal_gallery() if g['kind'] == page.kind],
        'business_ctx': business_ctx(business), 'plans_ctx': plans_ctx(business), 'fonts': {k: v[0] for k, v in FONTS.items()},
        'public_url': public_url, 'renderer_url': '/static/studio/portal-render.js',
        'ads_ctx': {k: ads_for(business, k) for k in ('login', 'redirect', 'status')},
    })


@login_required
@require_POST
def _publish_to_routers(request, business):
    """Publishing the default pages puts them on every router (served before customers have Internet)."""
    from .portal_deploy import deploy_business, site_base
    if not business.routers.exists():
        return
    results = deploy_business(business, base=site_base(request), user=request.user)
    good = [m for ok, m in results if ok]
    bad = [m for ok, m in results if not ok]
    if good:
        messages.success(request, 'On your routers: ' + ' '.join(good))
    if bad:
        messages.warning(request, 'Not installed: ' + ' '.join(bad))


def portal_action(request, pk):
    business = _b(request); page = get_object_or_404(business.portal_pages, pk=pk); action = request.POST.get('action')
    if action == 'delete':
        name = page.name; kind = page.kind; was_default = page.is_default; page.delete()
        if was_default:
            nxt = business.portal_pages.filter(kind=kind).first()
            if nxt: nxt.is_default = True; nxt.save(update_fields=['is_default'])
        messages.success(request, f'{name} deleted.')
    elif action == 'duplicate':
        copy_ = PortalPage.objects.create(business=business, name=f'{page.name} (copy)'[:120], slug=_unique_slug(f'{business.business_name}-{page.kind}'),
                                          kind=page.kind, template_key=page.template_key, config=page.config)
        messages.success(request, 'Page duplicated.'); return redirect('portal_editor', pk=copy_.pk)
    elif action == 'default':
        business.portal_pages.filter(kind=page.kind).update(is_default=False)
        page.is_default = True; page.is_published = True; page.save(update_fields=['is_default', 'is_published'])
        messages.success(request, f'{page.name} is now your default {page.get_kind_display().lower()}.')
        _publish_to_routers(request, business)
    elif action == 'toggle':
        page.is_published = not page.is_published; page.save(update_fields=['is_published'])
        if page.is_published and page.is_default: _publish_to_routers(request, business)
        messages.success(request, f'{page.name} {"published" if page.is_published else "unpublished"}.')
    return redirect('portal_studio')


def _public_ctx(request, page, mode):
    business = page.business
    q = request.GET
    mt = {'linkLoginOnly': q.get('link-login-only', ''), 'linkOrig': q.get('link-orig', ''), 'error': q.get('error', '')[:200],
          'mac': q.get('mac', ''), 'ip': q.get('ip', ''), 'linkLogout': q.get('link-logout', ''), 'linkRedirect': q.get('dst', '') or q.get('link-redirect', ''),
          'username': q.get('username', ''), 'uptime': q.get('uptime', ''), 'timeLeft': q.get('time-left', '')}
    # Only allow router login targets that look like a hotspot login URL — never an arbitrary POST target.
    if mt['linkLoginOnly'] and not mt['linkLoginOnly'].startswith(('http://', 'https://')):
        mt['linkLoginOnly'] = ''
    base = request.build_absolute_uri('/').rstrip('/')
    return {'mode': mode, 'kind': page.kind, 'business': business_ctx(business), 'plans': plans_ctx(business),
            'mt': mt, 'checkUrl': f'/p/{page.slug}/check/', 'deviceUrl': f'/p/device/{page.slug}/',
            'ads': {page.kind: ads_for(business, page.kind, base)}}


def portal_public(request, slug):
    page = PortalPage.objects.select_related('business').filter(slug=slug).first()
    if not page: raise Http404
    owner_preview = request.user.is_authenticated and getattr(request.user, 'business', None) == page.business
    if not page.is_published and not owner_preview: raise Http404
    if not owner_preview:
        PortalPage.objects.filter(pk=page.pk).update(views=page.views + 1)
    ctx = _public_ctx(request, page, 'hosted')
    return render(request, 'core/studio/portal_public.html', {'page': page, 'config_json': _safe_json(page.config), 'ctx_json': _safe_json(ctx),
                                                              'draft': not page.is_published})


@csrf_exempt
@require_POST
def portal_check(request, slug):
    page = PortalPage.objects.select_related('business').filter(slug=slug).first()
    if not page: return JsonResponse({'success': False, 'message': 'This page no longer exists.'}, status=404)
    ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    key = f'portal-check:{page.pk}:{ip}'
    try:
        hits = cache.get(key, 0)
        if hits >= 20:
            return JsonResponse({'success': False, 'message': 'Too many tries. Wait a minute and try again.'}, status=429)
        cache.set(key, hits + 1, 60)
    except Exception:
        pass
    try: data = json.loads(request.body or '{}')
    except Exception: data = request.POST
    code = str(data.get('code') or '').replace(' ', '').strip()
    if not code: return JsonResponse({'success': False, 'message': 'Type the code printed on your voucher.'}, status=400)
    v = page.business.vouchers.filter(code__iexact=code).first()
    if not v: return JsonResponse({'success': False, 'message': 'That code was not recognised. Check the letters and try again.'}, status=404)
    if v.status != 'active': return JsonResponse({'success': False, 'message': 'This voucher has been disabled. Ask staff for help.'}, status=403)
    if v.expires_at and v.expires_at <= timezone.now(): return JsonResponse({'success': False, 'message': 'This voucher has expired.'}, status=403)
    PortalPage.objects.filter(pk=page.pk).update(connects=page.connects + 1)
    if data.get('fp'):
        try:
            record_device(page.business, data.get('fp'), data.get('c') or {}, request.META.get('HTTP_USER_AGENT', ''),
                          mac=data.get('mac', ''), ip=data.get('ip') or ip, code=v.code, portal=page)
        except Exception:
            pass  # identification must never block a customer from logging in
    return JsonResponse({'success': True, 'code': v.code, 'plan': v.plan_name, 'duration': duration_text(v.duration_minutes), 'devices': v.max_devices})


# ─────────────────────── MikroTik export ───────────────────────
MT_VARS = {
    'login': "{linkLoginOnly:'$(link-login-only)',chapId:'$(chap-id)',chapChallenge:'$(chap-challenge)',mac:'$(mac)',ip:'$(ip)'}",
    'redirect': "{linkRedirect:'$(link-redirect)',linkLogout:'$(link-logout)',linkStatus:'$(link-status)',username:'$(username)',uptime:'$(uptime)',timeLeft:'$(session-time-left)',ip:'$(ip)',mac:'$(mac)',bytesIn:'$(bytes-in-nice)',bytesOut:'$(bytes-out-nice)'}",
}
MT_VARS['status'] = MT_VARS['redirect']


def _export_html(page, base=''):
    renderer = (STATIC_DIR / 'studio' / 'portal-render.js').read_text(encoding='utf-8')
    business = page.business
    ctx = {'mode': 'mikrotik', 'kind': page.kind, 'business': business_ctx(business), 'plans': plans_ctx(business),
           'ads': {page.kind: ads_for(business, page.kind, base)}, 'deviceUrl': f'{base}/p/device/{page.slug}/' if base else ''}
    # Values that may contain quotes go through the DOM, not a JS string literal.
    hidden = '<div id="tp-err" hidden>$(error)</div><div id="tp-orig" hidden>$(link-orig)</div>' if page.kind == 'login' else ''
    refresh = '<meta http-equiv="refresh" content="60">' if page.kind == 'status' else ''
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="pragma" content="no-cache"><meta http-equiv="expires" content="-1">{refresh}
<title>{business.business_name} Wi-Fi</title></head>
<body><div id="tp"></div>{hidden}
<!-- Generated by TapTap Portal Studio · page "{page.name}" · {timezone.localtime():%Y-%m-%d %H:%M} -->
<script>{renderer}</script>
<script>
var cfg={_safe_json(page.config)};
var ctx={_safe_json(ctx)};
ctx.mt={MT_VARS[page.kind]};
var e=document.getElementById('tp-err'),o=document.getElementById('tp-orig');
if(e)ctx.mt.error=e.textContent.replace(/^\\s+|\\s+$/g,'');if(o)ctx.mt.linkOrig=o.textContent.replace(/^\\s+|\\s+$/g,'');
TapPortal.render(document.getElementById('tp'),cfg,ctx);
</script></body></html>'''


def _redirect_stub(public_url):
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="pragma" content="no-cache"><meta http-equiv="expires" content="-1"><title>Wi-Fi login</title>
<style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;color:#334}}</style></head>
<body><form name="go" action="{public_url}" method="get">
<input type="hidden" name="link-login-only" value="$(link-login-only)">
<input type="hidden" name="link-orig" value="$(link-orig)">
<input type="hidden" name="mac" value="$(mac)"><input type="hidden" name="ip" value="$(ip)">
<input type="hidden" name="error" value="$(error)">
<noscript><button type="submit">Continue to the login page</button></noscript>
</form><p>Opening the login page…</p>
<script>document.go.submit();</script></body></html>'''


def _alogin_stub(public_url):
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="pragma" content="no-cache"><title>Connected</title></head>
<body><form name="go" action="{public_url}" method="get">
<input type="hidden" name="dst" value="$(link-redirect)"><input type="hidden" name="link-logout" value="$(link-logout)">
<input type="hidden" name="username" value="$(username)"><input type="hidden" name="uptime" value="$(uptime)">
<input type="hidden" name="time-left" value="$(session-time-left)"><input type="hidden" name="ip" value="$(ip)"><input type="hidden" name="mac" value="$(mac)">
</form><script>document.go.submit();</script><noscript><a href="$(link-redirect)">Continue</a></noscript></body></html>'''


@login_required
def portal_export(request, pk):
    business = _b(request); page = get_object_or_404(business.portal_pages, pk=pk)
    mode = request.GET.get('mode', 'offline')
    host = request.get_host().split(':')[0]
    base = request.build_absolute_uri('/').rstrip('/')
    buf = io.BytesIO()
    redirect_page = business.portal_pages.filter(kind='redirect', is_default=True).first() or business.portal_pages.filter(kind='redirect').first()
    status_page = business.portal_pages.filter(kind='status', is_default=True).first()
    fname = {'login': 'login.html', 'redirect': 'alogin.html', 'status': 'status.html'}[page.kind]
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        if mode == 'hosted':
            url = request.build_absolute_uri(f'/p/{page.slug}/')
            if page.kind == 'login':
                z.writestr('login.html', _redirect_stub(url))
                if redirect_page: z.writestr('alogin.html', _alogin_stub(request.build_absolute_uri(f'/p/{redirect_page.slug}/')))
            else:
                z.writestr(fname, _alogin_stub(url))
            z.writestr('taptap-walled-garden.rsc',
                       f'# Let customers reach the TapTap-hosted portal before they log in\n'
                       f'/ip hotspot walled-garden add dst-host={host} comment="TapTap portal"\n'
                       f'/ip hotspot walled-garden add dst-host=fonts.googleapis.com comment="TapTap portal fonts"\n'
                       f'/ip hotspot walled-garden add dst-host=fonts.gstatic.com comment="TapTap portal fonts"\n'
                       f'# The hosted page logs in with a plain password, so allow PAP alongside CHAP:\n'
                       f'# /ip hotspot profile set [find] login-by=http-chap,http-pap\n')
        else:
            z.writestr(fname, _export_html(page, base))
            if page.kind == 'login' and redirect_page: z.writestr('alogin.html', _export_html(redirect_page, base))
            if page.kind == 'login' and status_page: z.writestr('status.html', _export_html(status_page, base))
            z.writestr('taptap-fonts-walled-garden.rsc',
                       '# Optional: lets the custom fonts load before login. Without it the page uses system fonts.\n'
                       f'# The TapTap line lets device identification and advert statistics reach TapTap before login.\n'
                       f'/ip hotspot walled-garden add dst-host={host} comment="TapTap device id and ads"\n'
                       '/ip hotspot walled-garden add dst-host=fonts.googleapis.com comment="TapTap portal fonts"\n'
                       '/ip hotspot walled-garden add dst-host=fonts.gstatic.com comment="TapTap portal fonts"\n')
        z.writestr('README.txt', f'''TapTap Portal Studio export — "{page.name}" ({business.business_name})
Exported {timezone.localtime():%d %b %Y %H:%M}

{"HOSTED MODE: the router shows a tiny login.html that forwards customers to" if mode == "hosted" else "OFFLINE MODE: the full page runs from the router itself."}
{"  " + request.build_absolute_uri(f"/p/{page.slug}/") if mode == "hosted" else "Plans and prices are baked in — export again after you change them."}

How to install
1. WinBox → Files. Open the hotspot folder (usually "hotspot" or "flash/hotspot").
2. Back up the existing {fname}{" and alogin.html" if page.kind == "login" else ""}.
3. Drag the .html files from this zip into that folder, replacing the old ones.
4. {"Import taptap-walled-garden.rsc (Terminal: /import taptap-walled-garden.rsc) so phones can reach TapTap before logging in, and allow HTTP PAP on the hotspot profile." if mode == "hosted" else "Optionally import taptap-fonts-walled-garden.rsc for the custom fonts."}
5. Connect a phone to the hotspot and open any http:// page to test.

Undo: put your backed-up files back.
''')
    resp = HttpResponse(buf.getvalue(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="taptap-{slugify(page.name) or "portal"}-{mode}.zip"'
    log(business, 'Portal Exported', f'{page.name} ({mode})')
    return resp


# ═════════════════════════════ Voucher Design Studio ═════════════════════════════
@login_required
def voucher_designs(request):
    business = _b(request)
    if request.method == 'POST':
        key = request.POST.get('template', 'classic')
        name = request.POST.get('name', '').strip() or VOUCHER_TEMPLATES.get(key, {}).get('label', 'My design')
        first = not business.voucher_designs.exists()
        dsg = VoucherDesign.objects.create(business=business, name=name[:120], template_key=key, config=voucher_template(key), is_default=first)
        return redirect('voucher_design_editor', pk=dsg.pk)
    designs = list(business.voucher_designs.all())
    for x in designs: x.config_json = _safe_json(x.config)
    return render(request, 'core/studio/voucher_list.html', {
        'designs': designs, 'gallery': voucher_gallery(), 'business_ctx': business_ctx(business),
    })


@login_required
def voucher_design_editor(request, pk):
    business = _b(request); dsg = get_object_or_404(business.voucher_designs, pk=pk)
    if request.method == 'POST':
        try: data = _load_json_body(request)
        except ValueError as e: return JsonResponse({'success': False, 'message': str(e)}, status=400)
        if isinstance(data.get('config'), dict): dsg.config = data['config']
        if data.get('name'): dsg.name = str(data['name'])[:120]
        if data.get('is_default'):
            business.voucher_designs.exclude(pk=dsg.pk).update(is_default=False); dsg.is_default = True
        dsg.save()
        return JsonResponse({'success': True, 'updated_at': timezone.localtime(dsg.updated_at).strftime('%H:%M:%S'), 'is_default': dsg.is_default})
    plans = [{'name': p.name, 'price': _money(p.price), 'duration': duration_text(p.duration_minutes),
              'devices': f'{p.max_devices} device{"s" if p.max_devices != 1 else ""}', 'speed': p.speed_limit or 'Full speed',
              'data': f'{p.data_limit_mb} MB' if p.data_limit_mb else 'Unlimited'} for p in business.plans.filter(active=True).order_by('price')]
    return render(request, 'core/studio/voucher_editor.html', {
        'design': dsg, 'config': dsg.config or voucher_template(dsg.template_key), 'gallery': voucher_gallery(),
        'business_ctx': business_ctx(business), 'plans': plans, 'fonts': {k: v[0] for k, v in FONTS.items()},
        'sizes': {k: list(v) for k, v in CARD_SIZES.items()}, 'papers': {k: list(v) for k, v in PAPERS.items()}, 'tokens': VOUCHER_TOKENS,
        'ads': ads_for(business, 'voucher'),
    })


@login_required
@require_POST
def voucher_design_action(request, pk):
    business = _b(request); dsg = get_object_or_404(business.voucher_designs, pk=pk); action = request.POST.get('action')
    if action == 'delete':
        was = dsg.is_default; dsg.delete()
        if was:
            nxt = business.voucher_designs.first()
            if nxt: nxt.is_default = True; nxt.save(update_fields=['is_default'])
        messages.success(request, 'Design deleted.')
    elif action == 'duplicate':
        c = VoucherDesign.objects.create(business=business, name=f'{dsg.name} (copy)'[:120], template_key=dsg.template_key, config=dsg.config)
        return redirect('voucher_design_editor', pk=c.pk)
    elif action == 'default':
        business.voucher_designs.update(is_default=False); dsg.is_default = True; dsg.save(update_fields=['is_default'])
        messages.success(request, f'{dsg.name} is now the default print design.')
    return redirect('voucher_designs')


def voucher_print_rows(business, qs):
    plans = {p.name: p for p in business.plans.all()}
    rows = []
    for v in qs.select_related('batch'):
        p = plans.get(v.plan_name)
        rows.append({'code': v.code, 'plan': v.plan_name, 'price': _money(v.price or (p.price if p else 0)), 'duration': duration_text(v.duration_minutes),
                     'devices': f'{v.max_devices} device{"s" if v.max_devices != 1 else ""}', 'speed': (p.speed_limit if p and p.speed_limit else 'Full speed'),
                     'data': (f'{p.data_limit_mb} MB' if p and p.data_limit_mb else 'Unlimited'), 'serial': f'{v.pk:06d}',
                     'batch': v.batch.name if v.batch else '', 'created': timezone.localtime(v.created_at).strftime('%d %b %Y')})
    return rows


@login_required
def voucher_print(request):
    business = _b(request)
    dsg = business.voucher_designs.filter(pk=request.GET.get('design') or 0).first() or business.voucher_designs.filter(is_default=True).first()
    config = dsg.config if dsg else voucher_template('classic')
    qs = business.vouchers.all().order_by('created_at')
    title = 'Vouchers'
    if request.GET.get('batch'):
        batch = get_object_or_404(business.batches, pk=request.GET['batch']); qs = qs.filter(batch=batch); title = batch.name
    elif request.GET.get('ids'):
        ids = [int(x) for x in request.GET['ids'].split(',') if x.strip().isdigit()][:1000]; qs = qs.filter(pk__in=ids); title = f'{len(ids)} selected vouchers'
    elif request.GET.get('agent'):
        ag = get_object_or_404(business.agents, pk=request.GET['agent'])
        qs = qs.filter(agent=ag, status='active', sold_at__isnull=True, used_at__isnull=True); title = f'Unsold vouchers held by {ag.name}'
    elif request.GET.get('plan'):
        qs = qs.filter(plan_name=request.GET['plan'], status='active', sold_at__isnull=True, used_at__isnull=True, agent__isnull=True); title = f'Unsold {request.GET["plan"]} (shop stock)'
    elif request.GET.get('sample'):
        qs = qs.none(); title = 'Test sheet'
    else:
        qs = qs.filter(status='active', sold_at__isnull=True, used_at__isnull=True, agent__isnull=True); title = 'Unsold shop stock'
    limit = min(2000, int(request.GET.get('limit') or 1000))
    rows = voucher_print_rows(business, qs[:limit])
    return render(request, 'core/studio/voucher_print.html', {
        'title': title, 'design': dsg, 'designs': business.voucher_designs.all(), 'config_json': _safe_json(config),
        'rows_json': _safe_json(rows), 'business_json': _safe_json(business_ctx(business)), 'count': len(rows),
        'sample': bool(request.GET.get('sample')), 'papers_json': _safe_json({k: list(v) for k, v in PAPERS.items()}),
        'ads_json': _safe_json(ads_for(business, 'voucher')),
        'query': request.GET.urlencode(), 'sample_len': portal_code_length(business),
    })



# ─────────────────────── Put the portal on the routers ───────────────────────
def portal_router_file(request, token, name):
    """Public, token-protected: the router downloads its hotspot pages from here."""
    from .portal_deploy import FILES, default_pages, read_token
    from .models import Router
    data = read_token(token)
    if not data or name not in FILES:
        raise Http404
    router = Router.objects.select_related('business').filter(pk=data.get('r'), business_id=data.get('b')).first()
    page = default_pages(router.business).get(name) if router else None
    if not page:
        raise Http404
    base = request.build_absolute_uri('/').rstrip('/')
    resp = HttpResponse(_export_html(page, base), content_type='text/html; charset=utf-8')
    resp['Cache-Control'] = 'no-store'
    return resp


def _deploy_rows(business):
    from .portal_deploy import version_for
    current = version_for(business)
    deps = {d.router_id: d for d in business.portal_deployments.all()}
    rows = []
    for r in business.routers.all().order_by('name'):
        d = deps.get(r.id)
        rows.append({'id': r.id, 'name': r.name, 'mode': r.connection_mode, 'online': r.status == 'Online',
                     'status': d.status if d else 'none', 'status_label': d.get_status_display() if d else 'Not installed',
                     'installed_at': d.installed_at.isoformat() if d and d.installed_at else None, 'error': d.error if d else '',
                     'files': d.files if d else [], 'outdated': bool(d and d.status == 'installed' and d.version != current)})
    return rows


@login_required
def portal_deploy_status(request):
    return JsonResponse({'routers': _deploy_rows(_b(request))})


@login_required
@require_POST
def portal_deploy(request):
    from .portal_deploy import deploy, reset
    business = _b(request)
    ids = [int(x) for x in request.POST.getlist('router') if str(x).isdigit()]
    routers = business.routers.filter(pk__in=ids) if ids else business.routers.all()
    if not routers.exists():
        return JsonResponse({'success': False, 'message': 'Add a router first.', 'routers': []}, status=400)
    fn = reset if request.POST.get('action') == 'reset' else deploy
    results = [fn(r, user=request.user) for r in routers]
    log(business, 'Portal Deployed' if fn is deploy else 'Portal Removed', '; '.join(m for _, m in results)[:500])
    return JsonResponse({'success': all(ok for ok, _ in results), 'message': ' '.join(m for _, m in results), 'routers': _deploy_rows(business)})
