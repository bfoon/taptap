import hmac

from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .agent import agent_for_token
from .tunnel import (
    RouterTunnel,
    bootstrap_routeros_script,
    ensure_tunnel,
    normalize_wireguard_key,
    routeros_major,
    server_public_key,
    tunnel_enabled,
)


def _client_ip(request):
    # Same rule as the TapTap Link endpoints (views_link._client_ip), so IP pinning
    # behaves identically on both channels.
    return (
        request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", ""))
        .split(",")[0]
        .strip()
    )


def _agent(request):
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    agent = agent_for_token(token)
    if not agent or agent.revoked:
        return None, None

    ip = _client_ip(request)
    if agent.pinned_ip and not hmac.compare_digest(agent.pinned_ip, ip):
        return None, None
    return agent, token


def _unauthorized():
    response = HttpResponse("", status=401, content_type="text/plain")
    response["WWW-Authenticate"] = 'Bearer realm="TapTap Tunnel"'
    return response


def _script(body):
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@csrf_exempt
@require_POST
def tunnel_bootstrap(request):
    """Return the one-time, per-router RouterOS WireGuard/API bootstrap."""
    agent, raw_token = _agent(request)
    if not agent:
        return _unauthorized()

    if not tunnel_enabled():
        return _script(':log warning "TapTap Tunnel is disabled on the server"')

    major = routeros_major(agent.ros_version)
    if major and major < 7:
        return _script(
            ':log warning "TapTap Tunnel requires RouterOS 7; TapTap Link will continue to work"'
        )

    public_key = server_public_key()
    if not public_key:
        return _script(
            ':log warning "TapTap Tunnel server is not ready yet; retry the tunnel bootstrap shortly"'
        )

    try:
        tunnel = ensure_tunnel(agent.router)
    except RuntimeError as exc:
        return _script(f':log warning "TapTap Tunnel: {str(exc)[:120]}"')

    return _script(
        bootstrap_routeros_script(
            router=agent.router, tunnel=tunnel, link_token=raw_token, server_key=public_key,
        )
    )


@csrf_exempt
@require_POST
def tunnel_register(request):
    """Receive only the router-generated WireGuard public key (and its API port)."""
    agent, _raw_token = _agent(request)
    if not agent:
        return _unauthorized()

    key = normalize_wireguard_key(request.GET.get("pub", ""))
    if not key:
        return HttpResponse("invalid WireGuard public key", status=400, content_type="text/plain")

    try:
        port = int(request.GET.get("port", "") or 8728)
    except ValueError:
        port = 8728
    if not 1 <= port <= 65535:
        port = 8728

    tunnel = ensure_tunnel(agent.router)
    changed = tunnel.router_public_key != key
    RouterTunnel.objects.filter(router_id=tunnel.router_id).update(
        router_public_key=key,
        api_port=port,
        # A new key means a new tunnel: start clean. Same key (repair) keeps state.
        status="waiting" if changed else tunnel.status,
        last_error="" if changed else tunnel.last_error,
        repair_attempts=0 if changed else tunnel.repair_attempts,
        updated_at=timezone.now(),
    )
    return HttpResponse("ok", content_type="text/plain")
