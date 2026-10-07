"""Live owner-visible support presence.

This module sits on top of core.support_access. It does NOT change who may grant
support access or how authorization is validated.

What it adds
------------
* When an approved TapTap support user is viewing a business portal, the current
  page is stored in Django cache (Redis in production).
* The support browser sends a small heartbeat so "Finance" remains visibly
  active even when the technician stays on the page without clicking.
* The actual business owner gets a live banner at the top of every portal page.
* The banner refreshes automatically and shows support name, current page,
  last activity, current page duration and approval expiry.
* The owner can revoke the grant directly from the banner.
* Revocation is still handled by support_access.owner_decide(), so the existing
  durable PlatformAudit record and chat notification are preserved.
* The support heartbeat is rejected as soon as approval is revoked. The open
  support browser then leaves the customer portal automatically.

Privacy / data minimisation
---------------------------
Presence stores only:
  support user id/name, business id, support request token, friendly page name,
  request PATH (never the query string), last-seen/page-entered timestamps and
  grant expiry.

No form data, query values, response content, finance values, voucher codes,
customer details or page HTML are copied into the presence record.
"""
from __future__ import annotations

from functools import wraps
from html import escape
import json
import logging
import re
import time

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.urls import NoReverseMatch, path, reverse
from django.views.decorators.http import require_GET, require_POST

from . import support_access
from .models import Business

logger = logging.getLogger("taptap.support_presence")

CACHE_TTL = 150
ACTIVE_SECONDS = 25
HEARTBEAT_SECONDS = 8
OWNER_POLL_SECONDS = 4

_INSTALLED = False

# Friendly page names shown to the owner. Exact names take priority, then
# prefix groups. Unknown internal names are humanised rather than exposed raw.
EXACT_PAGE_NAMES = {
    "dashboard": "Overview",
    "finance": "Finance",
    "reports": "Reports",
    "vouchers": "Vouchers",
    "voucher_detail": "Voucher details",
    "generate_vouchers": "Generate vouchers",
    "batches": "Batches",
    "plans": "Plans",
    "members": "Members",
    "routers": "MikroTik Control",
    "router_inventory": "Router inventory",
    "active_users": "Active users",
    "ip_bindings": "IP bindings",
    "topology": "Topology",
    "traffic": "Traffic",
    "devices": "Devices",
    "alerts": "Alerts",
    "security": "Security",
    "settings": "Settings",
    "support": "Support",
    "team": "Team",
    "subscription": "Subscription",
    "ads": "Adverts",
    "portal_studio": "Portal Studio",
    "voucher_designs": "Voucher Designer",
    "tracking": "Tracking",
    "app_control": "App control",
    "notifications": "Email alerts",
    "missing_vouchers": "Missing vouchers",
    "bonanza_list": "Bonanza",
    "voucher_bin": "Voucher bin",
    "sales_daily": "Daily sales",
    "account_password": "My password",
}

PREFIX_PAGE_NAMES = (
    ("finance_", "Finance"),
    ("agent_", "Finance · Agents"),
    ("batch_", "Batches"),
    ("voucher_", "Vouchers"),
    ("vouchers_", "Vouchers"),
    ("plan_", "Plans"),
    ("member_", "Members"),
    ("members_", "Members"),
    ("router_", "MikroTik Control"),
    ("routers_", "MikroTik Control"),
    ("wan_", "Internet / WAN"),
    ("topology_", "Topology"),
    ("traffic_", "Traffic"),
    ("device_", "Devices"),
    ("netdev_", "Other routers & APs"),
    ("ip_binding", "IP bindings"),
    ("security_", "Security"),
    ("sharing_", "Security · Internet sharing"),
    ("alerts_", "Alerts"),
    ("alert_", "Alerts"),
    ("report", "Reports"),
    ("portal_", "Portal Studio"),
    ("voucher_design", "Voucher Designer"),
    ("ad_", "Adverts"),
    ("bonanza_", "Bonanza"),
    ("team_", "Team"),
    ("subscription_", "Subscription"),
    ("support_", "Support"),
    ("tracking_", "Tracking"),
    ("app_", "App control"),
    ("live_", "Live sync"),
    ("fup_", "Fair usage"),
)


def _presence_key(business_id, token):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(token or ""))[:80]
    return f"tt:support:presence:{int(business_id)}:{safe}"


def _now():
    return time.time()


def _business(request):
    return getattr(request, "tt_business", None)


def _is_actual_owner(request, business=None):
    business = business or _business(request)
    return bool(
        business
        and getattr(request.user, "is_authenticated", False)
        and business.user_id == request.user.pk
        and not getattr(request, "tt_view_as", None)
    )


def _route_name(request):
    match = getattr(request, "resolver_match", None)
    return str(getattr(match, "url_name", "") or "")


def page_name(request):
    """Friendly current page name for live owner presence."""
    name = _route_name(request)
    if name in EXACT_PAGE_NAMES:
        return EXACT_PAGE_NAMES[name]
    for prefix, label in PREFIX_PAGE_NAMES:
        if name.startswith(prefix):
            return label
    if name:
        return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)[:80]
    path_value = str(getattr(request, "path", "") or "").strip("/")
    if not path_value:
        return "Portal"
    first = path_value.split("/", 1)[0].replace("-", " ").replace("_", " ")
    return first.title()[:80] or "Portal"


def safe_path(request):
    """Path only. Query strings are deliberately not retained."""
    value = str(getattr(request, "path", "") or "/")
    # Defensive length cap and control-character removal.
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
    return value[:220] or "/"


def _grant_from_request(request):
    grant = request.session.get(support_access.GRANT_KEY) if hasattr(request, "session") else None
    if not isinstance(grant, dict):
        return None
    try:
        business_id = int(grant.get("business_id") or 0)
        support_user_id = int(grant.get("support_user_id") or 0)
    except (TypeError, ValueError):
        return None
    token = str(grant.get("token") or "").strip()
    if not business_id or not support_user_id or not token:
        return None
    return {
        "business_id": business_id,
        "support_user_id": support_user_id,
        "token": token,
        "expires_at": str(grant.get("expires_at") or ""),
    }


def _valid_support_request(request):
    grant = _grant_from_request(request)
    if not grant:
        return None
    if not getattr(request.user, "is_authenticated", False) or not request.user.is_superuser:
        return None
    if grant["support_user_id"] != request.user.pk:
        return None
    if not support_access.validate_session_grant(
        request.user,
        grant["business_id"],
        request.session.get(support_access.GRANT_KEY),
    ):
        return None
    return grant


def _support_name(user):
    return (
        user.get_full_name()
        or user.first_name
        or user.email
        or user.username
        or "TapTap Support"
    ).strip()[:120]


def _record_support_navigation(request):
    """Store the page support is actually viewing after a successful HTML GET."""
    grant = _valid_support_request(request)
    if not grant:
        return

    token = grant["token"]
    key = _presence_key(grant["business_id"], token)
    now = _now()
    label = page_name(request)
    path_value = safe_path(request)

    try:
        old = cache.get(key) or {}
    except Exception:
        old = {}

    same_page = (
        str(old.get("page_path") or "") == path_value
        and str(old.get("page_name") or "") == label
    )
    data = {
        "business_id": grant["business_id"],
        "support_user_id": request.user.pk,
        "support_name": _support_name(request.user),
        "token": token,
        "page_name": label,
        "page_path": path_value,
        "page_since": old.get("page_since") if same_page else now,
        "last_seen": now,
        "expires_at": grant["expires_at"],
    }
    try:
        cache.set(key, data, CACHE_TTL)
    except Exception:
        logger.info("Could not store support presence", exc_info=True)


def _touch_support_presence(request):
    """Refresh activity without changing the page currently shown to the owner."""
    grant = _valid_support_request(request)
    if not grant:
        return None

    key = _presence_key(grant["business_id"], grant["token"])
    now = _now()
    try:
        data = cache.get(key) or {}
    except Exception:
        data = {}

    # Cache can be empty after Redis/app restart while an already-rendered
    # support page is still open. Recreate a minimal active presence.
    data.update(
        {
            "business_id": grant["business_id"],
            "support_user_id": request.user.pk,
            "support_name": _support_name(request.user),
            "token": grant["token"],
            "last_seen": now,
            "expires_at": grant["expires_at"],
        }
    )
    data.setdefault("page_name", "Portal")
    data.setdefault("page_path", "")
    data.setdefault("page_since", now)
    try:
        cache.set(key, data, CACHE_TTL)
    except Exception:
        logger.info("Could not refresh support presence", exc_info=True)
    return data


def _presence_for_state(business, state, now_ts=None):
    now_ts = now_ts or _now()
    token = state["token"]
    try:
        data = cache.get(_presence_key(business.pk, token)) or {}
    except Exception:
        data = {}

    last_seen = float(data.get("last_seen") or 0)
    page_since = float(data.get("page_since") or last_seen or now_ts)
    live = bool(last_seen and now_ts - last_seen <= ACTIVE_SECONDS)
    expires = state.get("grant_expires")
    remaining = max(0, int((expires.timestamp() - now_ts))) if expires else 0

    return {
        "token": token,
        "support_user_id": state["requester_id"],
        "support_name": state["requester_name"],
        "support_email": state.get("requester_email") or "",
        "reason": state.get("reason") or "",
        "live": live,
        "page_name": (data.get("page_name") or ("Portal" if live else ""))[:80],
        "page_path": (data.get("page_path") or "")[:220],
        "last_seen_seconds": max(0, int(now_ts - last_seen)) if last_seen else None,
        "page_seconds": max(0, int(now_ts - page_since)) if last_seen else None,
        "expires_seconds": remaining,
        "expires_at": expires.isoformat() if expires else None,
    }


@login_required
@require_GET
def live_state(request):
    """Owner-only polling endpoint for the banner."""
    business = _business(request)
    if not _is_actual_owner(request, business):
        return JsonResponse({"ok": False, "error": "Business owner only."}, status=403)

    states = []
    try:
        requests = support_access.owner_requests(business, limit=12)
        now_ts = _now()
        for state in requests:
            if state.get("status") != "approved":
                continue
            if not state.get("grant_expires") or state["grant_expires"] <= support_access.timezone.now():
                continue
            states.append(_presence_for_state(business, state, now_ts))
    except Exception:
        logger.exception("Could not build owner support presence")
        return JsonResponse({"ok": False, "error": "Could not read support status."}, status=500)

    # Live users first, then the approvals that are currently idle.
    states.sort(key=lambda row: (not row["live"], row["support_name"].lower()))
    return JsonResponse(
        {
            "ok": True,
            "business": business.business_name,
            "sessions": states,
            "poll_seconds": OWNER_POLL_SECONDS,
        }
    )


@login_required
@require_POST
def heartbeat(request):
    """Support browser heartbeat. A revoked grant gets a 403 immediately."""
    grant = _valid_support_request(request)
    if not grant:
        return JsonResponse(
            {"ok": False, "revoked": True, "error": "Support approval is no longer active."},
            status=403,
        )
    _touch_support_presence(request)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def revoke(request):
    """Actual business owner revokes one support token from the live banner."""
    business = _business(request)
    if not _is_actual_owner(request, business):
        return JsonResponse({"ok": False, "error": "Business owner only."}, status=403)

    token = str(request.POST.get("token") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "error": "Missing support grant."}, status=400)

    try:
        status, _ = support_access.owner_decide(request, business, token, "revoke")
    except (PermissionError, ValueError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    try:
        cache.delete(_presence_key(business.pk, token))
    except Exception:
        pass

    return JsonResponse(
        {
            "ok": status == "revoked",
            "status": status,
            "message": "Support access revoked. The active support browser is being removed from your portal.",
        }
    )


def _urls():
    try:
        platform_url = reverse("platform_overview")
    except NoReverseMatch:
        platform_url = "/platform/"
    return {
        "live": reverse("support_presence_live"),
        "heartbeat": reverse("support_presence_heartbeat"),
        "revoke": reverse("support_presence_revoke"),
        "platform": platform_url,
    }


def _support_heartbeat_script(request):
    urls = _urls()
    csrf = get_token(request)
    # Invisible script only. Current page is recorded server-side from the HTML
    # navigation; heartbeats merely keep that same page marked active.
    return f"""
<script id="tt-support-presence-heartbeat">
(function(){{
  const url={json.dumps(urls["heartbeat"])};
  const exitUrl={json.dumps(urls["platform"])};
  const csrf={json.dumps(csrf)};
  let stopped=false;

  async function beat(){{
    if(stopped) return;
    try{{
      const r=await fetch(url,{{
        method:'POST',
        credentials:'same-origin',
        cache:'no-store',
        headers:{{
          'X-CSRFToken':csrf,
          'X-Requested-With':'XMLHttpRequest'
        }}
      }});
      if(r.status===401 || r.status===403){{
        stopped=true;
        window.location.replace(exitUrl);
      }}
    }}catch(e){{}}
  }}

  beat();
  window.setInterval(beat,{HEARTBEAT_SECONDS * 1000});
}})();
</script>
"""


def _owner_banner(request):
    urls = _urls()
    csrf = get_token(request)
    return f"""
<div id="ttSupportPresence" class="tt-support-presence" hidden
     data-live-url="{escape(urls["live"], quote=True)}"
     data-revoke-url="{escape(urls["revoke"], quote=True)}"
     data-csrf="{escape(csrf, quote=True)}">
  <div class="tt-sp-summary">
    <span class="tt-sp-pulse" aria-hidden="true"></span>
    <i class="bi bi-headset"></i>
    <strong id="ttSpTitle">TapTap Support access</strong>
    <span id="ttSpCount" class="badge text-bg-dark"></span>
  </div>
  <div id="ttSpRows" class="tt-sp-rows"></div>
</div>

<style id="tt-support-presence-style">
.tt-support-presence{{position:sticky;top:0;z-index:1065;margin:-.1rem 0 1rem;border:1px solid #f0c36a;
  border-radius:14px;background:#fff7df;color:#3c2b05;box-shadow:0 8px 24px rgba(75,53,4,.12);overflow:hidden}}
.tt-sp-summary{{display:flex;align-items:center;gap:.55rem;padding:.7rem .85rem;background:#ffe9a8;border-bottom:1px solid #f0c36a}}
.tt-sp-pulse{{width:9px;height:9px;border-radius:50%;background:#8a6a22;flex:0 0 auto}}
.tt-support-presence.has-live .tt-sp-pulse{{background:#d83b2f;box-shadow:0 0 0 0 rgba(216,59,47,.42);animation:ttSpPulse 1.7s infinite}}
@keyframes ttSpPulse{{70%{{box-shadow:0 0 0 7px rgba(216,59,47,0)}}100%{{box-shadow:0 0 0 0 rgba(216,59,47,0)}}}}
.tt-sp-rows{{display:flex;flex-direction:column}}
.tt-sp-row{{display:flex;align-items:center;gap:.75rem;padding:.7rem .85rem;border-top:1px solid rgba(125,89,11,.12)}}
.tt-sp-row:first-child{{border-top:0}}
.tt-sp-avatar{{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:#33260b;color:white;font-weight:800;flex:0 0 auto}}
.tt-sp-info{{min-width:0;flex:1}}
.tt-sp-line{{display:flex;gap:.45rem;align-items:center;flex-wrap:wrap}}
.tt-sp-name{{font-weight:800}}.tt-sp-status{{font-size:.75rem;font-weight:800;border-radius:999px;padding:.16rem .45rem;background:#ece4cf}}
.tt-sp-status.live{{background:#f9d7d4;color:#9c2118}}
.tt-sp-meta{{font-size:.78rem;color:#70591f;margin-top:.14rem;word-break:break-word}}
.tt-sp-revoke{{white-space:nowrap}}
html[data-theme="dark"] .tt-support-presence{{background:#2f291a;color:#fff4d1;border-color:#70591f}}
html[data-theme="dark"] .tt-sp-summary{{background:#443717;border-color:#70591f}}
html[data-theme="dark"] .tt-sp-meta{{color:#ddc987}}
@media(max-width:767.98px){{
  .tt-support-presence{{top:56px;border-radius:10px}}
  .tt-sp-row{{align-items:flex-start;flex-wrap:wrap}}
  .tt-sp-revoke{{margin-left:42px}}
}}
@media(prefers-reduced-motion:reduce){{.tt-support-presence.has-live .tt-sp-pulse{{animation:none}}}}
</style>

<script id="tt-support-presence-owner">
(function(){{
  const box=document.getElementById('ttSupportPresence');
  if(!box) return;
  const rows=document.getElementById('ttSpRows');
  const title=document.getElementById('ttSpTitle');
  const count=document.getElementById('ttSpCount');
  const liveUrl=box.dataset.liveUrl;
  const revokeUrl=box.dataset.revokeUrl;
  const csrf=box.dataset.csrf;
  let timer=null;

  function age(seconds){{
    if(seconds===null || seconds===undefined) return '';
    seconds=Math.max(0,Number(seconds)||0);
    if(seconds<10) return 'now';
    if(seconds<60) return Math.floor(seconds)+'s ago';
    if(seconds<3600) return Math.floor(seconds/60)+'m ago';
    return Math.floor(seconds/3600)+'h ago';
  }}

  function duration(seconds){{
    seconds=Math.max(0,Number(seconds)||0);
    if(seconds<60) return Math.max(1,Math.floor(seconds))+'s';
    if(seconds<3600) return Math.floor(seconds/60)+'m';
    const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60);
    return h+'h'+(m?' '+m+'m':'');
  }}

  function el(tag,cls,text){{
    const node=document.createElement(tag);
    if(cls) node.className=cls;
    if(text!==undefined && text!==null) node.textContent=text;
    return node;
  }}

  async function doRevoke(session,button){{
    if(!window.confirm('Revoke support access for '+session.support_name+' now? Their active support session will be blocked immediately.')) return;
    button.disabled=true;
    const old=button.textContent;
    button.textContent='Revoking…';
    try{{
      const body=new URLSearchParams();
      body.set('token',session.token);
      const r=await fetch(revokeUrl,{{
        method:'POST',
        credentials:'same-origin',
        cache:'no-store',
        headers:{{
          'X-CSRFToken':csrf,
          'X-Requested-With':'XMLHttpRequest',
          'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'
        }},
        body:body.toString()
      }});
      const data=await r.json().catch(()=>({{}}));
      if(!r.ok || !data.ok) throw new Error(data.error||'Could not revoke support access.');
      await refresh();
    }}catch(err){{
      window.alert(err.message||'Could not revoke support access.');
      button.disabled=false;
      button.textContent=old;
    }}
  }}

  function render(sessions){{
    rows.replaceChildren();
    if(!sessions.length){{
      box.hidden=true;
      box.classList.remove('has-live');
      return;
    }}

    box.hidden=false;
    const liveCount=sessions.filter(s=>s.live).length;
    box.classList.toggle('has-live',liveCount>0);
    title.textContent=liveCount
      ? (liveCount===1 ? 'TapTap Support is currently in your portal' : 'TapTap Support users are currently in your portal')
      : 'TapTap Support access is approved';
    count.textContent=String(sessions.length);

    sessions.forEach(session=>{{
      const row=el('div','tt-sp-row');
      const initial=(session.support_name||'S').trim().charAt(0).toUpperCase()||'S';
      row.appendChild(el('div','tt-sp-avatar',initial));

      const info=el('div','tt-sp-info');
      const line=el('div','tt-sp-line');
      line.appendChild(el('span','tt-sp-name',session.support_name||'TapTap Support'));

      const status=el('span','tt-sp-status'+(session.live?' live':''),
        session.live ? 'ON YOUR PORTAL' : 'ACCESS APPROVED');
      line.appendChild(status);
      info.appendChild(line);

      let main='';
      if(session.live){{
        main='Page: '+(session.page_name||'Portal');
        if(session.page_path) main+=' · '+session.page_path;
        main+=' · active '+age(session.last_seen_seconds);
        if(session.page_seconds!==null && session.page_seconds!==undefined) main+=' · on this page '+duration(session.page_seconds);
      }}else{{
        main='Not currently active in the portal';
      }}
      const meta=el('div','tt-sp-meta',main);
      info.appendChild(meta);

      const exp=el('div','tt-sp-meta',
        'Support approval expires in '+duration(session.expires_seconds)
        +(session.reason ? ' · Reason: '+session.reason : ''));
      info.appendChild(exp);
      row.appendChild(info);

      const button=el('button','btn btn-sm btn-danger tt-sp-revoke','Revoke access');
      button.type='button';
      button.addEventListener('click',()=>doRevoke(session,button));
      row.appendChild(button);
      rows.appendChild(row);
    }});
  }}

  async function refresh(){{
    try{{
      const r=await fetch(liveUrl,{{credentials:'same-origin',cache:'no-store',headers:{{'X-Requested-With':'XMLHttpRequest'}}}});
      if(!r.ok) return;
      const data=await r.json();
      if(data.ok) render(data.sessions||[]);
    }}catch(e){{}}
  }}

  refresh();
  timer=window.setInterval(refresh,{OWNER_POLL_SECONDS * 1000});
  window.addEventListener('beforeunload',()=>{{if(timer) clearInterval(timer);}});
}})();
</script>
"""


def _inject_after_main(html, block):
    marker = '<main class="main-content">'
    if marker in html:
        return html.replace(marker, marker + block, 1), True
    # Fallback for pages whose main tag has additional classes/attributes.
    match = re.search(r"<main\\b[^>]*class=[\"'][^\"']*\\bmain-content\\b[^\"']*[\"'][^>]*>", html, re.I)
    if match:
        pos = match.end()
        return html[:pos] + block + html[pos:], True
    return html, False


def _inject_response(request, response):
    if (
        getattr(response, "streaming", False)
        or response.status_code != 200
        or "text/html" not in response.get("Content-Type", "").lower()
        or not getattr(request.user, "is_authenticated", False)
    ):
        return response

    try:
        html = response.content.decode(response.charset or "utf-8")
    except Exception:
        return response

    # Support side: keep current-page presence alive. Only a valid support-view
    # session receives the script.
    if getattr(request, "tt_view_as", None) and request.user.is_superuser:
        if "tt-support-presence-heartbeat" not in html:
            script = _support_heartbeat_script(request)
            if "</body>" in html:
                html = html.replace("</body>", script + "</body>", 1)
            else:
                html += script

    # Owner side: inject a live banner into every normal portal page. It is
    # initially hidden and appears only when an approved support grant exists.
    business = _business(request)
    if _is_actual_owner(request, business) and "ttSupportPresence" not in html:
        block = _owner_banner(request)
        html, inserted = _inject_after_main(html, block)
        if not inserted and "</body>" in html:
            html = html.replace("</body>", block + "</body>", 1)

    output = html.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _middleware_call(original):
    @wraps(original)
    def wrapped(self, request):
        response = original(self, request)
        try:
            # Only real HTML page navigations change the displayed page.
            # Heartbeat/API/AJAX calls do not overwrite "Finance" with
            # "Support Presence Heartbeat".
            if (
                getattr(request, "tt_view_as", None)
                and request.user.is_superuser
                and request.method == "GET"
                and response.status_code < 400
                and "text/html" in response.get("Content-Type", "").lower()
            ):
                _record_support_navigation(request)

            response = _inject_response(request, response)
        except Exception:
            logger.exception("Support presence middleware failed safely")
        return response

    wrapped._taptap_support_presence = True
    return wrapped


def install():
    """Install URL endpoints and the live banner/heartbeat middleware wrapper."""
    global _INSTALLED
    if _INSTALLED:
        return

    from . import urls
    from .team import TeamAccessMiddleware

    names = {getattr(item, "name", None) for item in urls.urlpatterns}
    additions = [
        (
            "support_presence_live",
            "api/support-presence/live/",
            live_state,
        ),
        (
            "support_presence_heartbeat",
            "api/support-presence/heartbeat/",
            heartbeat,
        ),
        (
            "support_presence_revoke",
            "api/support-presence/revoke/",
            revoke,
        ),
    ]
    for name, route, view in additions:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))

    if not getattr(TeamAccessMiddleware.__call__, "_taptap_support_presence", False):
        TeamAccessMiddleware.__call__ = _middleware_call(TeamAccessMiddleware.__call__)

    _INSTALLED = True
    logger.info(
        "Live support presence enabled: owner banner, page navigation, heartbeat and revoke."
    )
