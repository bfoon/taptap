from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/tunnel/v1/", include("core.tunnel_urls")),
    path("", include("core.urls")),
]
