from django.contrib import admin
from django.urls import include, path

from core.admin_otp import admin_otp
from core import views_manage


urlpatterns = [
    path("admin/otp/", admin_otp, name="admin_otp"),
    path("admin/", admin.site.urls),
    path("api/tunnel/v1/", include("core.tunnel_urls")),
    # Dedicated full editing screens; existing member and plan pages are unchanged.
    path("manage/", views_manage.catalog_manage, name="catalog_manage"),
    path("manage/members/<int:pk>/edit/", views_manage.catalog_member_edit, name="catalog_member_edit"),
    path("manage/plans/<int:pk>/edit/", views_manage.catalog_plan_edit, name="catalog_plan_edit"),
    path("", include("core.urls")),
]
