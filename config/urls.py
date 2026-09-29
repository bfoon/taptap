from django.contrib import admin
from django.urls import include, path

from core.admin_otp import admin_otp


urlpatterns = [
    path("admin/otp/", admin_otp, name="admin_otp"),
    path("admin/", admin.site.urls),
    path("api/tunnel/v1/", include("core.tunnel_urls")),
    path("", include("core.urls")),
]
