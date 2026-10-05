"""Free websites before voucher login (MikroTik HotSpot walled garden).

The business configures sites in Settings. TapTap stores the desired list and can
push it to every managed MikroTik. Only RouterOS rules whose comment begins with
MANAGED_PREFIX are changed; manual WinBox walled-garden rules are never touched.

TapTap Link routers receive a fixed-catalogue `walled_garden_sync` command. Direct
API routers and Link routers with a healthy TapTap Tunnel are changed through the
RouterOS API.
"""
from __future__ import annotations

import functools
import ipaddress
import json
import re
from urllib.parse import urlsplit

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect


MANAGED_PREFIX = 'TapTap-free:'
MAX_SITES = 12
PURPOSES = {'advert', 'portal', 'public', 'other'}
DOMAIN_RE = re.compile(
    r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*'
    r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$',
    re.I,
)


class FreeAccessError(ValueError):
    pass


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_host(value):
    """Return one safe hostname from a hostname or full URL."""
    raw = str(value or '').strip()
    if not raw:
        raise FreeAccessError('Enter a website or domain.')

    probe = raw if '://' in raw else '//' + raw
    try:
        parts = urlsplit(probe)
        host = parts.hostname
    except ValueError:
        host = None

    if not host:
        host = raw.split('/', 1)[0].split(':', 1)[0]

    host = str(host or '').strip().lower().rstrip('.')
    if host.startswith('*.'):
        host = host[2:]

    if not host or host in {'*', '.'}:
        raise FreeAccessError('That website is too broad.')

    if _is_ip(host):
        return host

    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise FreeAccessError('That website name is not valid.') from exc

    if not DOMAIN_RE.fullmatch(host):
        raise FreeAccessError(
            f'"{raw}" is not a valid website/domain.'
        )

    return host


def clean_items(raw_items):
    """Validate/deduplicate the JSON rows from Settings."""
    if not isinstance(raw_items, list):
        raise FreeAccessError('The free-access website list is invalid.')

    out = []
    seen = set()

    for row in raw_items:
        if not isinstance(row, dict):
            continue

        entered = str(row.get('host') or '').strip()
        if not entered:
            continue

        host = normalize_host(entered)
        if host in seen:
            continue
        seen.add(host)

        purpose = str(row.get('purpose') or 'other')
        if purpose not in PURPOSES:
            purpose = 'other'

        include_subdomains = bool(row.get('include_subdomains', True))
        if '.' not in host and not _is_ip(host):
            include_subdomains = False

        out.append({
            'host': host,
            'label': str(row.get('label') or '').strip()[:100],
            'purpose': purpose,
            'include_subdomains': include_subdomains,
            'enabled': bool(row.get('enabled', True)),
        })

    if len(out) > MAX_SITES:
        raise FreeAccessError(
            f'Use at most {MAX_SITES} free-access websites. '
            'For an advert that uses several CDN domains, add only the domains '
            'actually required before login.'
        )

    return out


def sites_for_business(business, enabled_only=True):
    qs = business.free_access_sites.all()
    if enabled_only:
        qs = qs.filter(enabled=True)
    return list(qs)


def save_sites(business, items):
    """Replace this business's desired list while preserving stable rows."""
    from .models_free_access import FreeAccessSite

    items = clean_items(items)
    wanted = {x['host']: x for x in items}

    with transaction.atomic():
        existing = {
            row.host: row
            for row in FreeAccessSite.objects.select_for_update().filter(
                business=business
            )
        }

        for host, data in wanted.items():
            row = existing.pop(host, None)
            if row is None:
                FreeAccessSite.objects.create(
                    business=business,
                    **data,
                )
                continue

            changed = []
            for field in (
                'label',
                'purpose',
                'include_subdomains',
                'enabled',
            ):
                if getattr(row, field) != data[field]:
                    setattr(row, field, data[field])
                    changed.append(field)
            if changed:
                row.save(update_fields=changed + ['updated_at'])

        if existing:
            FreeAccessSite.objects.filter(
                pk__in=[x.pk for x in existing.values()]
            ).delete()

    return items


def _desired(business):
    return [
        {
            'host': row.host,
            'sub': bool(row.include_subdomains),
        }
        for row in sites_for_business(business, enabled_only=True)
    ]


def _comment(host, sub=False):
    return MANAGED_PREFIX + ('sub:' if sub else 'host:') + host


def _patterns(business):
    for row in sites_for_business(business, enabled_only=True):
        yield row.host, _comment(row.host, False)
        if row.include_subdomains and not _is_ip(row.host):
            yield '*.' + row.host, _comment(row.host, True)


def apply_api(service, business):
    """Reconcile only TapTap-managed /ip hotspot walled-garden rows."""
    resource = service.resource('/ip/hotspot/walled-garden')
    rows = resource.get()
    managed = {}

    for row in rows:
        comment = str(row.get('comment') or '')
        if not comment.startswith(MANAGED_PREFIX):
            continue
        managed.setdefault(comment, []).append(row)

    desired_comments = set()
    added = updated = removed = 0

    for pattern, comment in _patterns(business):
        desired_comments.add(comment)
        have = managed.get(comment) or []
        values = {
            'action': 'allow',
            'dst_host': pattern,
            'comment': comment,
            'disabled': 'no',
        }

        if have:
            first = have[0]
            if first.get('id'):
                resource.set(id=first['id'], **values)
                updated += 1
            for duplicate in have[1:]:
                if duplicate.get('id'):
                    resource.remove(id=duplicate['id'])
                    removed += 1
        else:
            resource.add(**values)
            added += 1

    for comment, old_rows in managed.items():
        if comment in desired_comments:
            continue
        for row in old_rows:
            if row.get('id'):
                resource.remove(id=row['id'])
                removed += 1

    return added, updated, removed


def _rq(value):
    """RouterOS string quote for the fixed Link command."""
    value = (
        str(value)
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('$', '\\$')
        .replace('\r', '')
        .replace('\n', '')
    )
    return '"' + value + '"'


def _command_items(params):
    rows = []
    for item in (params or {}).get('sites') or []:
        if not isinstance(item, dict):
            continue
        host = normalize_host(item.get('host'))
        sub = bool(item.get('sub')) and not _is_ip(host)
        rows.append({'host': host, 'sub': sub})

    if len(rows) > MAX_SITES:
        raise ValueError('Too many free-access websites in one router command.')
    return rows


def link_command_body(params):
    """RouterOS command sent by TapTap Link. It touches TapTap-managed rows only."""
    items = _command_items(params)

    parts = [
        ':foreach i in=[/ip hotspot walled-garden find where comment~'
        + _rq('^' + re.escape(MANAGED_PREFIX))
        + '] do={ /ip hotspot walled-garden remove $i }'
    ]

    for item in items:
        host = item['host']
        parts.append(
            '/ip hotspot walled-garden add action=allow dst-host='
            + _rq(host)
            + ' comment='
            + _rq(_comment(host, False))
        )
        if item['sub']:
            parts.append(
                '/ip hotspot walled-garden add action=allow dst-host='
                + _rq('*.' + host)
                + ' comment='
                + _rq(_comment(host, True))
            )

    body = '; '.join(parts)
    if len(body) > 3200:
        raise ValueError(
            'The free-access website list is too large for one TapTap Link command.'
        )
    return body


def apply_router(router, user=None):
    """Push desired rules to one router. Returns (ok, message)."""
    business = router.business

    try:
        from .linkops import uses_link

        if uses_link(router):
            from .linkops import send

            send(
                router,
                'walled_garden_sync',
                {'sites': _desired(business)},
                label='Free-access websites',
                user=user,
                minutes=60 * 24,
            )
            return True, f'{router.name}: queued through TapTap Link'

        from .mikrotik import MikroTikService

        service = MikroTikService(router).connect()
        try:
            added, updated, removed = apply_api(service, business)
        finally:
            service.close()

        return (
            True,
            f'{router.name}: applied '
            f'({added} added, {updated} refreshed, {removed} removed)',
        )

    except Exception as exc:
        return False, f'{router.name}: {exc}'


def apply_all(business, user=None):
    return [apply_router(router, user) for router in business.routers.all()]


def _save_from_request(request):
    business = request.user.business

    try:
        raw = json.loads(request.POST.get('free_access_json') or '[]')
        items = save_sites(business, raw)
    except (json.JSONDecodeError, FreeAccessError) as exc:
        messages.error(request, str(exc))
        return redirect('/settings/#free-access')

    active = sum(1 for item in items if item['enabled'])
    messages.success(
        request,
        f'Saved {len(items)} free-access website'
        f'{"s" if len(items) != 1 else ""} '
        f'({active} enabled).',
    )

    try:
        from .utils import log
        log(
            business,
            'Free Access Websites',
            f'{active} enabled walled-garden website(s) saved'
            + (
                ' and sent to routers'
                if request.POST.get('free_access_apply')
                else ''
            ),
        )
    except Exception:
        pass

    if request.POST.get('free_access_apply'):
        results = apply_all(business, request.user)
        if not results:
            messages.info(
                request,
                'Saved. There are no routers in this business to update yet.',
            )
        for ok, message in results:
            (messages.info if ok else messages.warning)(request, message)

    return redirect('/settings/#free-access')


def _install_agent_command():
    """Extend TapTap Link's fixed command catalogue without replacing agent.py."""
    from . import agent

    if getattr(agent, '_free_access_installed', False):
        return

    agent.SAFE_KINDS.add('walled_garden_sync')
    agent.DEFAULT_EXPIRY.setdefault('walled_garden_sync', 60 * 24)

    original = agent.command_body

    @functools.wraps(original)
    def command_body(cmd):
        if cmd.kind == 'walled_garden_sync':
            return link_command_body(cmd.params or {})
        return original(cmd)

    agent.command_body = command_body
    agent._free_access_installed = True


def _install_settings_handler():
    """Let the dedicated Settings form POST to the existing /settings/ route."""
    from . import views

    if getattr(views, '_free_access_settings_installed', False):
        return

    original = views.settings_view

    @functools.wraps(original)
    def settings_with_free_access(request, *args, **kwargs):
        if not getattr(request.user, 'is_authenticated', False):
            return original(request, *args, **kwargs)

        if (
            request.method == 'POST'
            and (
                request.POST.get('free_access_form') == '1'
                or request.POST.get('free_access_apply') == '1'
            )
        ):
            return _save_from_request(request)

        return original(request, *args, **kwargs)

    views.settings_view = settings_with_free_access
    views._free_access_settings_installed = True


def install():
    _install_agent_command()
    _install_settings_handler()
