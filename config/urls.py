from django.contrib import admin
from django.urls import include, path

from core.admin_otp import admin_otp
from core import views_manage, views_team
from core.views_archive import voucher_archive_settings
from core.views_speed import speed_control, speed_rule_save, speed_rule_action


urlpatterns = [
    path("admin/otp/", admin_otp, name="admin_otp"),
    path("admin/", admin.site.urls),
    path("api/tunnel/v1/", include("core.tunnel_urls")),

    # Multi-business account switcher.
    path(
        "account/business/<int:business_id>/switch/",
        views_team.business_switch,
        name="business_switch",
    ),

    # Expired voucher retention / archive settings.
    path(
        "settings/voucher-archive/",
        voucher_archive_settings,
        name="voucher_archive_settings",
    ),

    # Dedicated Traffic speed-control page.
    path(
        "traffic/speed/",
        speed_control,
        name="traffic_speed",
    ),
    path(
        "traffic/speed/save/",
        speed_rule_save,
        name="traffic_speed_save",
    ),
    path(
        "traffic/speed/<int:pk>/action/",
        speed_rule_action,
        name="traffic_speed_action",
    ),

    # Dedicated full editing screens; existing member and plan pages are unchanged.
    path("manage/", views_manage.catalog_manage, name="catalog_manage"),
    path(
        "manage/members/<int:pk>/edit/",
        views_manage.catalog_member_edit,
        name="catalog_member_edit",
    ),
    path(
        "manage/plans/<int:pk>/edit/",
        views_manage.catalog_plan_edit,
        name="catalog_plan_edit",
    ),
    path("", include("core.urls")),
]
