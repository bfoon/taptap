from django.urls import path
from . import views, views_business as vb, views_studio as vs, views_wan as vw, views_agents as va

from . import views_ads, views_ports, views_live, views_traffic, views_missing

urlpatterns = [
    path('',views.home,name='home'), path('register/',views.register,name='register'), path('login/',views.login_view,name='login'), path('logout/',views.logout_view,name='logout'),
    path('dashboard/',views.dashboard,name='dashboard'), path('subscription/',views.subscription,name='subscription'), path('subscription/select/<str:code>/',views.subscription_select,name='subscription_select'),
    path('vouchers/',views.vouchers,name='vouchers'), path('vouchers/generate/',views.generate_vouchers,name='generate_vouchers'), path('vouchers/<int:pk>/disable/',views.disable_voucher,name='disable_voucher'), path('vouchers/<int:pk>/reset-mac/',views.reset_mac,name='reset_mac'), path('vouchers/delete-expired/',views.delete_expired,name='delete_expired'),
    path('batches/',views.batches,name='batches'), path('plans/',views.plans,name='plans'), path('plans/<int:pk>/update/',views.plan_update,name='plan_update'),
    path('routers/',views.routers,name='routers'), path('routers/inventory/',views.router_inventory,name='router_inventory'), path('routers/sync-all/',views.routers_sync_all,name='routers_sync_all'), path('routers/sync-status/',views.router_sync_status,name='router_sync_status'),
    path('routers/<int:pk>/sync/',views.router_sync,name='router_sync'), path('routers/<int:pk>/test/',views.router_test,name='router_test'), path('routers/<int:pk>/delete/',views.router_delete,name='router_delete'),
    path('routers/<int:pk>/control/',views.router_control,name='router_control'), path('routers/<int:pk>/control/refresh/',views.router_config_refresh,name='router_config_refresh'),
    path('routers/<int:pk>/control/resource/',views.router_resource_api,name='router_resource_api'), path('routers/<int:pk>/control/apply/',views.router_config_apply,name='router_config_apply'),
    path('routers/<int:pk>/control/interface-role/',views.router_interface_role,name='router_interface_role'), path('routers/<int:pk>/control/recipe/',views.router_quick_recipe,name='router_quick_recipe'),
    path('routers/<int:pk>/telemetry/',views.router_telemetry,name='router_telemetry'),
    path('active-users/',views.active_users,name='active_users'), path('active-users/disconnect/',views.disconnect_user,name='disconnect_user'), path('ip-bindings/',views_live.ip_bindings,name='ip_bindings'), path('ip-bindings/data/',views_live.ip_bindings_data,name='ip_bindings_data'), path('ip-bindings/set/',views_live.ip_binding_set,name='ip_binding_set'), path('ip-bindings/action/',views.ip_binding_action,name='ip_binding_action'),
    path('topology/',views.topology,name='topology'), path('topology/graph/',views.topology_graph,name='topology_graph'), path('topology/live/',views.topology_live,name='topology_live'), path('topology/refresh/<int:pk>/',views.topology_refresh,name='topology_refresh'), path('security/',views.security,name='security'), path('security/ack/',views.security_ack,name='security_ack'), path('security/rescan/<int:pk>/',views.security_rescan,name='security_rescan'), path('security/fix/<int:pk>/',views.security_fix,name='security_fix'), path('settings/',views.settings_view,name='settings'), path('support/',views.support,name='support'),
    # Internet lines (multi-WAN designer)
    path('routers/<int:pk>/internet/',vw.wan_designer,name='wan_designer'), path('routers/<int:pk>/internet/detect/',vw.wan_detect,name='wan_detect'),
    path('routers/<int:pk>/internet/preview/',vw.wan_preview,name='wan_preview'), path('routers/<int:pk>/internet/apply/',vw.wan_apply,name='wan_apply'),
    path('routers/<int:pk>/internet/confirm/',vw.wan_confirm,name='wan_confirm'), path('routers/<int:pk>/internet/undo/',vw.wan_undo,name='wan_undo'),
    path('routers/<int:pk>/internet/status/',vw.wan_status,name='wan_status'), path('routers/<int:pk>/internet/script/',vw.wan_script,name='wan_script'),
    # Agents, agent batches and one-off vouchers
    path('vouchers/single/',va.single_voucher,name='single_voucher'), path('vouchers/<int:pk>/card/',va.voucher_card,name='voucher_card'),
    path('batches/<int:pk>/assign/',va.batch_assign,name='batch_assign'), path('finance/agents/<int:pk>/',va.agent_detail,name='agent_detail'),
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
    path('p/<slug:slug>/',vs.portal_public,name='portal_public'), path('p/<slug:slug>/check/',vs.portal_check,name='portal_check'),
    path('api/business/<int:business_id>/subscription-warning/',views.api_subscription_warning,name='api_subscription_warning'), path('api/voucher/login/',views.api_voucher_login,name='api_voucher_login'),
    path('ads/',views_ads.ads,name='ads'), path('ads/save/',views_ads.ad_save,name='ad_save'), path('ads/<int:pk>/action/',views_ads.ad_action,name='ad_action'),
    path('p/ad/<int:pk>/seen/',views_ads.ad_seen,name='ad_seen'), path('p/ad/<int:pk>/go/',views_ads.ad_go,name='ad_go'),
    path('p/device/<slug:slug>/',views_ads.device_beacon,name='device_beacon'),
    path('devices/',views_ads.devices,name='devices'), path('devices/<int:pk>/action/',views_ads.device_action,name='device_action'),
    path('routers/<int:pk>/port/',views_ports.router_port,name='router_port'), path('routers/<int:pk>/port/action/',views_ports.port_action,name='port_action'), path('routers/<int:pk>/reboot/',views_ports.router_reboot,name='router_reboot'), path('routers/<int:pk>/backups/',views_ports.router_backups,name='router_backups'), path('routers/<int:pk>/backups/<int:bid>/download/',views_ports.router_backup_download,name='router_backup_download'),
    path('live/tick/',views_live.live_tick,name='live_tick'), path('live/now/',views_live.live_now,name='live_now'), path('live/settings/',views_live.live_settings,name='live_settings'),
    path('security/incident/<int:pk>/fix/',views_live.incident_fix,name='incident_fix'), path('security/incident/<int:pk>/ignore/',views_live.incident_ignore,name='incident_ignore'),
    path('security/incidents/fix-all/',views_live.incident_fix_all,name='incident_fix_all'),
    path('finance/book-missing/',views_live.finance_book_missing,name='finance_book_missing'),
    path('traffic/',views_traffic.traffic,name='traffic'), path('traffic/now/',views_traffic.traffic_now,name='traffic_now'), path('traffic/data/',views_traffic.traffic_data,name='traffic_data'),
    path('topology/node-devices/',views_traffic.node_devices,name='node_devices'), path('alerts/device/',views_traffic.device_alert_toggle,name='device_alert_toggle'),
    path('alerts/',views_traffic.alerts,name='alerts'), path('alerts/rule/',views_traffic.alert_rule_save,name='alert_rule_save'),
    path('alerts/rule/<int:pk>/',views_traffic.alert_rule_action,name='alert_rule_action'), path('alerts/read/',views_traffic.alerts_read,name='alerts_read'),
    path('vouchers/missing/',views_missing.missing_vouchers,name='missing_vouchers'),
    path('vouchers/missing/report/',views_missing.report_missing_vouchers,name='report_missing_vouchers'),
    path('vouchers/missing/<int:pk>/resolve/',views_missing.resolve_missing_report,name='resolve_missing_report'),
    path('batches/<int:pk>/missing/report/',views_missing.report_missing_batch,name='report_missing_batch'),
]
