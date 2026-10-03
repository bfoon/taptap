"""Put the voucher portal ON the router, so customers see it before they have Internet.

A phone that joined the hotspot cannot reach TapTap yet, so the page must be served
by the router itself. TapTap gives the router a short-lived signed link and the
router downloads the pages straight into every hotspot HTML folder it uses:

    login.html   the voucher page (default login page)
    alogin.html  "you are connected" page (default redirect page, if any)
    status.html  session status page (default status page, if any)

Works over the direct API (a temporary RouterOS script) and over TapTap Link (a
queued command). Plans, prices and branding are baked into the pages, so TapTap
re-installs automatically when they change.
"""
import hashlib
import json
import logging
import re
import time

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils import timezone

from .models import PortalDeployment

logger = logging.getLogger('taptap.portal')
SALT = 'taptap-portal-files'
TOKEN_MAX_AGE = 2 * 3600
FILES = {'login': 'login', 'alogin': 'redirect', 'status': 'status'}   # file → portal page kind
HOST_RE = re.compile(r'^[A-Za-z0-9.-]{1,253}$')


def default_pages(business):
    pages = {}
    for fname, kind in FILES.items():
        page = (business.portal_pages.filter(kind=kind, is_default=True).first()
                or (business.portal_pages.filter(kind=kind, is_published=True).first() if kind == 'login' else None))
        if page:
            pages[fname] = page
    return pages


def version_for(business):
    """Fingerprint of everything baked into the pages (not the export timestamp)."""
    from .views_studio import business_ctx, plans_ctx
    pages = default_pages(business)
    blob = json.dumps({'pages': {f: [p.pk, p.config, str(p.updated_at)] for f, p in pages.items()},
                       'biz': business_ctx(business), 'plans': plans_ctx(business)}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def site_base(request=None):
    from .agent import base_url
    return base_url(request)


def make_token(router):
    return signing.dumps({'b': router.business_id, 'r': router.pk}, salt=SALT)


def read_token(token):
    try:
        return signing.loads(token, salt=SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return None


def check_flag(router, base):
    if not base.startswith('https://'):
        return 'no'
    try:
        if not router.agent.verify_tls:
            return 'no'
    except Exception:
        pass
    return 'yes-without-crl' if getattr(settings, 'AGENT_VERIFY_TLS', True) else 'no'


def _rs(v):
    from .agent import rs
    return rs(v)


def install_script(base, token, files, host, check, dns_name=''):
    """RouterOS script: download each page into every hotspot HTML folder; allow TapTap in the walled garden."""
    lines = [':local n 0']
    fetches = ' '.join(
        f'/tool fetch url={_rs(f"{base}/p/router-files/{token}/{f}.html")} dst-path=($d . "/{f}.html") check-certificate={check} duration=30s idle-timeout=20s;'
        for f in files)
    lines.append(':foreach p in=[/ip hotspot profile find] do={ :local d [/ip hotspot profile get $p html-directory]; '
                 ':if ([:len $d] = 0) do={ :set d "hotspot" }; '
                 f':do {{ {fetches} :set n ($n + 1) }} on-error={{ :log warning ("TapTap portal: could not write to " . $d) }} }}')
    if host and HOST_RE.match(host):
        lines.append(f':if ([:len [/ip hotspot walled-garden find dst-host={_rs(host)}]] = 0) do={{ '
                     f'/ip hotspot walled-garden add dst-host={_rs(host)} comment="TapTap portal (device id, adverts)" }}')
        # An easy address for the login page (e.g. login.wifi) — only where the hotspot has no name yet and does
        # not use HTTPS login (its certificate is tied to the name), so nothing that works is changed.
        if dns_name:
            lines.append(f':do {{ :foreach p in=[/ip hotspot profile find where dns-name="" and !(login-by~"https")] do={{ '
                         f'/ip hotspot profile set $p dns-name={_rs(dns_name)} }} }} on-error={{}}')
        # HTTPS calls (voucher check, wrong-code count, warnings, device id) are not covered by the HTTP walled
        # garden: without this the login page cannot reach TapTap before login and logs in blind.
        lines.append(f':do {{ :if ([:len [/ip hotspot walled-garden ip find dst-host={_rs(host)}]] = 0) do={{ '
                     f'/ip hotspot walled-garden ip add dst-host={_rs(host)} action=accept comment="TapTap portal (https: voucher check, warnings)" }} }} on-error={{ :log warning "TapTap portal: could not add the https walled-garden entry" }}')
    lines.append(':if ($n = 0) do={ :error "TapTap portal: no hotspot is set up on this router" }')
    lines.append(':log info "TapTap portal installed"')
    return '; '.join(lines)


def reset_script():
    """Put MikroTik's own default hotspot pages back."""
    return (':foreach s in=[/ip hotspot find] do={ :do { /ip hotspot reset-html $s } on-error={} }; '
            ':log info "TapTap portal removed: MikroTik default pages restored"')


def _host(base):
    return base.split('://', 1)[-1].split('/')[0].split(':')[0]


def deploy(router, base=None, user=None):
    """Install the business's default portal pages on one router. Returns (ok, message)."""
    business = router.business
    base = (base or site_base()).rstrip('/')
    pages = default_pages(business)
    dep, _ = PortalDeployment.objects.get_or_create(router=router, defaults={'business': business})
    if 'login' not in pages:
        dep.status, dep.error = 'failed', 'Publish a login page first (Portal Studio).'
        dep.save(update_fields=['status', 'error'])
        return False, dep.error
    if not base.startswith(('http://', 'https://')):
        dep.status, dep.error = 'failed', 'Set SITE_URL in .env so routers know where to download the pages from.'
        dep.save(update_fields=['status', 'error'])
        return False, dep.error
    files = list(pages)
    token = make_token(router)
    script = install_script(base, token, files, _host(base), check_flag(router, base), _dns_name(business))
    dep.files, dep.version, dep.requested_at, dep.error = files, version_for(business), timezone.now(), ''
    if router.connection_mode == 'agent':
        from . import agent as link
        try:
            link.queue(router, 'portal_install', {'files': files, 'token': token, 'base': base}, label='Install the voucher portal', user=user, minutes=60)
        except ValueError as exc:
            dep.status, dep.error = 'failed', str(exc)[:300]
            dep.save()
            return False, dep.error
        dep.status, dep.via = 'queued', 'link'
        dep.save()
        return True, f'{router.name}: sent through TapTap Link — installs at its next check-in.'
    try:
        _run_script(router, script)
        dep.status, dep.via, dep.installed_at = 'installed', 'api', timezone.now()
        dep.save()
        return True, f'{router.name}: portal installed.'
    except Exception as exc:
        dep.status, dep.via, dep.error = 'failed', 'api', str(exc)[:300]
        dep.save()
        return False, f'{router.name}: {exc}'


def reset(router, user=None):
    dep, _ = PortalDeployment.objects.get_or_create(router=router, defaults={'business': router.business})
    try:
        if router.connection_mode == 'agent':
            from . import agent as link
            link.queue(router, 'portal_reset', {}, label='Restore MikroTik default hotspot pages', user=user, minutes=60)
        else:
            _run_script(router, reset_script())
        dep.status, dep.error, dep.files, dep.version = 'removed', '', [], ''
        dep.save()
        return True, f'{router.name}: MikroTik default pages restored.'
    except Exception as exc:
        dep.error = str(exc)[:300]
        dep.save(update_fields=['error'])
        return False, f'{router.name}: {exc}'


def _run_script(router, source):
    """Run a RouterOS script over the direct API (temporary /system script), then remove it."""
    from .mikrotik import MikroTikService
    with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10)) as svc:
        scripts = svc.resource('/system/script')
        name = 'taptap-portal'
        for s in scripts.get(name=name):
            scripts.remove(id=s.get('id') or s.get('.id'))
        scripts.add(name=name, source=source, policy='ftp,read,write,test,sensitive')
        try:
            sid = scripts.get(name=name)[0]
            scripts.call('run', {'.id': sid.get('id') or sid.get('.id')})
            time.sleep(1)
        finally:
            for s in scripts.get(name=name):
                scripts.remove(id=s.get('id') or s.get('.id'))


def deploy_business(business, routers=None, base=None, user=None):
    results = []
    for r in (routers if routers is not None else business.routers.all()):
        results.append(deploy(r, base, user))
    return results


def schedule_redeploy(business):
    """After a page, plan or branding change: re-install on routers that already have the portal (debounced)."""
    if not business.portal_deployments.filter(status__in=['installed', 'queued', 'failed']).exists():
        return
    if not cache.add(f'tt:portal:redeploy:{business.pk}', 1, 30):
        return
    from .tasks import redeploy_portal
    try:
        redeploy_portal.apply_async((business.pk,), countdown=30)
    except Exception as exc:
        logger.info('portal redeploy not queued: %s', exc)


def _dns_name(business):
    name = str(getattr(business, 'hotspot_dns_name', '') or '').strip().lower()
    return name if re.match(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$', name) and len(name) <= 60 else ''


def link_command_body(cmd, check):
    p = cmd.params or {}
    if cmd.kind == 'portal_reset':
        return reset_script()
    files = [f for f in p.get('files', []) if f in FILES]
    token, base = str(p.get('token', '')), str(p.get('base', '')).rstrip('/')
    if not files or not read_token(token) or not base.startswith(('http://', 'https://')):
        raise ValueError('Portal install link expired — press "Put on routers" again.')
    return install_script(base, token, files, _host(base), check, _dns_name(cmd.router.business))


def link_ack(cmd, ok):
    dep = PortalDeployment.objects.filter(router=cmd.router).first()
    if not dep:
        return
    if cmd.kind == 'portal_install':
        dep.status = 'installed' if ok else 'failed'
        dep.installed_at = timezone.now() if ok else dep.installed_at
        dep.error = '' if ok else (cmd.result or 'The router could not download the pages (no hotspot set up, or TapTap unreachable).')[:300]
    elif cmd.kind == 'portal_reset' and ok:
        dep.status, dep.files, dep.version = 'removed', [], ''
    dep.save()
