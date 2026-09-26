from django.urls import path
from . import views

urlpatterns = [
    path('',views.home,name='home'), path('register/',views.register,name='register'), path('login/',views.login_view,name='login'), path('logout/',views.logout_view,name='logout'),
    path('dashboard/',views.dashboard,name='dashboard'), path('subscription/',views.subscription,name='subscription'), path('subscription/select/<str:code>/',views.subscription_select,name='subscription_select'),
    path('vouchers/',views.vouchers,name='vouchers'), path('vouchers/generate/',views.generate_vouchers,name='generate_vouchers'), path('vouchers/<int:pk>/disable/',views.disable_voucher,name='disable_voucher'), path('vouchers/<int:pk>/reset-mac/',views.reset_mac,name='reset_mac'), path('vouchers/delete-expired/',views.delete_expired,name='delete_expired'),
    path('batches/',views.batches,name='batches'), path('plans/',views.plans,name='plans'),
    path('routers/',views.routers,name='routers'), path('routers/inventory/',views.router_inventory,name='router_inventory'), path('routers/sync-all/',views.routers_sync_all,name='routers_sync_all'), path('routers/sync-status/',views.router_sync_status,name='router_sync_status'),
    path('routers/<int:pk>/sync/',views.router_sync,name='router_sync'), path('routers/<int:pk>/test/',views.router_test,name='router_test'), path('routers/<int:pk>/delete/',views.router_delete,name='router_delete'),
    path('routers/<int:pk>/control/',views.router_control,name='router_control'), path('routers/<int:pk>/control/refresh/',views.router_config_refresh,name='router_config_refresh'),
    path('routers/<int:pk>/control/resource/',views.router_resource_api,name='router_resource_api'), path('routers/<int:pk>/control/apply/',views.router_config_apply,name='router_config_apply'),
    path('routers/<int:pk>/control/interface-role/',views.router_interface_role,name='router_interface_role'), path('routers/<int:pk>/control/recipe/',views.router_quick_recipe,name='router_quick_recipe'),
    path('routers/<int:pk>/telemetry/',views.router_telemetry,name='router_telemetry'),
    path('active-users/',views.active_users,name='active_users'), path('active-users/disconnect/',views.disconnect_user,name='disconnect_user'), path('ip-bindings/',views.ip_bindings,name='ip_bindings'), path('ip-bindings/action/',views.ip_binding_action,name='ip_binding_action'),
    path('topology/',views.topology,name='topology'), path('topology/graph/',views.topology_graph,name='topology_graph'), path('topology/live/',views.topology_live,name='topology_live'), path('topology/refresh/<int:pk>/',views.topology_refresh,name='topology_refresh'), path('reports/',views.reports,name='reports'), path('finance/',views.finance,name='finance'), path('security/',views.security,name='security'), path('security/ack/',views.security_ack,name='security_ack'), path('security/rescan/<int:pk>/',views.security_rescan,name='security_rescan'), path('security/fix/<int:pk>/',views.security_fix,name='security_fix'), path('settings/',views.settings_view,name='settings'), path('support/',views.support,name='support'),
    path('api/business/<int:business_id>/subscription-warning/',views.api_subscription_warning,name='api_subscription_warning'), path('api/voucher/login/',views.api_voucher_login,name='api_voucher_login'),
]
