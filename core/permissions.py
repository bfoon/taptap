"""Team roles and permissions for TapTap business accounts.

Every business has one owner (Business.user) and any number of team members
(TeamMember). Access is permission-based: a role is just a named bundle of
permissions, and every URL in the app is mapped to the permission it needs.
The TeamAccessMiddleware enforces this map on every request, so hiding a menu
item is only cosmetic — the server refuses the page or action either way.

URLs that are not listed below are refused for team members (fail closed), so a
new page must be added to URL_PERMS before staff can use it. Owners and platform
staff viewing as an owner are never restricted.
"""

# ── permissions ─────────────────────────────────────────────────────────────
PERMISSIONS = {
    'dashboard.view': 'Overview dashboard (vouchers, routers, monthly sales)',
    'sales.daily': 'Daily sales summary',
    'vouchers.view': 'See vouchers, batches and voucher details',
    'vouchers.create': 'Generate vouchers, single vouchers, assign batches, print',
    'vouchers.support': 'Voucher support: enable/disable, reset MAC, active users, missing-voucher reports',
    'vouchers.warn': 'Warn a customer / device: pause the internet until they read a message and press "I agree" — Owner and Admin only',
    'vouchers.manage': 'Change voucher codes, delete unused vouchers and batches (to the bin) and expired vouchers',
    'plans.manage': 'Create and edit plans and prices; delete plans whose vouchers were never used (to the bin)',
    'plans.delete_used': 'Delete plans whose vouchers have been used (to the bin) — Owner and Admin only',
    'reports.view': 'Voucher & sales reports',
    'finance.view': 'Full finance: overview, sales, expenses, profit & loss',
    'finance.manage': 'Record/void sales, expenses, agents and finance settings',
    'finance.agents': 'Agent balances (agent report)',
    'agents.view': 'Agent statements',
    'finance.collect': 'Collect cash from agents',
    'network.manage': 'Routers, IP bindings, topology, traffic, security and alerts',
    'studio.manage': 'Portal studio, voucher designer and adverts',
    'settings.manage': 'Business settings and email alerts',
    'team.manage': 'Create and manage team accounts',
    'subscription.manage': 'Pay for / renew the TapTap subscription',
}
ALL_PERMISSIONS = frozenset(PERMISSIONS)
# Permissions that only come with a role, never as an extra on top of one.
NEVER_EXTRA = frozenset({'plans.delete_used', 'vouchers.warn'})

# ── roles ───────────────────────────────────────────────────────────────────
# (label, description, permissions, landing url name)
ROLES = {
    'owner': ('Owner', 'Full rights to everything, including billing and the team.', ALL_PERMISSIONS, 'dashboard'),
    'admin': ('Admin', 'Runs the business day to day and manages non-admin staff. Cannot pay for the subscription.',
              ALL_PERMISSIONS - {'subscription.manage'}, 'dashboard'),
    'finance': ('Finance', 'Voucher reports, agent reports and collecting cash from agents.',
                frozenset({'sales.daily', 'reports.view', 'finance.agents', 'agents.view', 'finance.collect'}), 'reports'),
    'voucher_creator': ('Voucher creator', 'Generates, assigns and prints vouchers.',
                        frozenset({'vouchers.view', 'vouchers.create'}), 'generate_vouchers'),
    'voucher_support': ('Voucher support', 'Helps customers: looks up vouchers, enables/disables them, resets MACs, sees active users.',
                        frozenset({'vouchers.view', 'vouchers.support'}), 'vouchers'),
    'viewer': ('View only', 'Can only see daily sales.', frozenset({'sales.daily'}), 'sales_daily'),
}
STAFF_ROLES = [(k, v[0]) for k, v in ROLES.items() if k != 'owner']

# ── url name → permission (a tuple means "any of these") ────────────────────
_NETWORK = '''routers router_agent_register router_inventory routers_sync_all router_sync_status router_sync router_test
router_delete router_control router_config_refresh router_resource_api router_config_apply router_interface_role
router_quick_recipe router_telemetry ip_bindings ip_bindings_data ip_binding_set ip_binding_action topology topology_graph
topology_live topology_refresh security security_ack security_rescan security_fix wan_designer wan_detect wan_preview
wan_apply wan_confirm wan_undo wan_status wan_script router_port port_action router_reboot router_backups
router_backup_download live_now incident_fix incident_ignore incident_fix_all traffic traffic_now traffic_data
node_devices device_detail device_alert_toggle alerts alert_rule_save alert_rule_action alerts_read router_link router_link_action
router_link_status devices device_action'''.split()
_STUDIO = '''portal_studio portal_editor portal_action portal_export portal_deploy portal_deploy_status voucher_designs
voucher_design_editor voucher_design_action ads ad_save ad_action'''.split()

URL_PERMS = {
    'dashboard': 'dashboard.view',
    'bonanza_list': 'vouchers.view', 'bonanza_spins': 'vouchers.view', 'bonanza_new': 'vouchers.manage', 'bonanza_edit': 'vouchers.manage',
    'bonanza_status': 'vouchers.manage', 'bonanza_payout': 'vouchers.support',
    'sales_daily': ('sales.daily', 'finance.view'),
    'vouchers': 'vouchers.view', 'voucher_detail': 'vouchers.view', 'voucher_card': 'vouchers.view', 'batches': 'vouchers.view', 'batch_detail': 'vouchers.view', 'plan_detail': ('vouchers.view', 'plans.manage'),
    'generate_vouchers': 'vouchers.create', 'single_voucher': 'vouchers.create', 'batch_assign': 'vouchers.create',
    'voucher_print': 'vouchers.create',
    'disable_voucher': 'vouchers.support', 'enable_voucher': 'vouchers.support', 'reset_mac': 'vouchers.support',
    'active_users': ('vouchers.support', 'network.manage'), 'disconnect_user': ('vouchers.support', 'network.manage'),
    'missing_vouchers': ('vouchers.support', 'vouchers.create'), 'report_missing_vouchers': ('vouchers.support', 'vouchers.create'),
    'report_missing_batch': ('vouchers.support', 'vouchers.create'), 'resolve_missing_report': 'vouchers.support',
    'mark_voucher_missing': ('vouchers.support', 'vouchers.create'),
    'delete_expired': 'vouchers.manage',
    'voucher_delete': 'vouchers.manage', 'change_voucher_code': 'vouchers.manage',
    'voucher_freeze': 'vouchers.support', 'voucher_warn': 'vouchers.warn', 'session_warn': 'vouchers.warn', 'vouchers_freeze': 'vouchers.manage', 'batch_freeze': 'vouchers.manage',
    'shared_resolve': 'vouchers.support', 'shared_settings': 'vouchers.manage', 'vouchers_delete': 'vouchers.manage', 'batch_delete': 'vouchers.manage',
    'voucher_bin': 'vouchers.view',
    'fup_new': 'network.manage', 'fup_edit': 'network.manage', 'fup_action': 'network.manage',
    'fup_lift': ('vouchers.support', 'network.manage'), 'fup_slowed': ('vouchers.support', 'network.manage'),
    'plans': 'plans.manage', 'plan_update': 'plans.manage', 'plan_delete': 'plans.manage',
    'reports': 'reports.view', 'reports_data': 'reports.view', 'reports_export': 'reports.view',
    'finance': ('finance.view', 'finance.agents'), 'finance_export': ('finance.view', 'finance.agents'),
    'agent_detail': ('agents.view', 'finance.view'),
    'finance_collection_add': 'finance.collect',
    'finance_sale_add': 'finance.manage', 'finance_sale_delete': 'finance.manage', 'finance_expense_add': 'finance.manage',
    'finance_expense_delete': 'finance.manage', 'finance_expense_repeat': 'finance.manage', 'finance_agent_save': 'finance.manage',
    'finance_book_missing': 'finance.manage', 'finance_settings': 'finance.manage',
    'settings': 'settings.manage', 'notifications': 'settings.manage', 'notifications_test': 'settings.manage',
    'live_settings': 'settings.manage',
    'subscription_select': 'subscription.manage',
    'team': 'team.manage', 'team_member_save': 'team.manage', 'team_member_action': 'team.manage',
    **{n: 'network.manage' for n in _NETWORK},
    **{n: 'studio.manage' for n in _STUDIO},
}

# Pages every signed-in member may open (their own account, help, read-only subscription status,
# and the live heartbeat that keeps router data and automatic sales recording fresh).
MEMBER_ALLOWED = {
    'home', 'login', 'register', 'logout', 'verify_code', 'resend_code', 'cancel_verification',
    'trusted_devices', 'trusted_device_remove', 'trusted_devices_remove_all', 'support', 'subscription',
    'live_tick', 'account_password', 'support_guide',
    # live chat: everyone in a business can chat (the views check who may see which conversation)
    'chat_page', 'chat_state', 'chat_history', 'chat_send', 'chat_read', 'chat_direct', 'chat_settings', 'chat_support_action',
}

# Landing pages tried in order when a member opens a page they can't use (e.g. the dashboard after login).
LANDING_ORDER = ['dashboard', 'sales_daily', 'reports', 'finance', 'vouchers', 'generate_vouchers']


def role_permissions(role, extra=()):
    base = ROLES.get(role, ROLES['viewer'])[2]
    return frozenset(base) | ((frozenset(extra or ()) & ALL_PERMISSIONS) - NEVER_EXTRA)


def allowed(perms, url_name):
    """True/False for a mapped or always-allowed url name; None when the url isn't known."""
    if url_name in MEMBER_ALLOWED:
        return True
    need = URL_PERMS.get(url_name)
    if need is None:
        return None
    if isinstance(need, tuple):
        return any(p in perms for p in need)
    return need in perms


def landing_for(perms, role=None):
    if role in ROLES and allowed(perms, ROLES[role][3]):
        return ROLES[role][3]
    for name in LANDING_ORDER:
        if allowed(perms, name):
            return name
    return 'support'
