"""
TapTap Tunnel
=============

Primary management transport for RouterOS 7 routers already enrolled through
TapTap Link.

Design:
- Router.connection_mode remains "agent".
- Router.ip_address / username / password stay blank.
- The MikroTik establishes an outbound WireGuard tunnel.
- TapTap connects to RouterOS API through that tunnel.
- TapTap Link remains installed as heartbeat, command and repair fallback.
- RouterOS 6 simply stays on TapTap Link.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import DatabaseError, models
from django.utils import timezone

logger = logging.getLogger("taptap.tunnel")

TUNNEL_TABLE = "core_router_tunnel"
WG_KEY_DIR = Path(os.getenv("TAPTAP_WG_KEY_DIR", "/var/lib/taptap-tunnel"))
WG_PRIVATE_FILE = WG_KEY_DIR / "server.key"
WG_PUBLIC_FILE = WG_KEY_DIR / "server.pub"
_PATCHED = False


class RouterTunnel(models.Model):
    """Runtime model for the transport table created by migration 0017."""

    router_id = models.BigIntegerField(primary_key=True)
    tunnel_ip = models.GenericIPAddressField(unique=True)
    router_public_key = models.CharField(max_length=128, blank=True)
    api_username = models.CharField(max_length=64, default="taptap-tunnel")
    api_password = models.CharField(max_length=160)
    status = models.CharField(max_length=32, default="waiting")
    last_handshake_at = models.DateTimeField(null=True, blank=True)
    last_api_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_repair_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = TUNNEL_TABLE
        app_label = "core"


def _env_bool(name: str, default=False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def tunnel_enabled() -> bool:
    return _env_bool("TAPTAP_TUNNEL_ENABLED", False)


def wg_network():
    return ipaddress.ip_network(
        os.getenv("TAPTAP_WG_NETWORK", "10.77.0.0/16"), strict=False
    )


def wg_server_ip() -> str:
    return os.getenv("TAPTAP_WG_SERVER_IP", "10.77.0.1").strip()


def wg_interface() -> str:
    return os.getenv("TAPTAP_WG_INTERFACE", "taptap-wg").strip()[:15]


def wg_port() -> int:
    return int(os.getenv("TAPTAP_WG_PORT", "51820"))


def wg_endpoint() -> str:
    explicit = os.getenv("TAPTAP_WG_ENDPOINT", "").strip()
    if explicit:
        return explicit
    site = getattr(settings, "SITE_URL", "") or ""
    return urlparse(site).hostname or "" if site else ""


def handshake_timeout() -> int:
    return max(45, int(os.getenv("TAPTAP_WG_HANDSHAKE_TIMEOUT", "90")))


def routeros_major(version: str) -> int:
    match = re.match(r"\s*(\d+)", str(version or ""))
    return int(match.group(1)) if match else 0


def normalize_wireguard_key(value: str) -> str:
    # Query-string decoding turns '+' into a space. Put it back.
    value = str(value or "").strip().replace(" ", "+")
    if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", value):
        return ""
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        return ""
    return value if len(decoded) == 32 else ""


def server_public_key() -> str:
    env_key = normalize_wireguard_key(os.getenv("TAPTAP_WG_PUBLIC_KEY", ""))
    if env_key:
        return env_key
    try:
        return normalize_wireguard_key(WG_PUBLIC_FILE.read_text().strip())
    except Exception:
        return ""


def _allocate_ip(router_id: int) -> str:
    network = wg_network()
    server = ipaddress.ip_address(wg_server_ip())
    used = set(
        RouterTunnel.objects.exclude(tunnel_ip="")
        .values_list("tunnel_ip", flat=True)
    )

    # Stable first choice based on Router PK.
    candidate_int = int(network.network_address) + 10 + int(router_id)
    if candidate_int < int(network.broadcast_address):
        candidate = ipaddress.ip_address(candidate_int)
        if candidate != server and str(candidate) not in used:
            return str(candidate)

    # Collision/very-large-PK fallback.
    for raw in range(
        int(network.network_address) + 10,
        int(network.broadcast_address),
    ):
        candidate = ipaddress.ip_address(raw)
        if candidate == server:
            continue
        if str(candidate) not in used:
            return str(candidate)
    raise RuntimeError(f"No free TapTap Tunnel address remains in {network}")


def ensure_tunnel(router) -> RouterTunnel:
    """Return/create the transport record without touching Router.ip_address."""
    try:
        return RouterTunnel.objects.get(router_id=router.pk)
    except RouterTunnel.DoesNotExist:
        return RouterTunnel.objects.create(
            router_id=router.pk,
            tunnel_ip=_allocate_ip(router.pk),
            api_username="taptap-tunnel",
            api_password=secrets.token_urlsafe(30),
            status="waiting",
            last_error="",
        )


def get_tunnel(router) -> RouterTunnel | None:
    try:
        return RouterTunnel.objects.filter(router_id=router.pk).first()
    except Exception:
        return None


def tunnel_ready(router) -> bool:
    if not tunnel_enabled():
        return False
    tunnel = get_tunnel(router)
    if not tunnel or tunnel.status != "online":
        return False

    now = timezone.now()
    if not tunnel.last_handshake_at or not tunnel.last_api_ok_at:
        return False
    if now - tunnel.last_handshake_at > timedelta(seconds=handshake_timeout() * 2):
        return False
    if now - tunnel.last_api_ok_at > timedelta(
        seconds=max(90, handshake_timeout() * 2)
    ):
        return False
    return True


def mark_tunnel_error(router, exc):
    try:
        RouterTunnel.objects.filter(router_id=router.pk).update(
            status="degraded",
            last_error=str(exc)[:2000],
            updated_at=timezone.now(),
        )
    except Exception:
        pass


def mark_api_ok(router):
    try:
        RouterTunnel.objects.filter(router_id=router.pk).update(
            status="online",
            last_api_ok_at=timezone.now(),
            last_error="",
            updated_at=timezone.now(),
        )
    except Exception:
        pass


def _rs(value) -> str:
    """Quote a value as a RouterOS string literal."""
    value = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("\r", "")
        .replace("\n", "\\n")
    )
    return f'"{value}"'


def _tls_check() -> str:
    site = getattr(settings, "SITE_URL", "")
    if not site.startswith("https://"):
        return "no"
    return "yes-without-crl" if getattr(settings, "AGENT_VERIFY_TLS", True) else "no"


def _register_url() -> str:
    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return site + "/api/tunnel/v1/register?pub="


def bootstrap_routeros_script(router, tunnel, link_token: str, server_key: str) -> str:
    """
    One-time bootstrap returned only to an authenticated TapTap Link router.

    It is launched from the administrator-pasted enrollment/bootstrap command,
    not the permanently restricted taptap-link scheduler. Therefore the Link
    does not need policy/password rights during normal operation.
    """
    endpoint = wg_endpoint()
    if not endpoint:
        return ':log warning "TapTap Tunnel: TAPTAP_WG_ENDPOINT is not configured"'

    prefix = wg_network().prefixlen
    server_ip = wg_server_ip()
    register = _register_url()

    return f'''# --- TapTap Tunnel bootstrap for {router.name} ---
:local ifName "taptap-wg"
:local apiGroup "taptap-tunnel"
:local apiUser {_rs(tunnel.api_username)}
:local apiPass {_rs(tunnel.api_password)}

:if ([:len [/interface wireguard find where name=$ifName]] = 0) do={{
  /interface wireguard add name=$ifName mtu=1420 comment="TapTap secure management"
}}
:local wg [/interface wireguard find where name=$ifName]

/ip address remove [find where interface=$ifName]
/ip address add address={_rs(str(tunnel.tunnel_ip) + "/" + str(prefix))} interface=$ifName comment="TapTap tunnel"

/interface wireguard peers remove [find where interface=$ifName]
/interface wireguard peers add interface=$ifName public-key={_rs(server_key)} endpoint-address={_rs(endpoint)} endpoint-port={wg_port()} allowed-address={_rs(server_ip + "/32")} persistent-keepalive=25s comment="TapTap server"

:if ([:len [/user group find where name=$apiGroup]] = 0) do={{
  /user group add name=$apiGroup policy=read,write,policy,test,api,sensitive,reboot
}} else={{
  /user group set [find where name=$apiGroup] policy=read,write,policy,test,api,sensitive,reboot
}}
:if ([:len [/user find where name=$apiUser]] = 0) do={{
  /user add name=$apiUser group=$apiGroup password=$apiPass comment="TapTap tunnel API"
}} else={{
  /user set [find where name=$apiUser] group=$apiGroup password=$apiPass disabled=no
}}

:local apiSvc [/ip service find where name="api"]
/ip service set $apiSvc disabled=no port=8728 address={_rs(server_ip + "/32")}

/ip firewall filter remove [find where comment="TapTap tunnel API"]
/ip firewall filter add chain=input action=accept in-interface=$ifName src-address={_rs(server_ip)} protocol=tcp dst-port=8728 place-before=0 comment="TapTap tunnel API"

:local pub [/interface wireguard get $wg public-key]
:do {{
  /tool fetch url=({_rs(register)} . $pub) http-method=post http-header-field={_rs("Authorization: Bearer " + link_token)} output=none check-certificate={_tls_check()} duration=10s idle-timeout=8s
}} on-error={{
  :log warning "TapTap Tunnel: configured but public-key registration failed; run the tunnel bootstrap again"
}}
:log info "TapTap Tunnel configured"
'''


def enrollment_bootstrap_trigger(link_token: str) -> str:
    """Append tunnel bootstrap to newly-generated Link enrollment/recovery scripts."""
    if not tunnel_enabled():
        return ""
    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if not site:
        return ""
    url = site + "/api/tunnel/v1/bootstrap"
    return f'''
# --- TapTap Tunnel: primary persistent connection (RouterOS 7) ---
:do {{
  :local ttBoot [/tool fetch url={_rs(url)} http-method=post http-header-field={_rs("Authorization: Bearer " + link_token)} output=user as-value check-certificate={_tls_check()} duration=12s idle-timeout=8s]
  :local ttCode ($ttBoot->"data")
  :if ([:len $ttCode] > 20) do={{ :execute $ttCode }}
}} on-error={{
  :log warning "TapTap Tunnel bootstrap did not start; TapTap Link remains active"
}}
'''


def existing_router_bootstrap_script() -> str:
    """Generic one-time bootstrap for a router that already has TapTap Link."""
    site = (getattr(settings, "SITE_URL", "") or "https://taptapnetwork.com").rstrip("/")
    url = site + "/api/tunnel/v1/bootstrap"
    return f'''# TapTap Tunnel — existing TapTap Link router
:local sid [/system script find where name="taptap-link"]
:if ([:len $sid] = 0) do={{ :error "TapTap Link is not installed" }}
:local src [/system script get $sid source]
:local p [:find $src "ttl_"]
:local q [:find $src "\\\"" $p]
:local tok [:pick $src $p $q]
:if ([:len $tok] < 20) do={{ :error "TapTap Link token was not found" }}
:put "Requesting TapTap Tunnel configuration..."
:local r [/tool fetch url={_rs(url)} http-method=post http-header-field=("Authorization: Bearer " . $tok) output=user as-value check-certificate={_tls_check()} duration=12s idle-timeout=8s]
:local code ($r->"data")
:if ([:len $code] < 20) do={{ :error "TapTap returned no tunnel bootstrap" }}
:execute $code
:put "TapTap Tunnel bootstrap started. Wait around 30 seconds."
'''


def repair_routeros_script(tunnel, server_key: str) -> str:
    """Repair only WireGuard/network pieces; no user/password changes."""
    endpoint = wg_endpoint()
    prefix = wg_network().prefixlen
    server_ip = wg_server_ip()
    return f''':local ifName "taptap-wg"
:if ([:len [/interface wireguard find where name=$ifName]] = 0) do={{ /interface wireguard add name=$ifName mtu=1420 comment="TapTap secure management" }}
/ip address remove [find where interface=$ifName]
/ip address add address={_rs(str(tunnel.tunnel_ip) + "/" + str(prefix))} interface=$ifName comment="TapTap tunnel"
/interface wireguard peers remove [find where interface=$ifName]
/interface wireguard peers add interface=$ifName public-key={_rs(server_key)} endpoint-address={_rs(endpoint)} endpoint-port={wg_port()} allowed-address={_rs(server_ip + "/32")} persistent-keepalive=25s comment="TapTap server"
/ip firewall filter remove [find where comment="TapTap tunnel API"]
/ip firewall filter add chain=input action=accept in-interface=$ifName src-address={_rs(server_ip)} protocol=tcp dst-port=8728 place-before=0 comment="TapTap tunnel API"
:log info "TapTap Tunnel repaired"
'''


def queue_repair(router, tunnel):
    """Queue a trusted internal repair through the existing restricted Link."""
    from .models import AgentCommand

    if AgentCommand.objects.filter(
        router=router,
        status__in=["queued", "sent"],
        label__startswith="TapTap Tunnel repair",
    ).exists():
        return None

    key = server_public_key()
    if not key:
        return None

    now = timezone.now()
    cmd = AgentCommand.objects.create(
        router=router,
        kind="script",
        params={"source": repair_routeros_script(tunnel, key)},
        label="TapTap Tunnel repair",
        expires_at=now + timedelta(minutes=15),
    )
    RouterTunnel.objects.filter(router_id=router.pk).update(
        last_repair_at=now,
        updated_at=now,
    )
    return cmd


# ---------------------------------------------------------------------------
# Runtime integration with existing TapTap code
# ---------------------------------------------------------------------------

def _connect_agent_via_tunnel(service):
    from .mikrotik import MikroTikError, routeros_api

    tunnel = get_tunnel(service.router)
    if not tunnel or not tunnel_ready(service.router):
        raise MikroTikError(
            f"{service.router.name} is currently using TapTap Link fallback; "
            "the primary TapTap Tunnel is not healthy."
        )

    service._taptap_tunnel = tunnel
    last = None
    for plaintext in (True, False):
        try:
            service.pool = routeros_api.RouterOsApiPool(
                str(tunnel.tunnel_ip),
                username=tunnel.api_username,
                password=tunnel.api_password,
                port=8728,
                use_ssl=False,
                ssl_verify=False,
                ssl_verify_hostname=False,
                plaintext_login=plaintext,
            )
            service.pool.socket_timeout = service.timeout
            service.api = service.pool.get_api()
            mark_api_ok(service.router)
            return service
        except Exception as exc:
            last = exc
            try:
                if service.pool:
                    service.pool.disconnect()
            except Exception:
                pass
            low = str(exc).lower()
            if not any(
                word in low
                for word in (
                    "login", "password", "cannot log", "invalid user", "failure"
                )
            ):
                break

    mark_tunnel_error(service.router, last or "RouterOS API connection failed")
    raise MikroTikError(
        f"TapTap Tunnel API connection to {service.router.name} failed: {last}"
    ) from last


def install_runtime_patches():
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # New Link enrollment/recovery scripts also request the one-time tunnel bootstrap.
    from . import agent as agent_mod

    if not getattr(agent_mod.enrollment_script, "_taptap_tunnel_wrapped", False):
        original_enrollment = agent_mod.enrollment_script

        def enrollment_with_tunnel(router, token, request=None):
            base = original_enrollment(router, token, request)
            if token:
                base += enrollment_bootstrap_trigger(token)
            return base

        enrollment_with_tunnel._taptap_tunnel_wrapped = True
        agent_mod.enrollment_script = enrollment_with_tunnel

    # Agent routers transparently use their tunnel credentials/address in
    # MikroTikService; Router.ip_address remains blank.
    from .mikrotik import MikroTikService

    if not getattr(MikroTikService.connect, "_taptap_tunnel_wrapped", False):
        original_connect = MikroTikService.connect

        def connect_with_tunnel(self):
            if getattr(self.router, "connection_mode", "api") != "agent":
                return original_connect(self)
            return _connect_agent_via_tunnel(self)

        connect_with_tunnel._taptap_tunnel_wrapped = True
        MikroTikService.connect = connect_with_tunnel

    # Existing live.watch_business excludes agent routers. Add them only while
    # the tunnel is healthy. When unhealthy, normal Link heartbeat remains the
    # source of online/offline state and prevents status flapping.
    from . import live as live_mod

    if not getattr(live_mod.watch_business, "_taptap_tunnel_wrapped", False):
        original_watch_business = live_mod.watch_business

        def watch_business_with_tunnels(business, force=False):
            results = original_watch_business(business, force)
            if not business.live_sync and not force:
                return results

            ids = []
            for router in business.routers.filter(connection_mode="agent"):
                if not tunnel_ready(router):
                    continue
                if (
                    router.status == "Offline"
                    and not force
                    and not live_mod.cache.add(f"tt:watch:retry:{router.pk}", 1, 60)
                ):
                    continue
                ids.append(router.pk)

            if not ids:
                return results

            if len(ids) == 1 or live_mod.connection.vendor == "sqlite":
                for router_id in ids:
                    router = live_mod.Router.objects.select_related("business").get(
                        pk=router_id
                    )
                    results.append(live_mod.watch_router(router, force))
                return results

            with live_mod.ThreadPoolExecutor(max_workers=min(6, len(ids))) as pool:
                results.extend(
                    list(
                        pool.map(
                            lambda rid: live_mod._watch_in_thread(rid, force), ids
                        )
                    )
                )
            return results

        watch_business_with_tunnels._taptap_tunnel_wrapped = True
        live_mod.watch_business = watch_business_with_tunnels


# ---------------------------------------------------------------------------
# Linux WireGuard host manager
# ---------------------------------------------------------------------------

def _run(args, *, input_text=None, check=True):
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def ensure_server_identity():
    WG_KEY_DIR.mkdir(parents=True, exist_ok=True)
    private_from_env = os.getenv("TAPTAP_WG_PRIVATE_KEY", "").strip()

    if private_from_env:
        private_key = private_from_env
        WG_PRIVATE_FILE.write_text(private_key + "\n")
        os.chmod(WG_PRIVATE_FILE, 0o600)
    elif WG_PRIVATE_FILE.exists():
        private_key = WG_PRIVATE_FILE.read_text().strip()
    else:
        private_key = _run(["wg", "genkey"]).stdout.strip()
        WG_PRIVATE_FILE.write_text(private_key + "\n")
        os.chmod(WG_PRIVATE_FILE, 0o600)

    public_key = _run(
        ["wg", "pubkey"], input_text=private_key + "\n"
    ).stdout.strip()
    if not normalize_wireguard_key(public_key):
        raise RuntimeError("Could not generate a valid WireGuard server public key")

    WG_PUBLIC_FILE.write_text(public_key + "\n")
    os.chmod(WG_PUBLIC_FILE, 0o644)
    return str(WG_PRIVATE_FILE), public_key


def _iptables_rule(table, check_args, add_args):
    base = ["iptables"]
    if table:
        base += ["-t", table]
    if _run(base + check_args, check=False).returncode != 0:
        _run(base + add_args, check=False)


def configure_server_interface(private_key_file: str):
    ifname = wg_interface()
    network = wg_network()
    server = ipaddress.ip_address(wg_server_ip())
    if server not in network:
        raise RuntimeError(f"TAPTAP_WG_SERVER_IP {server} is not inside {network}")

    if _run(["ip", "link", "show", "dev", ifname], check=False).returncode != 0:
        _run(["ip", "link", "add", "dev", ifname, "type", "wireguard"])

    _run(["ip", "address", "replace", f"{server}/{network.prefixlen}", "dev", ifname])
    _run(
        [
            "wg", "set", ifname,
            "listen-port", str(wg_port()),
            "private-key", private_key_file,
        ]
    )
    _run(["ip", "link", "set", "up", "dev", ifname])
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)

    net = str(network)
    port = str(wg_port())

    _iptables_rule(
        "",
        ["-C", "INPUT", "-p", "udp", "--dport", port, "-j", "ACCEPT"],
        ["-I", "INPUT", "1", "-p", "udp", "--dport", port, "-j", "ACCEPT"],
    )
    _iptables_rule(
        "nat",
        ["-C", "POSTROUTING", "-d", net, "-o", ifname, "-j", "MASQUERADE"],
        ["-A", "POSTROUTING", "-d", net, "-o", ifname, "-j", "MASQUERADE"],
    )
    _iptables_rule(
        "",
        ["-C", "FORWARD", "-d", net, "-o", ifname, "-j", "ACCEPT"],
        ["-I", "FORWARD", "1", "-d", net, "-o", ifname, "-j", "ACCEPT"],
    )
    _iptables_rule(
        "",
        [
            "-C", "FORWARD", "-s", net, "-i", ifname,
            "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
            "-j", "ACCEPT",
        ],
        [
            "-I", "FORWARD", "1", "-s", net, "-i", ifname,
            "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
            "-j", "ACCEPT",
        ],
    )

    if _run(["iptables", "-S", "DOCKER-USER"], check=False).returncode == 0:
        _iptables_rule(
            "",
            ["-C", "DOCKER-USER", "-d", net, "-j", "ACCEPT"],
            ["-I", "DOCKER-USER", "1", "-d", net, "-j", "ACCEPT"],
        )


def reconcile_server_peers():
    ifname = wg_interface()
    desired = {}
    for tunnel in RouterTunnel.objects.exclude(router_public_key=""):
        key = normalize_wireguard_key(tunnel.router_public_key)
        if key:
            desired[key] = tunnel

    current_result = _run(["wg", "show", ifname, "peers"], check=False)
    current = {
        line.strip()
        for line in current_result.stdout.splitlines()
        if normalize_wireguard_key(line.strip())
    }

    for pub, tunnel in desired.items():
        _run(
            [
                "wg", "set", ifname,
                "peer", pub,
                "allowed-ips", f"{tunnel.tunnel_ip}/32",
            ]
        )

    for pub in current - set(desired):
        _run(["wg", "set", ifname, "peer", pub, "remove"], check=False)


def _handshakes():
    out = {}
    result = _run(["wg", "show", wg_interface(), "latest-handshakes"], check=False)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pub = normalize_wireguard_key(parts[0])
        if not pub:
            continue
        try:
            epoch = int(parts[1])
        except ValueError:
            continue
        if epoch > 0:
            out[pub] = datetime.fromtimestamp(epoch, tz=dt_timezone.utc)
    return out


def _probe_api(tunnel) -> tuple[bool, str]:
    """Authenticate to RouterOS so online means the API is genuinely usable."""
    try:
        import routeros_api

        pool = routeros_api.RouterOsApiPool(
            str(tunnel.tunnel_ip),
            username=tunnel.api_username,
            password=tunnel.api_password,
            port=8728,
            use_ssl=False,
            ssl_verify=False,
            ssl_verify_hostname=False,
            plaintext_login=True,
        )
        pool.socket_timeout = float(os.getenv("TAPTAP_WG_API_TIMEOUT", "4"))
        api = pool.get_api()
        rows = api.get_resource("/system/resource").get()
        pool.disconnect()
        if rows:
            return True, ""
        return False, "RouterOS API returned no system resource data"
    except Exception as exc:
        return False, str(exc)[:500]


def refresh_tunnel_states():
    from .models import Router, RouterAgent

    now = timezone.now()
    handshakes = _handshakes()
    tunnel_rows = list(RouterTunnel.objects.all())
    router_ids = [row.router_id for row in tunnel_rows]
    routers = {
        r.pk: r
        for r in Router.objects.select_related("business").filter(pk__in=router_ids)
    }
    agents = {
        a.router_id: a
        for a in RouterAgent.objects.filter(router_id__in=router_ids, revoked=False)
    }

    for tunnel in tunnel_rows:
        router = routers.get(tunnel.router_id)
        if not router:
            tunnel.delete()
            continue

        agent = agents.get(router.pk)
        agent_online = bool(agent and agent.online)

        if not tunnel.router_public_key:
            tunnel.status = "bootstrap-required"
            tunnel.last_error = "Tunnel bootstrap has not registered a router public key yet."
            tunnel.save(update_fields=["status", "last_error", "updated_at"])
            continue

        hs = handshakes.get(normalize_wireguard_key(tunnel.router_public_key))
        if hs:
            tunnel.last_handshake_at = hs

        fresh = bool(
            hs and now - hs <= timedelta(seconds=handshake_timeout())
        )
        if fresh:
            ok, error = _probe_api(tunnel)
            if ok:
                tunnel.status = "online"
                tunnel.last_api_ok_at = now
                tunnel.last_error = ""
            else:
                tunnel.status = "degraded" if agent_online else "offline"
                tunnel.last_error = (
                    "WireGuard handshake is fresh but RouterOS API failed: " + error
                )
        else:
            tunnel.status = "fallback" if agent_online else "offline"
            tunnel.last_error = (
                "WireGuard handshake is stale; TapTap Link is carrying fallback control."
                if agent_online
                else "Neither a recent WireGuard handshake nor a live TapTap Link is available."
            )

            old_enough = (
                not tunnel.last_repair_at
                or now - tunnel.last_repair_at >= timedelta(minutes=5)
            )
            if agent_online and old_enough:
                try:
                    queue_repair(router, tunnel)
                except Exception as exc:
                    logger.warning(
                        "Could not queue tunnel repair for %s: %s", router, exc
                    )

        tunnel.save(
            update_fields=[
                "status",
                "last_handshake_at",
                "last_api_ok_at",
                "last_error",
                "updated_at",
            ]
        )


def manager_iteration():
    private_file, public_key = ensure_server_identity()
    configure_server_interface(private_file)
    reconcile_server_peers()
    refresh_tunnel_states()
    return public_key
