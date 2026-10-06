"""Free websites before voucher login (MikroTik HotSpot walled garden).

TapTap manages only rows whose comment begins ``TapTap-free:``. Manual WinBox
walled-garden entries are never modified.

A leading ``www.`` host is expanded to its apex too. This matters because many
sites redirect www.example.com -> example.com; without the apex rule an
unauthenticated HotSpot customer is captured by the login page at the redirect.
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
from django.urls import path

MANAGED_PREFIX = "TapTap-free:"
MAX_SITES = 12
MAX_PATTERNS = MAX_SITES * 3
PURPOSES = {"advert", "portal", "public", "other"}
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)

class FreeAccessError(ValueError):
    pass

def _is_ip(host):
    try:
        ipaddress.ip_address(host); return True
    except ValueError:
        return False

def normalize_host(value):
    raw = str(value or "").strip()
    if not raw: raise FreeAccessError("Enter a website or domain.")
    probe = raw if "://" in raw else "//" + raw
    try:
        host = urlsplit(probe).hostname
    except ValueError:
        host = None
    if not host: host = raw.split("/", 1)[0].split(":", 1)[0]
    host = str(host or "").strip().lower().rstrip(".")
    if host.startswith("*."): host = host[2:]
    if not host or host in {"*", "."}: raise FreeAccessError("That website is too broad.")
    if _is_ip(host): return host
    try: host = host.encode("idna").decode("ascii")
    except UnicodeError as exc: raise FreeAccessError("That website name is not valid.") from exc
    if not DOMAIN_RE.fullmatch(host): raise FreeAccessError(f'"{raw}" is not a valid website/domain.')
    return host

def clean_items(raw_items):
    if not isinstance(raw_items, list): raise FreeAccessError("The free-access website list is invalid.")
    out, seen = [], set()
    for row in raw_items:
        if not isinstance(row, dict): continue
        entered = str(row.get("host") or "").strip()
        if not entered: continue
        host = normalize_host(entered)
        if host in seen: continue
        seen.add(host)
        purpose = str(row.get("purpose") or "other")
        if purpose not in PURPOSES: purpose = "other"
        sub = bool(row.get("include_subdomains", True))
        if "." not in host and not _is_ip(host): sub = False
        out.append({"host": host, "label": str(row.get("label") or "").strip()[:100], "purpose": purpose,
                    "include_subdomains": sub, "enabled": bool(row.get("enabled", True))})
    if len(out) > MAX_SITES: raise FreeAccessError(f"Use at most {MAX_SITES} free-access websites.")
    return out

def sites_for_business(business, enabled_only=True):
    qs = business.free_access_sites.all()
    return list(qs.filter(enabled=True) if enabled_only else qs)

def save_sites(business, items):
    from .models_free_access import FreeAccessSite
    items = clean_items(items); wanted = {x["host"]: x for x in items}
    with transaction.atomic():
        existing = {x.host: x for x in FreeAccessSite.objects.select_for_update().filter(business=business)}
        for host, data in wanted.items():
            row = existing.pop(host, None)
            if row is None:
                FreeAccessSite.objects.create(business=business, **data); continue
            changed = []
            for f in ("label", "purpose", "include_subdomains", "enabled"):
                if getattr(row, f) != data[f]: setattr(row, f, data[f]); changed.append(f)
            if changed: row.save(update_fields=changed + ["updated_at"])
        if existing: FreeAccessSite.objects.filter(pk__in=[x.pk for x in existing.values()]).delete()
    return items

def _comment(kind, host): return f"{MANAGED_PREFIX}{kind}:{host}"

def expanded_patterns(host, include_subdomains=True):
    host = normalize_host(host)
    if _is_ip(host): return [(host, _comment("host", host))]
    out = [(host, _comment("host", host))]
    if host.startswith("www.") and host.count(".") >= 2:
        apex = host[4:]
        out.append((apex, _comment("apex", apex)))
        if include_subdomains: out.append(("*." + apex, _comment("sub", apex)))
    elif include_subdomains:
        out.append(("*." + host, _comment("sub", host)))
    seen, unique = set(), []
    for p, c in out:
        if p not in seen: seen.add(p); unique.append((p, c))
    return unique

def _patterns(business):
    seen = set()
    for row in sites_for_business(business, True):
        for pattern, comment in expanded_patterns(row.host, bool(row.include_subdomains)):
            if pattern not in seen: seen.add(pattern); yield pattern, comment

def _desired(business): return [{"pattern": p, "comment": c} for p, c in _patterns(business)]
def _row_id(row): return row.get("id") or row.get(".id")

def apply_api(service, business):
    resource = service.resource("/ip/hotspot/walled-garden")
    rows = resource.get(); managed = {}
    for row in rows:
        c = str(row.get("comment") or "")
        if c.startswith(MANAGED_PREFIX): managed.setdefault(c, []).append(row)
    desired = list(_patterns(business)); wanted_comments = set(); added = updated = removed = 0
    for pattern, comment in desired:
        wanted_comments.add(comment); have = managed.get(comment) or []
        values = {"action": "allow", "dst_host": pattern, "comment": comment, "disabled": "no"}
        if have:
            rid = _row_id(have[0])
            if rid: resource.set(id=rid, **values); updated += 1
            for dupe in have[1:]:
                rid = _row_id(dupe)
                if rid: resource.remove(id=rid); removed += 1
        else: resource.add(**values); added += 1
    for comment, old in managed.items():
        if comment in wanted_comments: continue
        for row in old:
            rid = _row_id(row)
            if rid: resource.remove(id=rid); removed += 1
    # Re-read: direct/Tunnel apply must not claim success if RouterOS did not retain a rule.
    found = {(str(r.get("comment") or ""), str(r.get("dst-host") or r.get("dst_host") or ""))
             for r in resource.get() if str(r.get("comment") or "").startswith(MANAGED_PREFIX)
             and str(r.get("disabled") or "no").lower() not in {"yes", "true", "1"}}
    missing = [p for p, c in desired if (c, p) not in found]
    if missing: raise RuntimeError("Router did not retain these free-access rules: " + ", ".join(missing[:6]))
    return added, updated, removed

def _rq(value):
    value = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("\r", "").replace("\n", "")
    return '"' + value + '"'

def _command_patterns(params):
    out = []
    for row in (params or {}).get("patterns") or []:
        if not isinstance(row, dict): continue
        p = str(row.get("pattern") or "").strip().lower(); wildcard = p.startswith("*.")
        h = normalize_host(p[2:] if wildcard else p); p = "*." + h if wildcard else h
        c = str(row.get("comment") or "").strip()
        if not c.startswith(MANAGED_PREFIX) or any(ch in c for ch in '\r\n"$;{}[]'): raise ValueError("Invalid free-access rule marker.")
        out.append((p, c))
    if len(out) > MAX_PATTERNS: raise ValueError("Too many free-access router patterns.")
    return out

def link_command_body(params):
    patterns = _command_patterns(params)
    parts = [':foreach i in=[/ip hotspot walled-garden find where comment~' + _rq("^TapTap-free:") + '] do={ /ip hotspot walled-garden remove $i }']
    for pattern, comment in patterns:
        parts.append("/ip hotspot walled-garden add action=allow dst-host=" + _rq(pattern) + " disabled=no comment=" + _rq(comment))
    body = "; ".join(parts)
    if len(body) > 3200: raise ValueError("The expanded free-access list is too large for one TapTap Link command.")
    return body

def apply_router(router, user=None):
    try:
        from .linkops import uses_link
        if uses_link(router):
            from .linkops import send
            send(router, "walled_garden_sync", {"patterns": _desired(router.business)}, label="Free-access websites", user=user, minutes=60*24)
            return True, f"{router.name}: queued through TapTap Link"
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try: a, u, r = apply_api(svc, router.business)
        finally: svc.close()
        return True, f"{router.name}: verified on router ({a} added, {u} refreshed, {r} removed)"
    except Exception as exc: return False, f"{router.name}: {exc}"

def apply_all(business, user=None): return [apply_router(r, user) for r in business.routers.all()]
def _current_business(request): return getattr(request, "tt_business", None) or request.user.business

def _save_from_request(request):
    business = _current_business(request)
    try: items = save_sites(business, json.loads(request.POST.get("free_access_json") or "[]"))
    except (json.JSONDecodeError, FreeAccessError) as exc: messages.error(request, str(exc)); return redirect("/settings/#free-access")
    active = sum(1 for x in items if x["enabled"])
    messages.success(request, f"Saved {len(items)} free-access website{'s' if len(items)!=1 else ''} ({active} enabled).")
    www = [x["host"] for x in items if x["enabled"] and x["host"].startswith("www.")]
    if www: messages.info(request, "TapTap also allows the matching non-www address for: " + ", ".join(www[:4]) + ".")
    if request.POST.get("free_access_apply"):
        results = apply_all(business, request.user)
        if not results: messages.info(request, "Saved. There are no routers in this business to update yet.")
        for ok, msg in results: (messages.info if ok else messages.warning)(request, msg)
    return redirect("/settings/#free-access")

def _install_agent_command():
    from . import agent
    if getattr(agent, "_free_access_installed", False): return
    agent.SAFE_KINDS.add("walled_garden_sync"); agent.DEFAULT_EXPIRY.setdefault("walled_garden_sync", 60*24)
    original = agent.command_body
    @functools.wraps(original)
    def command_body(cmd): return link_command_body(cmd.params or {}) if cmd.kind == "walled_garden_sync" else original(cmd)
    agent.command_body = command_body; agent._free_access_installed = True

def _install_settings_handler():
    from . import urls, views
    if getattr(views, "_free_access_settings_installed", False): return
    original = views.settings_view
    @functools.wraps(original)
    def wrapped(request, *args, **kwargs):
        if getattr(request.user, "is_authenticated", False) and request.method == "POST" and (request.POST.get("free_access_form") == "1" or request.POST.get("free_access_apply") == "1"):
            return _save_from_request(request)
        return original(request, *args, **kwargs)
    views.settings_view = wrapped
    for i, p in enumerate(list(urls.urlpatterns)):
        if getattr(p, "name", None) == "settings": urls.urlpatterns[i] = path("settings/", wrapped, name="settings"); break
    views._free_access_settings_installed = True

def install():
    _install_agent_command(); _install_settings_handler()
