"""
TapTap Tunnel
=============

Primary management transport for RouterOS 7 routers already enrolled through
TapTap Link.

Design:
- Router.connection_mode remains "agent".
- Router.ip_address / username / password stay blank.
- The MikroTik establishes an outbound WireGuard tunnel (persistent keepalive,
  so it works behind NAT, CGNAT and 4G).
- TapTap connects to the RouterOS API through that tunnel.
- TapTap Link remains installed as heartbeat, command and repair fallback.
  Every view asks ``uses_link(router)``: True only while the tunnel is not healthy.
- RouterOS 6 simply stays on TapTap Link.

Health model:
- WireGuard renews its handshake roughly every 2 minutes even with keepalives,
  so a handshake up to ~150 s old is normal. The stale threshold defaults to
  180 s (WireGuard's own REJECT_AFTER_TIME).
- "online" additionally requires a recent successful RouterOS API login.
- Repairs go through the Link with exponential backoff and are skipped when the
  server itself looks broken (no tunnel at all has a fresh handshake).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone

logger = logging.getLogger("taptap.tunnel")

TUNNEL_TABLE = "core_router_tunnel"
WG_KEY_DIR = Path(os.getenv("TAPTAP_WG_KEY_DIR", "/var/lib/taptap-tunnel"))
WG_PRIVATE_FILE = WG_KEY_DIR / "server.key"
WG_PUBLIC_FILE = WG_KEY_DIR / "server.pub"
ROUTER_IFNAME = "taptap-wg"
REPAIR_LABEL = "TapTap Tunnel repair"
ENC_PREFIX = "fernet:"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class RouterTunnel(models.Model):
    """Runtime model for the transport table created by migrations 0017/0018."""

    router_id = models.BigIntegerField(primary_key=True)
    tunnel_ip = models.GenericIPAddressField(unique=True)
    router_public_key = models.CharField(max_length=128, blank=True)
    api_username = models.CharField(max_length=64, default="taptap-tunnel")
    # Stored encrypted ("fernet:..."); use .password to read/write the clear value.
    api_password = models.CharField(max_length=512)
    api_port = models.IntegerField(default=8728)
    status = models.CharField(max_length=32, default="waiting")
    last_handshake_at = models.DateTimeField(null=True, blank=True)
    last_api_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_repair_at = models.DateTimeField(null=True, blank=True)
    repair_attempts = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = TUNNEL_TABLE
        app_label = "core"

    @property
    def password(self) -> str:
        return decrypt_secret(self.api_password)

    @password.setter
    def password(self, value: str):
        self.api_password = encrypt_secret(value)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _env_bool(name: str, default=False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


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
    return _env_int("TAPTAP_WG_PORT", 51820)


def wg_mtu() -> int:
    # 1380 is safe on 4G/LTE and PPPoE uplinks; 1420 stalls on some of them.
    return max(1280, min(1420, _env_int("TAPTAP_WG_MTU", 1380)))


def wg_endpoint_explicit() -> bool:
    return bool(os.getenv("TAPTAP_WG_ENDPOINT", "").strip())


def wg_endpoint() -> str:
    explicit = os.getenv("TAPTAP_WG_ENDPOINT", "").strip()
    if explicit:
        return explicit
    site = getattr(settings, "SITE_URL", "") or ""
    return (urlparse(site).hostname or "") if site else ""


def handshake_timeout() -> int:
    """Seconds after which a handshake is stale. WireGuard rekeys every ~120 s."""
    return max(150, _env_int("TAPTAP_WG_HANDSHAKE_TIMEOUT", 180))


def probe_interval() -> int:
    """How often an already-online tunnel gets a fresh API login check."""
    return max(10, _env_int("TAPTAP_WG_PROBE_SECONDS", 30))


def api_fresh_window() -> int:
    return max(120, probe_interval() * 4)


def routeros_major(version: str) -> int:
    match = re.match(r"\s*(\d+)", str(version or ""))
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Secret storage
# ---------------------------------------------------------------------------

_FERNET = None


def _fernet():
    global _FERNET
    if _FERNET is None:
        from cryptography.fernet import Fernet

        secret = os.getenv("TAPTAP_TUNNEL_SECRET", "").strip() or settings.SECRET_KEY
        key = base64.urlsafe_b64encode(
            hashlib.sha256(("taptap-tunnel:" + secret).encode()).digest()
        )
        _FERNET = Fernet(key)
    return _FERNET


def encrypt_secret(value: str) -> str:
    value = str(value or "")
    if not value or value.startswith(ENC_PREFIX):
        return value
    return ENC_PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    value = str(value or "")
    if not value.startswith(ENC_PREFIX):
        return value  # legacy plaintext row (migration 0018 encrypts these)
    try:
        return _fernet().decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except Exception:
        logger.error(
            "Cannot decrypt a TapTap Tunnel API password. Was TAPTAP_TUNNEL_SECRET "
            "or SECRET_KEY changed? Re-run the tunnel bootstrap on affected routers."
        )
        return ""


# ---------------------------------------------------------------------------
# Keys and records
# ---------------------------------------------------------------------------

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
        RouterTunnel.objects.exclude(tunnel_ip="").values_list("tunnel_ip", flat=True)
    )

    # Stable first choice based on Router PK.
    candidate_int = int(network.network_address) + 10 + int(router_id)
    if candidate_int < int(network.broadcast_address):
        candidate = ipaddress.ip_address(candidate_int)
        if candidate != server and str(candidate) not in used:
            return str(candidate)

    for raw in range(int(network.network_address) + 10, int(network.broadcast_address)):
        candidate = ipaddress.ip_address(raw)
        if candidate != server and str(candidate) not in used:
            return str(candidate)
    raise RuntimeError(f"No free TapTap Tunnel address remains in {network}")


def ensure_tunnel(router) -> RouterTunnel:
    """Return/create the transport record. Safe against concurrent bootstraps."""
    for _attempt in range(6):
        existing = RouterTunnel.objects.filter(router_id=router.pk).first()
        if existing:
            return existing
        try:
            with transaction.atomic():
                tunnel = RouterTunnel(
                    router_id=router.pk,
                    tunnel_ip=_allocate_ip(router.pk),
                    api_username="taptap-tunnel",
                    status="waiting",
                    last_error="",
                )
                tunnel.password = secrets.token_urlsafe(30)
                tunnel.save(force_insert=True)
                return tunnel
        except IntegrityError:
            # Another request took this router or this IP at the same moment.
            continue
    raise RuntimeError("Could not allocate a TapTap Tunnel address; please retry.")


def get_tunnel(router) -> RouterTunnel | None:
    try:
        return RouterTunnel.objects.filter(router_id=router.pk).first()
    except Exception:
        return None


def _is_ready(tunnel, now=None) -> bool:
    if not tunnel or tunnel.status != "online":
        return False
    now = now or timezone.now()
    if not tunnel.last_handshake_at or not tunnel.last_api_ok_at:
        return False
    if now - tunnel.last_handshake_at > timedelta(seconds=handshake_timeout()):
        return False
    if now - tunnel.last_api_ok_at > timedelta(seconds=api_fresh_window()):
        return False
    return True


def tunnel_ready(router) -> bool:
    if not tunnel_enabled():
        return False
    if getattr(router, "connection_mode", "api") != "agent":
        return False
    return _is_ready(get_tunnel(router))


def uses_link(router) -> bool:
    """True when TapTap must use Link commands/snapshots for this router right now."""
    if not router or getattr(router, "connection_mode", "api") != "agent":
        return False
    try:
        return not tunnel_ready(router)
    except Exception:
        return True


def tunnel_summary(router) -> dict:
    """Small dict for the Link page and its status JSON."""
    t = get_tunnel(router)
    try:
        ver = router.agent.ros_version
    except Exception:
        ver = ""
    major = routeros_major(ver)
    return {
        "enabled": tunnel_enabled(),
        "supported": not (major and major < 7),
        "exists": bool(t),
        "registered": bool(t and t.router_public_key),
        "ready": _is_ready(t) if tunnel_enabled() else False,
        "status": (t.status if t else "not-set-up"),
        "ip": (str(t.tunnel_ip) if t else ""),
        "last_handshake": (t.last_handshake_at.isoformat() if t and t.last_handshake_at else None),
        "last_api_ok": (t.last_api_ok_at.isoformat() if t and t.last_api_ok_at else None),
        "last_repair": (t.last_repair_at.isoformat() if t and t.last_repair_at else None),
        "error": (t.last_error if t else ""),
    }


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
        now = timezone.now()
        RouterTunnel.objects.filter(router_id=router.pk).update(
            status="online", last_api_ok_at=now, last_error="", updated_at=now,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RouterOS scripts
# ---------------------------------------------------------------------------

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


def _site() -> str:
    return (getattr(settings, "SITE_URL", "") or "").rstrip("/")


def _register_url() -> str:
    return _site() + "/api/tunnel/v1/register"


def _script_vars(tunnel, server_key: str) -> str:
    prefix = wg_network().prefixlen
    server_ip = wg_server_ip()
    return f''':local ifName "{ROUTER_IFNAME}"
:local srvKey {_rs(server_key)}
:local srvHost {_rs(wg_endpoint())}
:local srvPort {wg_port()}
:local srvIp {_rs(server_ip)}
:local srvAllow {_rs(server_ip + "/32")}
:local myAddr {_rs(f"{tunnel.tunnel_ip}/{prefix}")}
:local fwComment "TapTap tunnel API"
'''


# Idempotent: a working tunnel is left untouched (no peer delete/re-add), and the
# router keeps its existing private key unless the interface itself is missing.
_WG_SECTION = f''':if ([:len [/interface wireguard find where name=$ifName]] = 0) do={{
  /interface wireguard add name=$ifName mtu={{mtu}} comment="TapTap secure management"
  :log warning "TapTap Tunnel: WireGuard interface created (new key)"
}}
:local wg [/interface wireguard find where name=$ifName]
/interface wireguard set $wg disabled=no mtu={{mtu}}
:if ([:len [/ip address find where interface=$ifName and address=$myAddr]] = 0) do={{
  /ip address remove [find where interface=$ifName]
  /ip address add address=$myAddr interface=$ifName comment="TapTap tunnel"
}}
/interface wireguard peers remove [find where interface=$ifName and public-key!=$srvKey]
:local peer [/interface wireguard peers find where interface=$ifName and public-key=$srvKey]
:if ([:len $peer] = 0) do={{
  /interface wireguard peers add interface=$ifName public-key=$srvKey endpoint-address=$srvHost endpoint-port=$srvPort allowed-address=$srvAllow persistent-keepalive=25s comment="TapTap server"
}} else={{
  /interface wireguard peers set $peer endpoint-address=$srvHost endpoint-port=$srvPort allowed-address=$srvAllow persistent-keepalive=25s disabled=no
}}
'''

# place-before needs an existing, non-dynamic rule; an empty filter list just appends.
_FW_SECTION = ''':local apiPort [/ip service get [find where name="api"] port]
/ip firewall filter remove [find where comment=$fwComment]
:local firstRule [/ip firewall filter find where dynamic=no]
:if ([:len $firstRule] > 0) do={
  /ip firewall filter add chain=input action=accept in-interface=$ifName src-address=$srvIp protocol=tcp dst-port=[:tostr $apiPort] place-before=[:pick $firstRule 0] comment=$fwComment
} else={
  /ip firewall filter add chain=input action=accept in-interface=$ifName src-address=$srvIp protocol=tcp dst-port=[:tostr $apiPort] comment=$fwComment
}
'''

# Keep whatever API allow-list the owner already has; only add the TapTap server.
_API_SECTION = ''':local apiSvc [/ip service find where name="api"]
:local wasOff [/ip service get $apiSvc disabled]
:local addrs [/ip service get $apiSvc address]
:if ($wasOff) do={
  /ip service set $apiSvc disabled=no address=$srvAllow
} else={
  :if ([:len $addrs] > 0) do={
    :local has false
    :local joined ""
    :foreach a in=$addrs do={
      :local s [:tostr $a]
      :if ($s = $srvAllow or $s = $srvIp) do={ :set has true }
      :if ([:len $s] > 0) do={ :set joined ($joined . $s . ",") }
    }
    :if (!$has) do={ /ip service set $apiSvc address=($joined . $srvAllow) }
  } else={
    :log warning "TapTap Tunnel: IP > Services > api is open to every address; left unchanged. Consider restricting it."
  }
}
'''


def _register_section(token_expr: str) -> str:
    """Report the router's public key and API port. token_expr is a RouterOS expression."""
    return f''':local pub [/interface wireguard get [find where name=$ifName] public-key]
:local regPort [/ip service get [find where name="api"] port]
:do {{
  /tool fetch url=({_rs(_register_url() + "?pub=")} . $pub . "&port=" . [:tostr $regPort]) http-method=post http-header-field=("Authorization: Bearer " . {token_expr}) output=none check-certificate={_tls_check()} duration=10s idle-timeout=8s
}} on-error={{
  :log warning "TapTap Tunnel: public-key registration failed; TapTap will retry through the Link"
}}
'''


_TOKEN_FROM_LINK = ''':local ttSid [/system script find where name="taptap-link"]
:local ttTok ""
:if ([:len $ttSid] > 0) do={
  :local ttSrc [/system script get $ttSid source]
  :local ttP [:find $ttSrc "ttl_"]
  :if ([:typeof $ttP] = "num") do={
    :local ttQ [:find $ttSrc "\\"" $ttP]
    :set ttTok [:pick $ttSrc $ttP $ttQ]
  }
}
'''


def bootstrap_routeros_script(router, tunnel, link_token: str, server_key: str) -> str:
    """
    One-time bootstrap returned only to an authenticated TapTap Link router.

    It is launched from the administrator-pasted enrollment/bootstrap command
    (full rights), not the restricted taptap-link scheduler, because it creates
    the API user and touches IP > Services.
    """
    if not wg_endpoint():
        return ':log warning "TapTap Tunnel: TAPTAP_WG_ENDPOINT is not configured"'

    return (
        f"# --- TapTap Tunnel bootstrap for {re.sub(r'[^\x20-\x7e]', '?', router.name)} ---\n"
        + _script_vars(tunnel, server_key)
        + f''':local apiGroup "taptap-tunnel"
:local apiUser {_rs(tunnel.api_username)}
:local apiPass {_rs(tunnel.password)}
'''
        + _WG_SECTION.replace("{mtu}", str(wg_mtu()))
        + ''':if ([:len [/user group find where name=$apiGroup]] = 0) do={
  /user group add name=$apiGroup policy=read,write,policy,test,api,sensitive,reboot
} else={
  /user group set [find where name=$apiGroup] policy=read,write,policy,test,api,sensitive,reboot
}
:if ([:len [/user find where name=$apiUser]] = 0) do={
  /user add name=$apiUser group=$apiGroup password=$apiPass comment="TapTap tunnel API"
} else={
  /user set [find where name=$apiUser] group=$apiGroup password=$apiPass disabled=no
}
'''
        + _API_SECTION
        + _FW_SECTION
        + _register_section(_rs(link_token))
        + ':log info "TapTap Tunnel configured"\n'
    )


def enrollment_bootstrap_trigger(link_token: str) -> str:
    """Appended to newly-generated Link enrollment/recovery scripts."""
    if not tunnel_enabled() or not _site():
        return ""
    url = _site() + "/api/tunnel/v1/bootstrap"
    return f'''
# --- TapTap Tunnel: primary persistent connection (RouterOS 7) ---
:local ttVer [/system resource get version]
:if ([:tonum [:pick $ttVer 0 [:find $ttVer "."]]] >= 7) do={{
  :do {{
    :local ttBoot [/tool fetch url={_rs(url)} http-method=post http-header-field={_rs("Authorization: Bearer " + link_token)} output=user as-value check-certificate={_tls_check()} duration=12s idle-timeout=8s]
    :local ttCode ($ttBoot->"data")
    :if ([:len $ttCode] > 20) do={{ :execute $ttCode }}
  }} on-error={{
    :log warning "TapTap Tunnel bootstrap did not start; TapTap Link remains active"
  }}
}}
'''


def existing_router_bootstrap_script() -> str:
    """Generic one-time bootstrap for a router that already has TapTap Link.

    Contains no token (it reads the router's own), so it is safe to display.
    """
    site = _site() or "https://taptapnetwork.com"
    url = site + "/api/tunnel/v1/bootstrap"
    return f'''# TapTap Tunnel - router that already has TapTap Link (RouterOS 7)
:local ver [/system resource get version]
:if ([:tonum [:pick $ver 0 [:find $ver "."]]] < 7) do={{ :error "TapTap Tunnel needs RouterOS 7; TapTap Link keeps working" }}
:local sid [/system script find where name="taptap-link"]
:if ([:len $sid] = 0) do={{ :error "TapTap Link is not installed" }}
:local src [/system script get $sid source]
:local p [:find $src "ttl_"]
:local q [:find $src "\\"" $p]
:local tok [:pick $src $p $q]
:if ([:len $tok] < 20) do={{ :error "TapTap Link token was not found" }}
:put "Requesting TapTap Tunnel configuration..."
:local r [/tool fetch url={_rs(url)} http-method=post http-header-field=("Authorization: Bearer " . $tok) output=user as-value check-certificate={_tls_check()} duration=12s idle-timeout=8s]
:local code ($r->"data")
:if ([:len $code] < 20) do={{ :error "TapTap returned no tunnel bootstrap" }}
:execute $code
:put "TapTap Tunnel bootstrap started. Wait around 30 seconds, then check the TapTap Link page."
'''


def repair_routeros_script(tunnel, server_key: str) -> str:
    """Repair WireGuard/network pieces and re-register the key. No user/password
    or IP > Services changes, so it runs within the restricted Link policy."""
    return (
        _script_vars(tunnel, server_key)
        + _WG_SECTION.replace("{mtu}", str(wg_mtu()))
        + _FW_SECTION
        + _TOKEN_FROM_LINK
        + ':if ([:len $ttTok] >= 20) do={\n'
        + _register_section("$ttTok")
        + '} else={ :log warning "TapTap Tunnel repair: Link token not found; key not re-registered" }\n'
        + ':log info "TapTap Tunnel repaired"\n'
    )


def repair_backoff(attempts: int) -> timedelta:
    """5, 10, 20, 40, 60, 60 ... minutes between repairs of the same router."""
    return timedelta(minutes=min(60, 5 * (2 ** max(0, attempts))))


def queue_repair(router, tunnel):
    """Queue a trusted internal repair through the existing restricted Link."""
    from .models import AgentCommand

    if AgentCommand.objects.filter(
        router=router, status__in=["queued", "sent"], label__startswith=REPAIR_LABEL,
    ).exists():
        return None

    key = server_public_key()
    if not key or not wg_endpoint():
        return None

    now = timezone.now()
    cmd = AgentCommand.objects.create(
        router=router,
        kind="script",
        params={"source": repair_routeros_script(tunnel, key)},
        label=REPAIR_LABEL,
        expires_at=now + timedelta(minutes=15),
    )
    RouterTunnel.objects.filter(router_id=router.pk).update(
        last_repair_at=now,
        repair_attempts=models.F("repair_attempts") + 1,
        updated_at=now,
    )
    return cmd


# ---------------------------------------------------------------------------
# RouterOS API through the tunnel (used by MikroTikService.connect)
# ---------------------------------------------------------------------------

def connect_via_tunnel(service):
    from .mikrotik import MikroTikError, routeros_api

    router = service.router
    tunnel = get_tunnel(router)
    if not tunnel or not tunnel_enabled() or not _is_ready(tunnel):
        raise MikroTikError(
            f"{router.name} is currently using TapTap Link fallback; "
            "the primary TapTap Tunnel is not healthy."
        )

    password = tunnel.password
    service._taptap_tunnel = tunnel
    last = None
    for plaintext in (True, False):
        try:
            service.pool = routeros_api.RouterOsApiPool(
                str(tunnel.tunnel_ip),
                username=tunnel.api_username,
                password=password,
                port=int(tunnel.api_port or 8728),
                use_ssl=False,
                ssl_verify=False,
                ssl_verify_hostname=False,
                plaintext_login=plaintext,
            )
            service.pool.socket_timeout = service.timeout
            service.api = service.pool.get_api()
            mark_api_ok(router)
            return service
        except Exception as exc:
            last = exc
            try:
                if service.pool:
                    service.pool.disconnect()
            except Exception:
                pass
            low = str(exc).lower()
            if not any(w in low for w in ("login", "password", "cannot log", "invalid user", "failure")):
                break

    mark_tunnel_error(router, last or "RouterOS API connection failed")
    raise MikroTikError(
        f"TapTap Tunnel API connection to {router.name} failed: {last}. "
        "TapTap switches this router to TapTap Link; retry in a moment."
    ) from last


# ---------------------------------------------------------------------------
# Linux WireGuard host manager (runs only in the tunnel container)
# ---------------------------------------------------------------------------

_IPTABLES = None
_WARNED = set()


def _warn_once(key, msg, *args):
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning(msg, *args)


def _run(args, *, input_text=None, check=True):
    result = subprocess.run(
        args, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def iptables_bin() -> str:
    """Use the same iptables backend (nft or legacy) as the host's Docker.

    Rules written through the wrong backend land in tables Docker never consults,
    so the web/worker containers could not reach 10.77.x.x.
    """
    global _IPTABLES
    if _IPTABLES:
        return _IPTABLES
    forced = os.getenv("TAPTAP_IPTABLES", "").strip()
    if forced:
        _IPTABLES = forced
        return _IPTABLES

    best, best_score = "iptables", -1
    for binary in ("iptables-nft", "iptables-legacy"):
        try:
            res = _run([binary, "-S"], check=False)
            nat = _run([binary, "-t", "nat", "-S"], check=False)
        except FileNotFoundError:
            continue
        if res.returncode != 0:
            continue
        text = res.stdout + nat.stdout
        score = text.count("\n") + (10000 if "DOCKER" in text else 0)
        if score > best_score:
            best, best_score = binary, score
    _IPTABLES = best
    logger.info("TapTap Tunnel: using %s (matches the host's Docker rules)", best)
    return _IPTABLES


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

    public_key = _run(["wg", "pubkey"], input_text=private_key + "\n").stdout.strip()
    if not normalize_wireguard_key(public_key):
        raise RuntimeError("Could not generate a valid WireGuard server public key")

    if not WG_PUBLIC_FILE.exists() or WG_PUBLIC_FILE.read_text().strip() != public_key:
        WG_PUBLIC_FILE.write_text(public_key + "\n")
        os.chmod(WG_PUBLIC_FILE, 0o644)
    return str(WG_PRIVATE_FILE), public_key


def _iptables_rule(table, check_args, add_args):
    base = [iptables_bin()]
    if table:
        base += ["-t", table]
    if _run(base + check_args, check=False).returncode != 0:
        res = _run(base + add_args, check=False)
        if res.returncode != 0:
            _warn_once(
                ("ipt",) + tuple(add_args),
                "TapTap Tunnel: could not add firewall rule %s: %s",
                " ".join(add_args), (res.stderr or res.stdout).strip(),
            )


def check_endpoint():
    """Log once when the endpoint is likely unusable for UDP."""
    endpoint = wg_endpoint()
    if not endpoint:
        _warn_once("ep-missing", "TapTap Tunnel: set TAPTAP_WG_ENDPOINT; routers cannot dial the server.")
        return
    if not wg_endpoint_explicit():
        _warn_once(
            "ep-derived",
            "TapTap Tunnel: TAPTAP_WG_ENDPOINT is not set, so routers dial %s (from SITE_URL). "
            "If that name is behind a proxy/CDN (e.g. Cloudflare orange cloud) UDP %s will not "
            "arrive. Set TAPTAP_WG_ENDPOINT to an unproxied DNS name or the server's public IP, "
            "and open UDP %s in any cloud firewall/security group.",
            endpoint, wg_port(), wg_port(),
        )


def configure_server_interface(private_key_file: str):
    ifname = wg_interface()
    network = wg_network()
    server = ipaddress.ip_address(wg_server_ip())
    if server not in network:
        raise RuntimeError(f"TAPTAP_WG_SERVER_IP {server} is not inside {network}")

    if _run(["ip", "link", "show", "dev", ifname], check=False).returncode != 0:
        _run(["ip", "link", "add", "dev", ifname, "type", "wireguard"])

    _run(["ip", "address", "replace", f"{server}/{network.prefixlen}", "dev", ifname])
    _run(["wg", "set", ifname, "listen-port", str(wg_port()), "private-key", private_key_file])
    _run(["ip", "link", "set", "mtu", str(wg_mtu()), "up", "dev", ifname])
    # Docker already enables forwarding on the host; /proc/sys may be read-only here.
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)

    net = str(network)
    port = str(wg_port())

    _iptables_rule(
        "",
        ["-C", "INPUT", "-p", "udp", "--dport", port, "-j", "ACCEPT"],
        ["-I", "INPUT", "1", "-p", "udp", "--dport", port, "-j", "ACCEPT"],
    )
    # Containers reach routers as 10.77.0.1, which the router firewall/API allow.
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
        ["-C", "FORWARD", "-s", net, "-i", ifname, "-m", "conntrack",
         "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["-I", "FORWARD", "1", "-s", net, "-i", ifname, "-m", "conntrack",
         "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    )
    if _run([iptables_bin(), "-S", "DOCKER-USER"], check=False).returncode == 0:
        _iptables_rule(
            "",
            ["-C", "DOCKER-USER", "-d", net, "-j", "ACCEPT"],
            ["-I", "DOCKER-USER", "1", "-d", net, "-j", "ACCEPT"],
        )
        _iptables_rule(
            "",
            ["-C", "DOCKER-USER", "-s", net, "-m", "conntrack",
             "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            ["-I", "DOCKER-USER", "1", "-s", net, "-m", "conntrack",
             "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        )


def reconcile_server_peers():
    """Add/update/remove peers live with `wg set`; only touches what changed."""
    ifname = wg_interface()
    desired = {}
    for tunnel in RouterTunnel.objects.exclude(router_public_key=""):
        key = normalize_wireguard_key(tunnel.router_public_key)
        if key:
            desired[key] = f"{tunnel.tunnel_ip}/32"

    current = {}
    result = _run(["wg", "show", ifname, "allowed-ips"], check=False)
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and normalize_wireguard_key(parts[0]):
            current[parts[0]] = " ".join(sorted(parts[1:]))

    for pub, allowed in desired.items():
        if current.get(pub) != allowed:
            _run(["wg", "set", ifname, "peer", pub, "allowed-ips", allowed])

    for pub in set(current) - set(desired):
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


def _probe_api(ip: str, username: str, password: str, port: int) -> tuple[bool, str]:
    """Authenticate to RouterOS so online means the API is genuinely usable.
    Runs in a worker thread: no database access here."""
    pool = None
    try:
        import routeros_api

        pool = routeros_api.RouterOsApiPool(
            ip, username=username, password=password, port=int(port or 8728),
            use_ssl=False, ssl_verify=False, ssl_verify_hostname=False, plaintext_login=True,
        )
        pool.socket_timeout = float(os.getenv("TAPTAP_WG_API_TIMEOUT", "4"))
        api = pool.get_api()
        rows = api.get_resource("/system/resource").get()
        if rows:
            return True, ""
        return False, "RouterOS API returned no system resource data"
    except Exception as exc:
        return False, str(exc)[:500]
    finally:
        try:
            if pool:
                pool.disconnect()
        except Exception:
            pass


def refresh_tunnel_states():
    from .models import Router, RouterAgent

    now = timezone.now()
    handshakes = _handshakes()
    tunnel_rows = list(RouterTunnel.objects.all())
    router_ids = [row.router_id for row in tunnel_rows]
    routers = {
        r.pk: r for r in Router.objects.select_related("business").filter(pk__in=router_ids)
    }
    agents = {
        a.router_id: a
        for a in RouterAgent.objects.filter(router_id__in=router_ids, revoked=False)
    }
    stale_after = timedelta(seconds=handshake_timeout())

    # ---- pass 1: classify, decide who needs an API probe --------------------
    plan = []  # (tunnel, router, agent_online, fresh)
    to_probe = []
    for tunnel in tunnel_rows:
        router = routers.get(tunnel.router_id)
        if not router:
            tunnel.delete()
            continue
        agent = agents.get(router.pk)
        agent_online = bool(agent and agent.online)

        if not tunnel.router_public_key:
            plan.append((tunnel, router, agent_online, None))
            continue

        hs = handshakes.get(normalize_wireguard_key(tunnel.router_public_key))
        if hs:
            tunnel.last_handshake_at = hs
        fresh = bool(hs and now - hs <= stale_after)
        plan.append((tunnel, router, agent_online, fresh))

        if fresh:
            due = (
                tunnel.status != "online"
                or not tunnel.last_api_ok_at
                or now - tunnel.last_api_ok_at >= timedelta(seconds=probe_interval())
            )
            if due:
                to_probe.append(tunnel)

    # ---- pass 2: probe in parallel (no DB in threads) -----------------------
    results = {}
    if to_probe:
        workers = max(1, min(_env_int("TAPTAP_WG_PROBE_WORKERS", 16), len(to_probe)))
        jobs = [
            (t.router_id, str(t.tunnel_ip), t.api_username, t.password, t.api_port)
            for t in to_probe
        ]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for rid, outcome in zip(
                [j[0] for j in jobs],
                pool.map(lambda j: _probe_api(j[1], j[2], j[3], j[4]), jobs),
            ):
                results[rid] = outcome

    # Server-side outage guard: if routers with a live Link and a registered key
    # exist but NONE has a fresh handshake, the problem is almost certainly on the
    # server (UDP port closed, endpoint wrong, interface down). Repairing every
    # router would only churn their configs.
    candidates = [p for p in plan if p[3] is not None and p[2]]
    server_suspect = len(candidates) >= 3 and not any(p[3] for p in candidates)
    if server_suspect:
        _warn_once(
            "server-suspect",
            "TapTap Tunnel: no router has a fresh WireGuard handshake although %s have a live "
            "Link. Check UDP %s reachability, TAPTAP_WG_ENDPOINT and the %s interface. "
            "Automatic router repairs are paused until one tunnel comes up.",
            len(candidates), wg_port(), wg_interface(),
        )
    else:
        _WARNED.discard("server-suspect")

    # ---- pass 3: apply states ------------------------------------------------
    for tunnel, router, agent_online, fresh in plan:
        fields = ["status", "last_handshake_at", "last_api_ok_at", "last_error", "updated_at"]

        if fresh is None:
            tunnel.status = "bootstrap-required"
            tunnel.last_error = "Tunnel bootstrap has not registered a router public key yet."
            tunnel.save(update_fields=["status", "last_error", "updated_at"])
            continue

        if fresh:
            outcome = results.get(tunnel.router_id)
            if outcome is None:
                pass  # online and probed recently; keep state
            elif outcome[0]:
                tunnel.status = "online"
                tunnel.last_api_ok_at = now
                tunnel.last_error = ""
                if tunnel.repair_attempts:
                    tunnel.repair_attempts = 0
                    fields.append("repair_attempts")
            else:
                tunnel.status = "degraded" if agent_online else "offline"
                tunnel.last_error = "WireGuard handshake is fresh but RouterOS API failed: " + outcome[1]
        else:
            tunnel.status = "fallback" if agent_online else "offline"
            tunnel.last_error = (
                "WireGuard handshake is stale; TapTap Link is carrying fallback control."
                if agent_online
                else "Neither a recent WireGuard handshake nor a live TapTap Link is available."
            )
            due = (
                not tunnel.last_repair_at
                or now - tunnel.last_repair_at >= repair_backoff(tunnel.repair_attempts)
            )
            if agent_online and due and not server_suspect:
                try:
                    queue_repair(router, tunnel)
                except Exception as exc:
                    logger.warning("Could not queue tunnel repair for %s: %s", router, exc)

        tunnel.save(update_fields=fields)


def manager_iteration():
    private_file, public_key = ensure_server_identity()
    check_endpoint()
    configure_server_interface(private_file)
    reconcile_server_peers()
    refresh_tunnel_states()
    return public_key
