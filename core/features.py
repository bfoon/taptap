"""Feature modules — switch parts of TapTap on or off per business.

Every page in TapTap belongs to a module below (by URL name, or by path prefix for pages that
add-on modules register at start-up). Pages not listed belong to the core, which is always on:
vouchers, batches, plans, routers, active users, settings, subscription, support, chat.

Who decides
-----------
1. The business's PLAN (tier) gives a bundle of modules: Free (vouchers only), Basic (everything but team
   accounts and the AI assistant), Pro (everything) or Enterprise (everything, plus features built for it).
   A paid plan or trial that ends falls back to Free — vouchers keep working, the rest locks (effective_tier).
2. The PLATFORM (superusers, Platform console → business → Features) can switch any module on
   or off for one business on top of its tier: Business.feature_overrides = {key: True/False}.
3. The OWNER (Settings → Features) can switch OFF optional modules they don't use, to keep
   the menu short. They cannot switch on what their package doesn't include.
   Business.features_off = [key, …]

A module is on when its tier (or a platform switch) gives it, the owner hasn't hidden it,
and every module it depends on is on too.

What "off" means
----------------
* Its pages refuse to open (FeatureGateMiddleware): a friendly page explains why and who can
  turn it on. JSON calls get 403.
* Its menu links disappear — also links that add-on modules inject into the menu later.
* Fit and background code can ask enabled(business, key).
Router enforcement is never switched off here: vouchers, members and their time keep working
exactly as before (members with time left are never disconnected because a page is hidden).
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache, wraps
import logging
import re

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import Resolver404, resolve

logger = logging.getLogger('taptap.features')


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    group: str
    icon: str
    description: str
    urls: frozenset = field(default_factory=frozenset)
    paths: tuple = ()
    depends: tuple = ()
    owner_can_hide: bool = True


def _f(key, label, group, icon, description, urls='', paths=(), depends=(), owner_can_hide=True):
    return Feature(key, label, group, icon, description, frozenset(urls.split()), tuple(paths), tuple(depends), owner_can_hide)


GROUPS = OrderedDict([
    ('sales', ('Sales & customers', 'bi-ticket-perforated')),
    ('money', ('Money & team', 'bi-cash-stack')),
    ('network', ('Network', 'bi-router')),
    ('security', ('Security', 'bi-shield-check')),
    ('studio', ('Studio & marketing', 'bi-palette')),
    ('growth', ('Growth & partners', 'bi-graph-up-arrow')),
    ('ai', ('AI technician', 'bi-robot')),
])

FEATURES = OrderedDict((f.key, f) for f in [
    # ── sales & customers ──
    _f('daily_sales', 'Daily sales', 'sales', 'bi-calendar-day', 'Day-by-day sales with plan and seller detail.', 'sales_daily'),
    _f('members', 'Members', 'sales', 'bi-person-badge', 'Username + password customers with renewals, arrears and a member portal.',
       'members member_detail member_support member_password member_renew member_push member_fix_all', paths=('/members/',)),
    _f('agents', 'Agents & resellers', 'sales', 'bi-shop', 'Agents who sell vouchers on commission, their statements, voucher checker and cash collection.',
       'agent_detail agent_portal_rotate agent_order_plans agent_log_action agent_portal_phone agent_statement batch_assign finance_agent_save finance_collection_add',
       paths=('/finance/agents/',)),
    _f('customer_reports', 'Customer reports', 'sales', 'bi-chat-square-text', 'Problems customers send from the portal ("my voucher does not work").', 'voucher_reports'),
    _f('missing_vouchers', 'Missing vouchers', 'sales', 'bi-question-diamond', 'Report and resolve lost or stolen vouchers.',
       'missing_vouchers report_missing_vouchers report_missing_batch resolve_missing_report mark_voucher_missing'),
    _f('bonanza', 'Bonanza', 'sales', 'bi-stars', 'Spin-the-wheel prize campaigns for customers.',
       'bonanza_list bonanza_new bonanza_edit bonanza_status bonanza_spins bonanza_payout'),
    _f('tracking', 'Watch list', 'sales', 'bi-binoculars', 'Follow vouchers, devices and routers and get told when they change.', 'tracking watch_save tracking_action'),
    # ── money & team ──
    _f('reports', 'Reports', 'money', 'bi-bar-chart', 'Voucher and sales reports with charts and exports.', 'reports reports_data reports_export'),
    _f('finance', 'Finance', 'money', 'bi-cash-stack', 'Sales, expenses, profit and loss.',
       'finance finance_sale_add finance_sale_delete finance_expense_add finance_expense_delete finance_expense_repeat finance_settings finance_export finance_book_missing'),
    _f('team', 'Team accounts', 'money', 'bi-people', 'Staff logins with roles and permissions.', 'team team_member_save team_member_action team_performance team_performance_person'),
    _f('payroll', 'Payroll', 'money', 'bi-wallet2', 'Staff salaries, commission deals and payslips.',
       'team_pay_save payroll_pay payroll_pay_all payroll_void payroll_payslip my_pay my_payslip', depends=('team', 'finance')),
    # ── network ──
    _f('ip_bindings', 'IP bindings', 'network', 'bi-shield-lock', 'Devices that skip the login page, with expiry.', 'ip_bindings ip_bindings_data ip_binding_set ip_binding_action'),
    _f('topology', 'Topology & map', 'network', 'bi-diagram-3', 'Network map, field map and other routers in the chain.',
       'topology topology_graph topology_live topology_refresh topology_routers topology_router_find topology_router_action topology_router_probe topology_field topology_field_save topology_geo topology_labels node_devices'),
    _f('traffic', 'Traffic & speed', 'network', 'bi-graph-up-arrow', 'Live traffic, usage per customer and speed rules.',
       'traffic traffic_now traffic_cdn traffic_data traffic_speed traffic_speed_save traffic_speed_action'),
    _f('app_control', 'App & site control', 'network', 'bi-funnel', 'Block or slow apps and sites, with peak-hour rules.', 'app_control app_rule_save app_rule_global app_rule_action', depends=('traffic',)),
    _f('devices', 'Devices', 'network', 'bi-fingerprint', 'Every phone and laptop seen, with sharing checks.', 'devices device_detail device_action'),
    _f('wan', 'Internet lines', 'network', 'bi-hdd-network', 'Multi-WAN designer, the line hub and the Internet Doctor.',
       'wan_designer wan_detect wan_preview wan_apply wan_confirm wan_undo wan_status wan_script wan_doctor_status wan_doctor_check wan_doctor_install wan_doctor_remove wan_doctor_fix wan_doctor_script router_wan_name '
       'line_hub line_hub_pulse line_hub_action line_hub_move'),
    _f('router_studio', 'Router control & Config Studio', 'network', 'bi-grid-1x2', 'Ports, clock, quick recipes and Config Studio: change the router from TapTap.',
       'router_control router_config_apply router_config_refresh router_resource_api router_quick_recipe router_telemetry router_port port_action port_blink '
       'router_interface_role router_clock router_clock_read router_clock_set config_studio config_studio_action config_studio_ai config_studio_backup '
       'config_studio_check config_studio_deploy config_studio_export config_studio_read config_studio_section config_studio_state config_studio_trace'),
    _f('schedules', 'Schedules & sync control', 'network', 'bi-calendar2-check', 'Restart / shutdown schedules, and when and how TapTap syncs each router.',
       'router_schedules router_schedule_save router_schedule_action sync_control sync_control_save sync_control_action sync_control_status'),
    _f('flow', 'Flow lab', 'network', 'bi-water', 'Slow customers, idle devices, DHCP and speed checks, step by step.', paths=('/flow/',)),
    _f('network_tools', 'Network tools', 'network', 'bi-activity', 'Ping, speed tests and path checks from the router.',
       'network_tools network_tools_run network_tools_test network_tools_path network_tools_devices network_tools_verdict network_tools_save_chain network_tools_blob network_tools_sink'),
    _f('other_devices', 'Other routers & APs', 'network', 'bi-hdd-rack', 'TP-Link, Omada and other devices TapTap does not manage, with remote access.',
       'netdev_list netdev_save netdev_action netdev_session remote_proxy_root remote_proxy omada_save omada_action'),
    _f('router_maintenance', 'Router maintenance', 'network', 'bi-tools', 'Backups, restore, clean-up and the control designer.',
       'router_cleanup_scan router_cleanup_clean router_cleanup_job router_cleanup_copy router_cleanup_download router_restore_upload router_restore_prepare router_restore_start router_restore_status router_restore_helper router_backups router_backup_download router_backup_server_download control_designer_state control_designer_refresh control_designer_inspect control_designer_preview control_designer_apply control_designer_rollback'),
    _f('hotspot_profiles', 'Hotspot profiles', 'network', 'bi-sliders', 'Check and fix router user profiles against your plans.', 'router_profiles router_profile_import profile_check_fix'),
    _f('alerts', 'Alerts & email alerts', 'network', 'bi-bell', 'Rules for router, device and business events, by email and in the app.',
       'alerts alert_rule_save alert_rule_action alerts_read event_rule_save event_rule_preset event_rule_action device_alert_toggle notifications notifications_test notifications_resend'),
    # ── security ──
    _f('security', 'Security center', 'security', 'bi-shield-check', 'Router security audit, incidents, sharing checks and sticky vouchers.',
       'security security_sticky security_ack security_rescan security_fix security_plan_limits security_sharing security_admin_account security_protection sharing_save sharing_case sharing_trust_remove incident_fix incident_ignore incident_fix_all shared_resolve shared_settings voucher_entry_policy_save voucher_entry_unblock voucher_entry_unblock_all'),
    _f('fair_usage', 'Fair usage', 'security', 'bi-speedometer2', 'Slow down heavy users step by step.',
       'fup_new fup_edit fup_action fup_lift fup_device fup_slowed fup_manual_steps fup_manual_step_action', paths=('/security/fair-usage/',)),
    # ── studio ──
    _f('portal_studio', 'Portal studio', 'studio', 'bi-window-stack', 'Design and install the customer login pages.',
       'portal_studio portal_editor portal_action portal_export portal_deploy portal_deploy_status portal_qr portal_online_prepare'),
    _f('voucher_designer', 'Voucher designer', 'studio', 'bi-palette', 'Design printed voucher cards.', 'voucher_designs voucher_design_editor voucher_design_action'),
    _f('adverts', 'Adverts', 'studio', 'bi-badge-ad', 'Adverts on the login page with views and clicks.', 'ads ad_save ad_action'),
    # ── growth ──
    _f('online_sales', 'Online sales', 'growth', 'bi-bag-check', 'Sell vouchers online with mobile money.', 'online_buy_admin', paths=('/online-sales/',)),
    _f('market', 'Market — sell batches', 'growth', 'bi-shop-window', 'List batches of vouchers for other people to buy and resell; they keep running on your network.',
       'market_sell market_listing_action', paths=('/market/sell/',)),
    _f('collaboration', 'Collaboration & roaming', 'growth', 'bi-arrow-left-right', 'Share vouchers with partner businesses and let customers roam.',
       'business_collaboration business_collaboration_action business_roaming business_roaming_action', paths=('/collaboration/',)),
    _f('investors', 'Investors', 'growth', 'bi-piggy-bank', 'Investor deals, investor portal and investment opportunities.',
       'investors_admin investors_action investment_admin investment_admin_data', paths=('/investors/',), depends=('finance',)),
    # ── AI ──
    _f('fit', 'Fit (AI technician)', 'ai', 'bi-robot', 'Ask Fit questions, let it act on routers, build dashboards and monitors.',
       'fit_state fit_ask fit_action fit_new fit_settings fit_look fit_tours fit_dashboards_page fit_dashboard fit_dashboard_data fit_chart_data fit_monitors_page fit_monitor_toggle fit_missions_page fit_missions_data fit_mission_op fit_workspace fit_workspace_data fit_table fit_table_data',
       paths=('/fit/',)),
])
ALL = frozenset(FEATURES)

# Plans, cheapest first. Pro is everything and the default, so existing accounts see no change.
#   (key, (name, one line, modules, extras shown on the plan card))
TIERS = OrderedDict([
    ('free', ('Free', 'Vouchers only: make, print, sell and manage vouchers on your routers.', frozenset(),
              ('Vouchers, batches and plans', 'Routers over TapTap Link', 'Minimal print design', 'Active users'))),
    ('basic', ('Basic', 'Every tool for one business, without team accounts and the AI assistant.', ALL - {'team', 'payroll', 'fit'},
               ('Everything in Free', 'Reports, finance and members', 'Network, security and studio tools', 'Online sales and partners'))),
    ('pro', ('Pro', 'Everything TapTap has, with team accounts and Fit, the AI assistant.', ALL,
             ('Everything in Basic', 'Team accounts and payroll', 'Fit — the AI technician', 'Every new feature first'))),
    ('enterprise', ('Enterprise', 'Everything in Pro, plus features tailor-made for your business.', ALL,
                    ('Everything in Pro', 'Features built just for you', 'Your requests tracked step by step', 'A TapTap contact for your business'))),
])
PAID_TIERS = ('basic', 'pro')               # bought on the Subscription page; Enterprise is agreed with TapTap
DEFAULT_TIER = 'pro'
# Package names before plans existed: Starter was a subset of Basic, Business was everything.
LEGACY_TIERS = {'starter': 'basic', 'business': 'pro'}

# Prefixes the gate never looks at (public pages, APIs, static) — same idea as the team gate.
SKIP_PREFIXES = ('/admin/', '/static/', '/api/', '/p/', '/b/', '/n/off/', '/n/me/', '/media/', '/ag/', '/c/', '/member/')
# Routers pages that belong to router_maintenance only when the path says so.
_ROUTER_MAINT = re.compile(r'^/routers/\d+/(cleanup|restore|backups)/')


# ───────────────────────────── deciding ─────────────────────────────
def tier_of(business):
    """The plan the business is on (or paying for)."""
    t = getattr(business, 'feature_tier', '') or DEFAULT_TIER
    t = LEGACY_TIERS.get(t, t)
    return t if t in TIERS else DEFAULT_TIER


def free_fallback():
    """Does a business whose paid time or trial ended keep the Free plan (instead of being locked out)?"""
    from django.conf import settings
    return bool(getattr(settings, 'FREE_PLAN_FALLBACK', True))


def paid_time_left(business):
    """True while the trial or a paid subscription runs (or the account is unlimited)."""
    if getattr(business, 'is_unlimited', False):
        return True
    from django.utils import timezone
    exp = business.access_expires_at() if hasattr(business, 'access_expires_at') else None
    return bool(exp and exp > timezone.now())


def effective_tier(business):
    """The plan in force right now: the chosen plan while its time runs, Free once it has ended."""
    tier = tier_of(business)
    if tier == 'free' or paid_time_left(business):
        return tier
    return 'free' if free_fallback() else tier


def cheapest_tier_with(key):
    """The first plan (cheapest) that includes module ``key`` — for "Unlock with Basic" buttons."""
    for t, row in TIERS.items():
        if key in row[2]:
            return t
    return 'pro'


def available(business):
    """Modules this business may use: plan bundle + platform switches (before the owner's choices)."""
    keys = set(TIERS[effective_tier(business)][2])
    for key, on in (getattr(business, 'feature_overrides', None) or {}).items():
        if key in FEATURES:
            (keys.add if on else keys.discard)(key)
    return keys


def _close_dependencies(keys):
    keys = set(keys)
    changed = True
    while changed:
        changed = False
        for key in list(keys):
            if any(dep not in keys for dep in FEATURES[key].depends):
                keys.discard(key)
                changed = True
    return keys


def enabled_set(business):
    """Modules switched on for this business right now."""
    if business is None:
        return set(ALL)
    keys = available(business)
    hidden = set(getattr(business, 'features_off', None) or [])
    keys -= {k for k in hidden if k in FEATURES and FEATURES[k].owner_can_hide}
    return _close_dependencies(keys)


def enabled(business, key):
    """True when module `key` is on for `business`. Unknown keys count as core (always on)."""
    if key not in FEATURES:
        return True
    return key in enabled_set(business)


def uses(business, key):
    """Background work of module ``key`` may run for this business: it has access and its plan includes the module
    (a business that fell back to Free keeps its vouchers, but not Fit missions, sharing checks or app rules)."""
    return bool(business is not None and business.has_access and enabled(business, key))


def why_off(business, key):
    """'package' | 'platform' | 'owner' | 'depends:<key>' | '' (= it is on)."""
    if key not in FEATURES or enabled(business, key):
        return ''
    override = (getattr(business, 'feature_overrides', None) or {}).get(key)
    if override is False:
        return 'platform'
    if key not in available(business):
        return 'package'
    if key in (getattr(business, 'features_off', None) or []):
        return 'owner'
    for dep in FEATURES[key].depends:
        if not enabled(business, dep):
            return f'depends:{dep}'
    return 'package'


def state_rows(business):
    """For the Settings → Features and platform pages: every module with its state."""
    on = enabled_set(business)
    avail = available(business)
    tier_keys = TIERS[effective_tier(business)][2]
    overrides = getattr(business, 'feature_overrides', None) or {}
    hidden = set(getattr(business, 'features_off', None) or [])
    groups = OrderedDict((g, {'key': g, 'label': lbl, 'icon': icon, 'items': []}) for g, (lbl, icon) in GROUPS.items())
    for f in FEATURES.values():
        groups[f.group]['items'].append({
            'f': f, 'on': f.key in on, 'available': f.key in avail, 'in_tier': f.key in tier_keys,
            'override': overrides.get(f.key), 'hidden': f.key in hidden, 'why': why_off(business, f.key),
            'depends': [FEATURES[d].label for d in f.depends],
        })
    return [g for g in groups.values() if g['items']]


# ───────────────────────────── pages ─────────────────────────────
@lru_cache(maxsize=512)
def _feature_for_name(url_name):
    for f in FEATURES.values():
        if url_name in f.urls:
            return f.key
    return None


def feature_for_path(path):
    """Module key a path belongs to, or None for core pages."""
    if _ROUTER_MAINT.match(path):
        return 'router_maintenance'
    try:
        name = resolve(path).url_name
    except Resolver404:
        name = None
    key = _feature_for_name(name) if name else None
    if key:
        return key
    for f in FEATURES.values():
        for prefix in f.paths:
            if path.startswith(prefix):
                return f.key
    return None


def _wants_json(request):
    accept = request.headers.get('Accept', '')
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest' or (accept and 'text/html' not in accept)


def request_features(request):
    """Enabled module keys for the request's business (computed once per request)."""
    cached = getattr(request, 'tt_features', None)
    if cached is not None:
        return cached
    business = getattr(request, 'tt_business', None)
    keys = frozenset(enabled_set(business)) if business is not None else frozenset(ALL)
    request.tt_features = keys
    return keys


class FeatureGateMiddleware:
    """Refuses pages of switched-off modules. Runs right after TeamAccessMiddleware (needs tt_business)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        business = getattr(request, 'tt_business', None)
        if business is not None and request.user.is_authenticated and not any(request.path.startswith(p) for p in SKIP_PREFIXES):
            key = feature_for_path(request.path_info)
            if key and key not in request_features(request):
                return self.refuse(request, business, key)
        return self.get_response(request)

    @staticmethod
    def refuse(request, business, key):
        f = FEATURES[key]
        why = why_off(business, key)
        if _wants_json(request):
            return JsonResponse({'ok': False, 'error': f'{f.label} is switched off for this business.', 'feature': key, 'why': why}, status=403)
        dep = FEATURES.get(why.split(':', 1)[1]) if why.startswith('depends:') else None
        unlock = cheapest_tier_with(key)
        return render(request, 'core/feature_off.html', {'feature': f, 'why': why, 'dep': dep,
                                                         'tier': TIERS[effective_tier(business)][0], 'tier_key': effective_tier(business),
                                                         'unlock': unlock, 'unlock_name': TIERS[unlock][0],
                                                         'unlock_extras': TIERS[unlock][3]}, status=403)


# ───────────────────────────── menus ─────────────────────────────
_NAV_REGIONS = (
    re.compile(r'<aside\b[^>]*class="[^"]*\bsidebar\b[^"]*"[^>]*>.*?</aside>', re.I | re.S),
    re.compile(r'<div\b[^>]*class="[^"]*\bmobile-nav\b[^"]*"[^>]*>.*?</div>', re.I | re.S),
)
_LINK = re.compile(r'<a\b[^>]*\bhref=["\'](/[^"\'#?]*)[^"\']*["\'][^>]*>.*?</a>', re.I | re.S)
_EMPTY_LABEL = re.compile(r'<div\b[^>]*class=["\'][^"\']*nav-label[^"\']*["\'][^>]*>[^<]*</div>\s*(?=<div\b[^>]*class=["\'][^"\']*nav-label|</nav>)', re.I | re.S)
_FIT_WIDGET = re.compile(r'<link\b[^>]*css/fit\.css[^>]*>\s*<div id="ttFit"[^>]*></div>\s*<script\b[^>]*js/fit\.js[^>]*></script>', re.I | re.S)


_A_OPEN = re.compile(r'^<a\b', re.I)
_A_CLOSE = re.compile(r'</a>$', re.I)


def _locked_link(html, key):
    """A menu link the plan does not include: kept, dimmed, with a lock — it opens the "Unlock with …" page."""
    name = TIERS[cheapest_tier_with(key)][0]
    html = _A_OPEN.sub(f'<a data-tt-locked="1" title="{FEATURES[key].label}: unlock with {name}"', html, count=1)
    return _A_CLOSE.sub('<i class="bi bi-lock-fill tt-lock" aria-label="locked"></i></a>', html, count=1)


def strip_menus(body, enabled_keys, locked_keys=()):
    """Remove menu links to switched-off modules from the sidebar and the mobile menu
    (including links added by other modules), empty section labels, and the Fit widget.
    Modules in ``locked_keys`` (not in the business's plan) keep their link, dimmed with a lock, so the owner sees
    what a plan would unlock; modules the owner or TapTap switched off disappear."""
    off = ALL - set(enabled_keys)
    if not off:
        return body
    locked = set(locked_keys) & off

    def drop(match):
        key = feature_for_path(match.group(1))
        if key in locked:
            return _locked_link(match.group(0), key)
        return '' if key in off else match.group(0)

    for rx in _NAV_REGIONS:
        m = rx.search(body)
        if m:
            region = _LINK.sub(drop, m.group(0))
            region = _EMPTY_LABEL.sub('', region)
            body = body[:m.start()] + region + body[m.end():]
    if 'fit' in off:
        body = _FIT_WIDGET.sub('', body)
    return body


def _clean_response(request, response):
    try:
        if not getattr(request.user, 'is_authenticated', False) or getattr(request, 'tt_business', None) is None:
            return response
        if getattr(response, 'streaming', False) or 'text/html' not in response.get('Content-Type', '').lower():
            return response
        keys = request_features(request)
        if keys == ALL:
            return response
        business = request.tt_business
        locked = {k for k in ALL - set(keys) if why_off(business, k) == 'package'}
        charset = response.charset or 'utf-8'
        body = response.content.decode(charset)
        new = strip_menus(body, keys, locked)
        if new != body:
            response.content = new.encode(charset)
            if response.has_header('Content-Length'):
                response['Content-Length'] = str(len(response.content))
    except Exception:
        logger.exception('Could not hide switched-off features from the menu')
    return response


def _install_permissions():
    from . import permissions
    permissions.URL_PERMS.setdefault('business_features', 'settings.manage')


def _install_urls():
    from django.contrib.auth.decorators import login_required
    from django.urls import path
    from . import urls
    from .views_features import business_features
    if not any(getattr(p, 'name', None) == 'business_features' for p in urls.urlpatterns):
        urls.urlpatterns.insert(0, path('settings/features/', login_required(business_features), name='business_features'))


def install():
    """Called last from CoreConfig.ready(): wraps TeamAccessMiddleware outermost, so menu links that
    other add-ons inject are already in the page when switched-off ones are removed."""
    from .team import TeamAccessMiddleware
    _install_permissions()
    _install_urls()
    original = TeamAccessMiddleware.__call__
    if getattr(original, '_taptap_features', False):
        return

    @wraps(original)
    def wrapped(self, request):
        return _clean_response(request, original(self, request))

    wrapped._taptap_features = True
    TeamAccessMiddleware.__call__ = wrapped
    logger.info('Feature modules enabled: %d modules, tiers %s', len(FEATURES), ', '.join(TIERS))
