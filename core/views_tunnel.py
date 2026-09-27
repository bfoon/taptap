import hmac

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .agent import agent_for_token
from .tunnel import (
    bootstrap_routeros_script,
    ensure_tunnel,
    normalize_wireguard_key,
    routeros_major,
    server_public_key,
    tunnel_enabled,
)


def _client_ip(request):
    return (
        request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", ""))
        .split(",")[0]
        .strip()
    )


def _agent(request):
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    agent = agent_for_token(token)
    if not agent:
        return None, None

    ip = _client_ip(request)
    if agent.pinned_ip and not hmac.compare_digest(agent.pinned_ip, ip):
        return None, None
    return agent, token


def _unauthorized():
    response = HttpResponse("", status=401, content_type="text/plain")
    response["WWW-Authenticate"] = 'Bearer realm="TapTap Tunnel"'
    return response


@csrf_exempt
@require_POST
def tunnel_bootstrap(request):
    """Return the one-time, per-router RouterOS WireGuard/API bootstrap."""
    agent, raw_token = _agent(request)
    if not agent:
        return _unauthorized()

    if not tunnel_enabled():
        return HttpResponse(
            ':log warning "TapTap Tunnel is disabled on the server"',
            content_type="text/plain; charset=utf-8",
        )

    major = routeros_major(agent.ros_version)
    if major and major < 7:
        return HttpResponse(
            ':log warning "TapTap Tunnel requires RouterOS 7; TapTap Link will continue to work"',
            content_type="text/plain; charset=utf-8",
        )

    public_key = server_public_key()
    if not public_key:
        return HttpResponse(
            ':log warning "TapTap Tunnel server is not ready yet; retry the tunnel bootstrap shortly"',
            content_type="text/plain; charset=utf-8",
        )

    tunnel = ensure_tunnel(agent.router)
    body = bootstrap_routeros_script(
        router=agent.router,
        tunnel=tunnel,
        link_token=raw_token,
        server_key=public_key,
    )
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@csrf_exempt
@require_POST
def tunnel_register(request):
    """Receive only the router-generated WireGuard public key."""
    agent, _raw_token = _agent(request)
    if not agent:
        return _unauthorized()

    key = normalize_wireguard_key(request.GET.get("pub", ""))
    if not key:
        return HttpResponse(
            "invalid WireGuard public key", status=400, content_type="text/plain"
        )

    tunnel = ensure_tunnel(agent.router)
    tunnel.router_public_key = key
    tunnel.status = "waiting"
    tunnel.last_error = ""
    tunnel.save(
        update_fields=["router_public_key", "status", "last_error", "updated_at"]
    )
    return HttpResponse("ok", content_type="text/plain")
