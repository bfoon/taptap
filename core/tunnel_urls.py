from django.urls import path

from . import views_tunnel

urlpatterns = [
    path("bootstrap", views_tunnel.tunnel_bootstrap, name="tunnel_bootstrap"),
    path("register", views_tunnel.tunnel_register, name="tunnel_register"),
]
