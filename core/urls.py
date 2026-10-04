from django.urls import path
from . import views, views_business as vb, views_studio as vs, views_wan as vw, views_agents as va, views_auth

from . import views_help as vhelp
from . import views_ads, views_ports, views_live, views_traffic, views_missing, views_link
from . import views_team as vteam, views_platform as vp, views_detail as vdetail, views_bin as vbin, views_freeze as vfz
from . import views_fup as vfup
from . import views_bonanza as vbz
from . import views_netdev as vnd
from . import views_sharing as vshare
from . import views_tracking as vtrack
from . import views_agent_portal as vap
from . import views_go as vgo
from . import views_apps as vapps
from . import views_profiles as vprof
from . import views_chat as vchat
from . import views_members as vmem
from . import views_topology as vtopo
from . import views_security_fixes as vsecfix

urlpatterns = [
    path('network/devices/',vnd.netdev_list,name='netdev_list'), path('network/devices/save/',vnd.netdev_save,name='netdev_save'),
    path('network/devices/<int:pk>/',vnd.netdev_action,name='netdev_action'), path('network/remote/<str:token>/',vnd.netdev_session,name='netdev_session'),
    path('remote/<str:token>/',vnd.remote_proxy,name='remote_proxy_root'), path('remote/<str:token>/<path:path>',vnd.remote_proxy,name='remote_proxy'),
    path('network/omada/',vnd.omada_save,name='omada_save'), path('network/omada/do/',vnd.omada_action,name='omada_action'),
    path('tracking/',vtrack.tracking_list,name='tracking'), path('tracking/save/',vtrack.watch_save,name='watch_save'), path('tracking/<int:pk>/',vtrack.tracking_action,name='tracking_action'),
    path('ag/<str:token>/',vap.agent_portal,name='agent_portal'), path('ag/<str:token>/help/',vap.agent_portal_help,name='agent_portal_help'), path('ag/<str:token>/order/',vap.agent_portal_order,name='agent_portal_order'),
    path('go/voucher/<str:code>/',vgo.go_voucher,name='go_voucher'), path('go/device/<str:mac>/',vgo.go_device,name='go_device'),
    path('traffic/apps/',vapps.app_control,name='app_control'), path('traffic/apps/save/',vapps.app_rule_save,name='app_rule_save'),
    path('traffic/apps/do/',vapps.app_rule_action,name='app_rule_global'), path('traffic/apps/<int:pk>/',vapps.app_rule_action,name='app_rule_action'),
    path('routers/profiles/',vprof.router_profiles,name='router_profiles'), path('routers/profiles/check/',vprof.profile_check_fix,name='profile_check_fix'), path('routers/<int:pk>/profiles/import/',vprof.router_profile_import,name='router_profile_import'),
    path('chat/',vchat.chat_page,name='chat_page'), path('chat/state/',vchat.chat_state,name='chat_state'),
    path('chat/t/<int:pk>/',vchat.chat_history,name='chat_history'), path('chat/send/',vchat.chat_send,name='chat_send'),
    path('chat/read/',vchat.chat_read,name='chat_read'), path('chat/direct/',vchat.chat_direct,name='chat_direct'),
    path('chat/settings/',vchat.chat_settings,name='chat_settings'), path('chat/support/',vchat.chat_support_action,name='chat_support_action'),
    path('platform/support-team/',vchat.platform_support_team,name='platform_support_team'),
    path('bonanza/',vbz.bonanza_list,name='bonanza_list'), path('bonanza/new/',vbz.bonanza_edit,name='bonanza_new'),
    path('bonanza/<int:pk>/',vbz.bonanza_edit,name='bonanza_edit'), path('bonanza/<int:pk>/status/',vbz.bonanza_status,name='bonanza_status'),
    path('bonanza/<int:pk>/winners/',vbz.bonanza_spins,name='bonanza_spins'), path('bonanza/payout/<int:pk>/',vbz.bonanza_payout,name='bonanza_payout'),
    path('b/<slug:slug>/',vbz.bonanza_public,name='bonanza_public'), path('b/<slug:slug>/check/',vbz.bonanza_check,name='bonanza_check'),
    path('b/<slug:slug>/spin/',vbz.bonanza_spin,name='bonanza_spin'),
    path('',views.home,name='home'), path('register/',views_auth.register,name='register'), path('login/',views_auth.login_view,name='login'), path('verify/',views_auth.verify_code,name='verify_code'), path('verify/resend/',views_auth.resend_code,name='resend_code'), path('verify/cancel/',views_auth.cancel_verification,name='cancel_verification'), path('account/devices/',views_auth.trusted_devices,name='trusted_devices'), path('account/devices/remove/',views_auth.trusted_device_remove,name='trusted_devices_remove_all'), path('account/devices/<int:pk>/remove/',views_auth.trusted_device_remove,name='trusted_device_remove'), path('logout/',views.logout_view,name='logout'),
    path('dashboard/',views.dashboard,name='dashboard'), path('subscription/',views.subscription,name='subscription'), path('subscription/select/<str:code>/',views.subscription_select,name='subscription_select'),
    path('vouchers/',views.vouchers,name='vouchers'), path('vouchers/generate/',views.generate_vouchers,name='generate_vouchers'), path('vouchers/<int:pk>/',views.voucher_detail,name='voucher_detail'), path('vouchers/<int:pk>/disable/',views.disable_voucher,name='disable_voucher'), path('vouchers/<int:pk>/enable/',views.enable_voucher,name='enable_voucher'), path('vouchers/<int:pk>/reset-mac/',views.reset_mac,name='reset_mac'), path('vouchers/delete-expired/',views.delete_expired,name='delete_expired'),
    path('vouchers/<int:pk>/delete/',vbin.voucher_delete,name='voucher_delete'), path('vouchers/<int:pk>/profile/',vbin.voucher_set_profile,name='voucher_set_profile'), path('vouchers/<int:pk>/archive/',vbin.voucher_archive_now,name='voucher_archive_now'), path('vouchers/<int:pk>/change-code/',views.change_voucher_code,name='change_voucher_code'), path('vouchers/<int:pk>/warn/',vfz.voucher_warn,name='voucher_warn'), path('active-users/warn/',vfz.session_warn,name='session_warn'), path('vouchers/<int:pk>/freeze/',vfz.voucher_freeze,name='voucher_freeze'), path('vouchers/freeze/',vfz.vouchers_freeze,name='vouchers_freeze'), path('batches/<int:pk>/freeze/',vfz.batch_freeze,name='batch_freeze'), path('vouchers/delete/',vbin.vouchers_delete,name='vouchers_delete'),
    path('batches/<int:pk>/delete/',vbin.batch_delete,name='batch_delete'), path('bin/',vbin.voucher_bin,name='voucher_bin'),
    path('batches/',views.batches,name='batches'), path('plans/',views.plans,name='plans'), path('plans/<int:pk>/update/',views.plan_update,name='plan_update'), path('plans/<int:pk>/delete/',vbin.plan_delete,name='plan_delete'), path('plans/<int:pk>/move/',vbin.plan_move,name='plan_move'),
    path('routers/',views.routers,name='routers'), path('routers/agent/register/',views_link.router_agent_register,name='router_agent_register'), path('routers/inventory/',views.router_inventory,name='router_inventory'), path('routers/sync-all/',views.routers_sync_all,name='routers_sync_all'), path('routers/sync-status/',views.router_sync_status,name='router_sync_status'),
    path('routers/<int:pk>/sync/',views.router_sync,name='router_sync'), path('routers/<int:pk>/test/',views_link.router_test,name='router_test'), path('routers/<int:pk>/delete/',views.router_delete,name='router_delete'),
    path('routers/<int:pk>/control/',views.router_control,name='router_control'), path('routers/<int:pk>/control/refresh/',views.router_config_refresh,name='router_config_refresh'),
    path('routers/<int:pk>/control/resource/',views.router_resource_api,name='router_resource_api'), path('routers/<int:pk>/control/apply/',views.router_config_apply,name='router_config_apply'),
    path('routers/<int:pk>/control/interface-role/',views.router_interface_role,name='router_interface_role'), path('routers/<int:pk>/control/recipe/',views.router_quick_recipe,name='router_quick_recipe'),
    path('routers/<int:pk>/telemetry/',views.router_telemetry,name='router_telemetry'),
    path('active-users/',views.active_users,name='active_users'), path('active-users/disconnect/',views.disconnect_user,name='disconnect_user'), path('ip-bindings/',views_live.ip_bindings,name='ip_bindings'), path('ip-bindings/data/',views_live.ip_bindings_data,name='ip_bindings_data'), path('ip-bindings/set/',views_live.ip_binding_set,name='ip_binding_set'), path('ip-bindings/action/',views.ip_binding_action,name='ip_binding_action'),
    path('topology/',views.topology,name='topology'), path('topology/graph/',views.topology_graph,name='topology_graph'), path('topology/live/',views.topology_live,name='topology_live'), path('topology/refresh/<int:pk>/',views.topology_refresh,name='topology_refresh'), path('security/',views.security,name='security'), path('security/sticky/',views.security_sticky,name='security_sticky'), path('security/internet-sharing/',vshare.sharing_save,name='sharing_save'), path('security/internet-sharing/<int:pk>/',vshare.sharing_case,name='sharing_case'), path('security/internet-sharing/trust/<int:pk>/remove/',vshare.sharing_trust_remove,name='sharing_trust_remove'), path('security/ack/',views.security_ack,name='security_ack'), path('security/rescan/<int:pk>/',views.security_rescan,name='security_rescan'), path('security/fix/<int:pk>/',views.security_fix,name='security_fix'), path('settings/',views.settings_view,name='settings'), path('support/',views.support,name='support'),
    # Internet lines (multi-WAN designer)
    path('routers/<int:pk>/internet/',vw.wan_designer,name='wan_designer'), path('routers/<int:pk>/internet/detect/',vw.wan_detect,name='wan_detect'),
    path('routers/<int:pk>/internet/preview/',vw.wan_preview,name='wan_preview'), path('routers/<int:pk>/internet/apply/',vw.wan_apply,name='wan_apply'),
    path('routers/<int:pk>/internet/confirm/',vw.wan_confirm,name='wan_confirm'), path('routers/<int:pk>/internet/undo/',vw.wan_undo,name='wan_undo'),
    path('routers/<int:pk>/internet/status/',vw.wan_status,name='wan_status'), path('routers/<int:pk>/internet/script/',vw.wan_script,name='wan_script'),
    # Agents, agent batches and one-off vouchers
    path('vouchers/single/',va.single_voucher,name='single_voucher'), path('vouchers/<int:pk>/card/',va.voucher_card,name='voucher_card'),
    path('batches/<int:pk>/assign/',va.batch_assign,name='batch_assign'), path('finance/agents/<int:pk>/',va.agent_detail,name='agent_detail'), path('finance/agents/<int:pk>/qr/',va.agent_portal_rotate,name='agent_portal_rotate'), path('finance/agents/<int:pk>/log/',va.agent_log_action,name='agent_log_action'), path('finance/agents/<int:pk>/order-plans/',va.agent_order_plans,name='agent_order_plans'), path('finance/agents/<int:pk>/checker-phone/',va.agent_portal_phone,name='agent_portal_phone'),
    # Finance
    path('finance/',vb.finance,name='finance'), path('finance/sale/',vb.finance_sale_add,name='finance_sale_add'), path('finance/sale/<int:pk>/void/',vb.finance_sale_delete,name='finance_sale_delete'),
    path('finance/expense/',vb.finance_expense_add,name='finance_expense_add'), path('finance/expense/<int:pk>/delete/',vb.finance_expense_delete,name='finance_expense_delete'),
    path('finance/expense/repeat/',vb.finance_expense_repeat,name='finance_expense_repeat'), path('finance/agent/',vb.finance_agent_save,name='finance_agent_save'),
    path('finance/collection/',vb.finance_collection_add,name='finance_collection_add'), path('finance/settings/',vb.finance_settings,name='finance_settings'),
    path('finance/export/',vb.finance_export,name='finance_export'),
    # Reports
    path('reports/',vb.reports,name='reports'), path('reports/data/',vb.reports_data,name='reports_data'), path('reports/export/',vb.reports_export,name='reports_export'),
    # Studios
    path('studio/portal/',vs.portal_studio,name='portal_studio'), path('studio/portal/<int:pk>/',vs.portal_editor,name='portal_editor'),
    path('studio/portal/<int:pk>/action/',vs.portal_action,name='portal_action'), path('studio/portal/<int:pk>/export/',vs.portal_export,name='portal_export'),
    path('studio/vouchers/',vs.voucher_designs,name='voucher_designs'), path('studio/vouchers/<int:pk>/',vs.voucher_design_editor,name='voucher_design_editor'),
    path('studio/vouchers/<int:pk>/action/',vs.voucher_design_action,name='voucher_design_action'), path('studio/vouchers/print/',vs.voucher_print,name='voucher_print'),
    # Public customer portal
    path('p/router-files/<str:token>/<str:name>.html',vs.portal_router_file,name='portal_router_file'), path('studio/portal/deploy/',vs.portal_deploy,name='portal_deploy'), path('studio/portal/deploy/status/',vs.portal_deploy_status,name='portal_deploy_status'), path('p/<slug:slug>/',vs.portal_public,name='portal_public'), path('p/<slug:slug>/check/',vs.portal_check,name='portal_check'),
    path('api/business/<int:business_id>/subscription-warning/',views.api_subscription_warning,name='api_subscription_warning'), path('api/voucher/login/',views.api_voucher_login,name='api_voucher_login'),
    path('ads/',views_ads.ads,name='ads'), path('ads/save/',views_ads.ad_save,name='ad_save'), path('ads/<int:pk>/action/',views_ads.ad_action,name='ad_action'),
    path('p/ad/<int:pk>/seen/',views_ads.ad_seen,name='ad_seen'), path('p/ad/<int:pk>/go/',views_ads.ad_go,name='ad_go'),
    path('p/device/<slug:slug>/',views_ads.device_beacon,name='device_beacon'), path('p/<slug:slug>/state/',views_ads.portal_state,name='portal_state'), path('p/<slug:slug>/accept/',views_ads.portal_accept,name='portal_accept'), path('devices/shared/<int:pk>/resolve/',views_ads.shared_resolve,name='shared_resolve'), path('devices/shared/settings/',views_ads.shared_settings,name='shared_settings'),
    path('devices/',views_ads.devices,name='devices'), path('devices/<int:pk>/',views_ads.device_detail,name='device_detail'), path('devices/<int:pk>/action/',views_ads.device_action,name='device_action'),
    path('routers/<int:pk>/port/',views_ports.router_port,name='router_port'), path('routers/<int:pk>/port/action/',views_ports.port_action,name='port_action'), path('routers/<int:pk>/reboot/',views_ports.router_reboot,name='router_reboot'), path('routers/<int:pk>/backups/',views_ports.router_backups,name='router_backups'), path('routers/<int:pk>/backups/<int:bid>/download/',views_ports.router_backup_download,name='router_backup_download'),
    path('live/tick/',views_live.live_tick,name='live_tick'), path('live/now/',views_live.live_now,name='live_now'), path('live/settings/',views_live.live_settings,name='live_settings'),
    path('security/incident/<int:pk>/fix/',views_live.incident_fix,name='incident_fix'), path('security/incident/<int:pk>/ignore/',views_live.incident_ignore,name='incident_ignore'),
    path('security/incidents/fix-all/',views_live.incident_fix_all,name='incident_fix_all'),
    path('finance/book-missing/',views_live.finance_book_missing,name='finance_book_missing'),
    path('traffic/',views_traffic.traffic,name='traffic'), path('traffic/now/',views_traffic.traffic_now,name='traffic_now'), path('traffic/cdn/',views_traffic.traffic_cdn,name='traffic_cdn'), path('traffic/data/',views_traffic.traffic_data,name='traffic_data'),
    path('topology/node-devices/',views_traffic.node_devices,name='node_devices'), path('alerts/device/',views_traffic.device_alert_toggle,name='device_alert_toggle'),
    path('alerts/',views_traffic.alerts,name='alerts'), path('alerts/rule/',views_traffic.alert_rule_save,name='alert_rule_save'),
    path('alerts/rule/<int:pk>/',views_traffic.alert_rule_action,name='alert_rule_action'), path('alerts/read/',views_traffic.alerts_read,name='alerts_read'), path('alerts/business/',views_traffic.event_rule_save,name='event_rule_save'), path('alerts/business/add/',views_traffic.event_rule_action,name='event_rule_preset'), path('alerts/business/<int:pk>/',views_traffic.event_rule_action,name='event_rule_action'),
    path('vouchers/<int:pk>/missing/',views_missing.mark_voucher_missing,name='mark_voucher_missing'),
    path('vouchers/missing/',views_missing.missing_vouchers,name='missing_vouchers'),
    path('vouchers/missing/report/',views_missing.report_missing_vouchers,name='report_missing_vouchers'),
    path('vouchers/missing/<int:pk>/resolve/',views_missing.resolve_missing_report,name='resolve_missing_report'),
    path('batches/<int:pk>/missing/report/',views_missing.report_missing_batch,name='report_missing_batch'),
    path('api/agent/v1/poll',views_link.agent_poll,name='agent_poll'), path('api/agent/v1/ack',views_link.agent_ack,name='agent_ack'),
    path('api/agent/v1/inventory',views_link.agent_inventory,name='agent_inventory'), path('api/agent/v1/probe',views_link.agent_probe,name='agent_probe'), path('api/agent/v1/hello',views_link.agent_hello,name='agent_hello'),
    path('routers/<int:pk>/link/',views_link.router_link,name='router_link'), path('routers/<int:pk>/link/action/',views_link.router_link_action,name='router_link_action'),
    path('routers/<int:pk>/link/status/',views_link.router_link_status,name='router_link_status'),
    path('notifications/',views_link.notifications,name='notifications'), path('notifications/test/',views_link.notifications_test,name='notifications_test'),
    path('n/off/<str:token>/',views_link.notifications_off,name='notifications_off'),
    # Team accounts, daily sales and own password
    path('team/',vteam.team,name='team'), path('team/save/',vteam.team_member_save,name='team_member_save'),
    path('team/<int:pk>/action/',vteam.team_member_action,name='team_member_action'),
    path('sales/today/',vteam.sales_daily,name='sales_daily'), path('account/password/',vteam.account_password,name='account_password'),
    # Platform console (app owner / superusers)
    path('platform/',vp.overview,name='platform_overview'), path('platform/businesses/',vp.businesses,name='platform_businesses'),
    path('platform/businesses/<int:pk>/',vp.business_detail,name='platform_business'),
    path('platform/businesses/<int:pk>/action/',vp.business_action,name='platform_business_action'),
    path('platform/subscriptions/',vp.subscriptions,name='platform_subscriptions'),
    path('platform/subscriptions/<int:pk>/action/',vp.sub_action,name='platform_sub_action'),
    path('platform/audit/',vp.audit_log,name='platform_audit'), path('platform/traffic/',vp.traffic,name='platform_traffic'), path('platform/view-as/stop/',vp.view_as_stop,name='platform_view_as_stop'),
    # Batch and plan details (summary + scrollable voucher lists)
    path('batches/<int:pk>/',vdetail.batch_detail,name='batch_detail'), path('plans/<int:pk>/',vdetail.plan_detail,name='plan_detail'),
    # Fair usage (data speed steps), managed from the Security Center
    path('security/fair-usage/slowed/',vfup.fup_slowed,name='fup_slowed'), path('security/fair-usage/new/',vfup.fup_edit,name='fup_new'), path('security/fair-usage/<int:pk>/',vfup.fup_edit,name='fup_edit'),
    path('security/fair-usage/<int:pk>/action/',vfup.fup_action,name='fup_action'), path('vouchers/<int:pk>/fair-usage/',vfup.fup_lift,name='fup_lift'), path('vouchers/<int:pk>/fair-usage/device/',vfup.fup_device,name='fup_device'),
    # Support › Help Center guides
    path('support/guides/<slug:slug>/',vhelp.support_guide,name='support_guide'),
    # Members (username + password logins), agent cash-flow statements, batch receipts
    path('members/',vmem.members,name='members'), path('members/<int:pk>/password/',vmem.member_password,name='member_password'),
    path('members/<int:pk>/renew/',vmem.member_renew,name='member_renew'), path('members/<int:pk>/push/',vmem.member_push,name='member_push'),
    path('finance/agents/<int:pk>/statement/',va.agent_statement,name='agent_statement'),
    path('batches/<int:pk>/receipt/',va.batch_receipt,name='batch_receipt'),
    # Topology → Detail: TP-Link & other routers TapTap does not manage
    path('topology/routers/',vtopo.topology_routers,name='topology_routers'), path('topology/routers/find/',vtopo.topology_router_find,name='topology_router_find'),
    path('topology/routers/action/',vtopo.topology_router_action,name='topology_router_action'),
    path('topology/routers/probe/',vtopo.topology_router_probe,name='topology_router_probe'),
    # Security: Fix dialogs (plan time for router users, voucher sharing, default admin account)
    path('security/plan-limits/<int:pk>/',vsecfix.security_plan_limits,name='security_plan_limits'),
    path('security/sharing/',vsecfix.security_sharing,name='security_sharing'),
    path('security/admin-account/<int:pk>/',vsecfix.security_admin_account,name='security_admin_account'),
    path('plans/fix-profile/',views.plan_fix_profile,name='plan_fix_profile'),
    path('batches/<int:pk>/health/',vdetail.batch_health_action,name='batch_health_action'),
    path('security/protection/<int:pk>/',vsecfix.security_protection,name='security_protection'),
]
