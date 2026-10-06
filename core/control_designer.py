"""Visual MikroTik Control Center designer.

Safe catalogue only: the browser sends a recipe key + parameters, never RouterOS
commands. The server validates the recipe, builds a deterministic plan, snapshots
the affected rows, applies it, and stores rollback data.

TapTap Link support is added as one fixed command kind. The command body is rebuilt
from the validated deployment, not accepted as arbitrary script text from the UI.
"""
from __future__ import annotations

import functools
import ipaddress
import json
import re
from copy import deepcopy

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Router
from .models_control_designer import RouterVisualDeployment

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:+/@ -]{1,120}$")
SAFE_SHORT = re.compile(r"^[A-Za-z0-9_.:+/@, -]{1,180}$")

CATALOG = {
    "port_enabled": {
        "label": "Port state",
        "icon": "bi-toggle-on",
        "scope": "port",
        "risk": "normal",
        "summary": "Enable or disable one interface.",
        "fields": [
            {"name": "enabled", "label": "State", "type": "select",
             "options": [["yes","Enabled"],["no","Disabled"]], "default": "yes"},
        ],
    },
    "bridge_member": {
        "label": "Bridge membership",
        "icon": "bi-diagram-3",
        "scope": "port",
        "risk": "normal",
        "summary": "Attach this physical port to a bridge.",
        "fields": [{"name":"bridge","label":"Bridge name","type":"text","default":"bridge1"}],
    },
    "access_vlan": {
        "label": "Access VLAN",
        "icon": "bi-box-arrow-in-down",
        "scope": "port",
        "risk": "advanced",
        "summary": "Set bridge PVID and register this port as untagged for one VLAN.",
        "fields": [
            {"name":"bridge","label":"Bridge","type":"text","default":"bridge1"},
            {"name":"vlan","label":"VLAN ID","type":"number","default":"10"},
        ],
    },
    "trunk_vlan": {
        "label": "VLAN trunk",
        "icon": "bi-bezier2",
        "scope": "port",
        "risk": "advanced",
        "summary": "Register selected VLANs as tagged on this port and the bridge CPU.",
        "fields": [
            {"name":"bridge","label":"Bridge","type":"text","default":"bridge1"},
            {"name":"vlans","label":"VLAN IDs (comma separated)","type":"text","default":"10,20"},
        ],
    },
    "wan_dhcp": {
        "label": "WAN DHCP + NAT",
        "icon": "bi-globe2",
        "scope": "port",
        "risk": "advanced",
        "summary": "Remove the port from a bridge, enable DHCP client and masquerade NAT.",
        "fields": [],
    },
    "static_wan": {
        "label": "Static WAN",
        "icon": "bi-signpost-split",
        "scope": "port",
        "risk": "high",
        "summary": "Static address + default route + masquerade NAT.",
        "fields": [
            {"name":"address","label":"Address / prefix","type":"text","default":"192.0.2.2/24"},
            {"name":"gateway","label":"Gateway","type":"text","default":"192.0.2.1"},
        ],
    },
    "identity": {
        "label": "Router identity",
        "icon": "bi-router",
        "scope": "router",
        "risk": "normal",
        "summary": "Change the RouterOS system identity.",
        "fields": [{"name":"identity","label":"Identity","type":"text","default":"TapTap-Router"}],
    },
    "dns": {
        "label": "DNS resolver",
        "icon": "bi-hdd-network",
        "scope": "router",
        "risk": "normal",
        "summary": "Set upstream DNS servers and whether LAN clients may query the router.",
        "fields": [
            {"name":"servers","label":"DNS servers","type":"text","default":"1.1.1.1,8.8.8.8"},
            {"name":"allow_remote","label":"Allow LAN DNS","type":"select",
             "options":[["yes","Yes"],["no","No"]], "default":"yes"},
        ],
    },
    "ntp": {
        "label": "NTP time sync",
        "icon": "bi-clock-history",
        "scope": "router",
        "risk": "normal",
        "summary": "Enable NTP and point RouterOS to pool.ntp.org.",
        "fields": [],
    },
    "mgmt_firewall": {
        "label": "Management firewall",
        "icon": "bi-shield-lock",
        "scope": "router",
        "risk": "high",
        "summary": "Add a controlled input-chain baseline and a final drop rule.",
        "fields": [
            {"name":"management_subnet","label":"Management subnet","type":"text","default":"192.168.1.0/24"},
        ],
    },
}

MANAGED = "TapTap-Designer:"


def _business(request):
    return getattr(request, "tt_business", None) or request.user.business


def _router(request, pk):
    return get_object_or_404(Router, pk=pk, business=_business(request))


def _name(value, field="value"):
    value = str(value or "").strip()
    if not SAFE_NAME.fullmatch(value):
        raise ValueError(f"Invalid {field}.")
    return value


def _ip_interface(value):
    try:
        return str(ipaddress.ip_interface(str(value).strip()))
    except ValueError:
        raise ValueError("Enter a valid IP address with prefix, e.g. 192.0.2.2/24.")


def _ip(value, field="IP address"):
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        raise ValueError(f"Enter a valid {field}.")


def _network(value):
    try:
        return str(ipaddress.ip_network(str(value).strip(), strict=False))
    except ValueError:
        raise ValueError("Enter a valid management subnet.")


def _vlans(value):
    out = []
    for x in str(value or "").split(","):
        if not x.strip():
            continue
        try:
            v = int(x)
        except ValueError:
            raise ValueError("VLAN IDs must be numbers.")
        if not 1 <= v <= 4094:
            raise ValueError("VLAN IDs must be from 1 to 4094.")
        if v not in out:
            out.append(v)
    if not out or len(out) > 32:
        raise ValueError("Enter 1 to 32 VLAN IDs.")
    return out


def _op(resource, action="ensure", find=None, values=None, label=""):
    return {
        "resource": resource,
        "action": action,
        "find": find or {},
        "values": values or {},
        "label": label or resource,
    }


def build_plan(recipe, target, params):
    if recipe not in CATALOG:
        raise ValueError("Unknown configuration tile.")
    meta = CATALOG[recipe]
    params = dict(params or {})

    if meta["scope"] == "port":
        target = _name(target, "interface")
    else:
        target = ""

    comment = f"{MANAGED}{recipe}:{target or 'router'}"

    if recipe == "port_enabled":
        return [_op("/interface", "set", {"name": target},
                    {"disabled": "no" if params.get("enabled", "yes") == "yes" else "yes"},
                    "Port state")]

    if recipe == "bridge_member":
        bridge = _name(params.get("bridge"), "bridge")
        return [_op("/interface/bridge/port", "ensure",
                    {"interface": target}, {"bridge": bridge, "disabled": "no", "comment": comment},
                    "Bridge membership")]

    if recipe == "access_vlan":
        bridge = _name(params.get("bridge"), "bridge")
        try:
            vlan = int(params.get("vlan"))
        except Exception:
            raise ValueError("VLAN ID must be a number.")
        if not 1 <= vlan <= 4094:
            raise ValueError("VLAN ID must be from 1 to 4094.")
        return [
            _op("/interface/bridge/port", "ensure", {"interface": target},
                {"bridge": bridge, "pvid": str(vlan), "disabled": "no", "comment": comment},
                "Access VLAN PVID"),
            _op("/interface/bridge/vlan", "ensure",
                {"bridge": bridge, "vlan_ids": str(vlan)},
                {"untagged": target, "comment": comment},
                "Access VLAN table"),
        ]

    if recipe == "trunk_vlan":
        bridge = _name(params.get("bridge"), "bridge")
        ids = _vlans(params.get("vlans"))
        return [
            _op("/interface/bridge/port", "ensure", {"interface": target},
                {"bridge": bridge, "disabled": "no", "comment": comment}, "Trunk bridge member"),
            *[
                _op("/interface/bridge/vlan", "ensure",
                    {"bridge": bridge, "vlan_ids": str(v)},
                    {"tagged": f"{bridge},{target}", "comment": comment},
                    f"Tagged VLAN {v}")
                for v in ids
            ],
        ]

    if recipe == "wan_dhcp":
        return [
            _op("/interface/bridge/port", "remove_matching", {"interface": target}, {}, "Remove from bridge"),
            _op("/ip/dhcp-client", "ensure", {"interface": target},
                {"disabled": "no", "add_default_route": "yes", "use_peer_dns": "yes", "comment": comment},
                "WAN DHCP"),
            _op("/ip/firewall/nat", "ensure",
                {"chain": "srcnat", "action": "masquerade", "out_interface": target},
                {"comment": comment}, "WAN masquerade"),
        ]

    if recipe == "static_wan":
        address = _ip_interface(params.get("address"))
        gateway = _ip(params.get("gateway"), "gateway")
        return [
            _op("/interface/bridge/port", "remove_matching", {"interface": target}, {}, "Remove from bridge"),
            _op("/ip/address", "ensure", {"interface": target, "address": address},
                {"comment": comment}, "Static WAN address"),
            _op("/ip/route", "ensure", {"dst_address": "0.0.0.0/0", "gateway": gateway},
                {"comment": comment}, "Default route"),
            _op("/ip/firewall/nat", "ensure",
                {"chain": "srcnat", "action": "masquerade", "out_interface": target},
                {"comment": comment}, "WAN masquerade"),
        ]

    if recipe == "identity":
        identity = _name(params.get("identity"), "identity")
        return [_op("/system/identity", "singleton_set", {}, {"name": identity}, "Router identity")]

    if recipe == "dns":
        servers = str(params.get("servers") or "").replace(" ", "")
        ips = [_ip(x, "DNS server") for x in servers.split(",") if x]
        if not 1 <= len(ips) <= 4:
            raise ValueError("Enter between 1 and 4 DNS servers.")
        return [_op("/ip/dns", "singleton_set", {},
                    {"servers": ",".join(ips),
                     "allow_remote_requests": "yes" if params.get("allow_remote", "yes") == "yes" else "no"},
                    "DNS resolver")]

    if recipe == "ntp":
        return [_op("/system/ntp/client", "singleton_set", {},
                    {"enabled": "yes", "server_dns_names": "pool.ntp.org"}, "NTP client")]

    if recipe == "mgmt_firewall":
        subnet = _network(params.get("management_subnet"))
        return [
            _op("/ip/firewall/filter", "ensure",
                {"chain":"input","connection_state":"established,related","action":"accept"},
                {"comment":comment + ":estrel"}, "Allow established/related"),
            _op("/ip/firewall/filter", "ensure",
                {"chain":"input","connection_state":"invalid","action":"drop"},
                {"comment":comment + ":invalid"}, "Drop invalid"),
            _op("/ip/firewall/filter", "ensure",
                {"chain":"input","src_address":subnet,"action":"accept"},
                {"comment":comment + ":mgmt"}, "Allow management subnet"),
            _op("/ip/firewall/filter", "ensure",
                {"chain":"input","action":"drop","comment":comment + ":drop"},
                {}, "Final input drop"),
        ]

    raise ValueError("Recipe is not implemented.")


def _rid(row):
    return row.get("id") or row.get(".id")


def _rows(resource, find):
    try:
        return list(resource.get(**find)) if find else list(resource.get())
    except TypeError:
        return list(resource.get())


def snapshot_plan(service, plan):
    snap = []
    for op in plan:
        resource = service.resource(op["resource"])
        rows = _rows(resource, op["find"])
        # only the first 30 matching rows; recipes are deliberately narrow
        snap.append({
            "resource": op["resource"],
            "find": deepcopy(op["find"]),
            "rows": rows[:30],
            "label": op["label"],
        })
    return snap


def _apply_direct_op(service, op):
    resource = service.resource(op["resource"])
    rows = _rows(resource, op["find"])
    action = op["action"]
    values = dict(op["values"])

    if action == "set":
        if not rows:
            raise RuntimeError(f"{op['label']}: target does not exist.")
        rid = _rid(rows[0])
        if not rid:
            raise RuntimeError(f"{op['label']}: RouterOS item has no ID.")
        resource.set(id=rid, **values)
        return

    if action == "singleton_set":
        if not rows:
            raise RuntimeError(f"{op['label']}: RouterOS resource is unavailable.")
        rid = _rid(rows[0])
        if rid:
            resource.set(id=rid, **values)
        else:
            # some singleton RouterOS resources accept set without an item id
            resource.set(**values)
        return

    if action == "ensure":
        if rows:
            rid = _rid(rows[0])
            if rid:
                resource.set(id=rid, **values)
            else:
                resource.set(**values)
        else:
            resource.add(**op["find"], **values)
        return

    if action == "remove_matching":
        for row in rows:
            rid = _rid(row)
            if rid:
                resource.remove(id=rid)
        return

    raise RuntimeError("Unsupported configuration operation.")


def apply_direct(service, plan):
    for op in plan:
        _apply_direct_op(service, op)


def _rollback_from(before_state, plan):
    out = []
    for old, op in zip(before_state, plan):
        rows = old.get("rows") or []
        if not rows:
            # Item did not exist before. Remove the item matching the original selector
            # plus a TapTap comment when one was created.
            find = dict(op["find"])
            comment = op.get("values", {}).get("comment")
            if comment:
                find["comment"] = comment
            out.append(_op(op["resource"], "remove_matching", find, {}, "Remove created item"))
            continue

        # Restore every changed field on the first prior item.
        prior = rows[0]
        values = {}
        for key in op.get("values", {}):
            # routeros_api converts underscores/hyphens; snapshots normally expose hyphens
            hy = key.replace("_", "-")
            if key in prior:
                values[key] = prior[key]
            elif hy in prior:
                values[key] = prior[hy]
        if op["action"] == "remove_matching":
            # Re-create removed rows from their safe visible fields is risky and device-version
            # dependent, so the rollback preview explicitly reports manual restore required.
            out.append({"resource": op["resource"], "action": "manual_restore",
                        "find": op["find"], "values": {}, "label": "Manual restore required",
                        "rows": rows})
        elif values:
            out.append(_op(op["resource"], "set", op["find"], values, "Restore previous values"))
    return out


def _rosq(value):
    s = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{s}"'


def _selector(find):
    if not find:
        return ""
    bits = []
    for k, v in find.items():
        key = k.replace("_", "-")
        bits.append(f"{key}={_rosq(v)}")
    return " ".join(bits)


def link_script(plan):
    """Build RouterOS script from already validated catalogue operations."""
    chunks = []
    for op in plan:
        menu = op["resource"]
        sel = _selector(op["find"])
        vals = " ".join(f'{k.replace("_","-")}={_rosq(v)}' for k, v in op["values"].items())
        if op["action"] == "set":
            chunks.append(f':local x [{menu} find where {sel}]; :if ([:len $x]=0) do={{ :error "TapTap target missing" }}; {menu} set $x {vals}')
        elif op["action"] == "singleton_set":
            chunks.append(f"{menu} set {vals}")
        elif op["action"] == "ensure":
            chunks.append(f':local x [{menu} find where {sel}]; :if ([:len $x]=0) do={{ {menu} add {sel} {vals} }} else={{ {menu} set $x {vals} }}')
        elif op["action"] == "remove_matching":
            chunks.append(f':foreach x in=[{menu} find where {sel}] do={{ {menu} remove $x }}')
        else:
            raise ValueError("Unsupported Link operation.")
    script = "; ".join(chunks)
    if len(script) > 7500:
        raise ValueError("This configuration plan is too large for one TapTap Link operation.")
    return script



# ───────────────────────── Current configuration + TapTap Link safety ─────────────────────────

LINK_SENSITIVE_PATHS = (
    "/ip/route",
    "/ip/dns",
    "/ip/address",
    "/ip/dhcp-client",
    "/ip/firewall",
    "/interface",
    "/interface/bridge",
    "/interface/vlan",
    "/interface/wireguard",
    "/routing",
    "/system/script",
    "/system/scheduler",
    "/system/clock",
    "/system/ntp",
    "/ip/service",
    "/tool/netwatch",
)

LINK_CRITICAL_PATHS = (
    "/ip/route",
    "/ip/dns",
    "/ip/dhcp-client",
    "/ip/firewall",
    "/interface/wireguard",
    "/system/script",
    "/system/scheduler",
)


def _link_managed(router):
    """Whether TapTap Link/Tunnel is part of this router's management design."""
    if getattr(router, "connection_mode", "") == "agent":
        return True
    try:
        return hasattr(router, "agent")
    except Exception:
        return False


def _link_live_state(router):
    if not _link_managed(router):
        return {
            "managed": False,
            "mode": "Direct API",
            "online": None,
            "message": "This router is not currently registered as a TapTap Link router.",
        }
    try:
        from .linkops import uses_link
        if uses_link(router):
            try:
                from .linklive import link_state
                online, why = link_state(router)
            except Exception:
                online, why = None, "TapTap Link state is unavailable."
            return {
                "managed": True,
                "mode": "TapTap Link",
                "online": online,
                "message": why or ("TapTap Link is online." if online else "TapTap Link is not currently reporting."),
            }
        return {
            "managed": True,
            "mode": "TapTap Tunnel/API",
            "online": True,
            "message": "TapTap Tunnel/API is healthy; TapTap Link remains the recovery channel.",
        }
    except Exception:
        return {
            "managed": True,
            "mode": "TapTap Link/Tunnel",
            "online": None,
            "message": "TapTap management state could not be confirmed.",
        }


def _port_link_risk(router, target):
    if not target:
        return "", ""
    try:
        from .portctl import port_risk, RISK_TEXT
        risk = port_risk(router, target)
        return risk, RISK_TEXT.get(risk, "")
    except Exception:
        return "", ""


def advanced_link_impact(router, resource_path):
    """Safety classification for the raw/expert RouterOS editor."""
    path_value = str(resource_path or "").strip().lower().rstrip("/")
    if not _link_managed(router) or not path_value:
        return {"affects": False, "severity": "none", "reasons": []}

    matched = next(
        (prefix for prefix in LINK_SENSITIVE_PATHS
         if path_value == prefix or path_value.startswith(prefix + "/")),
        None,
    )
    if not matched:
        return {"affects": False, "severity": "none", "reasons": []}

    reasons = []
    severity = "critical" if any(
        path_value == prefix or path_value.startswith(prefix + "/")
        for prefix in LINK_CRITICAL_PATHS
    ) else "warning"

    if path_value.startswith("/system/script"):
        reasons.append(
            "RouterOS scripts include the TapTap Link bootstrap/poll script. "
            "Changing scripts can stop the router from checking in."
        )
    elif path_value.startswith("/system/scheduler"):
        reasons.append(
            "TapTap Link relies on a RouterOS scheduler entry. Changing schedulers can stop automatic check-ins."
        )
    elif path_value.startswith("/interface/wireguard"):
        reasons.append(
            "WireGuard carries the TapTap Tunnel. Changing it can remove the healthy tunnel and force Link recovery."
        )
    elif path_value.startswith("/ip/dns"):
        reasons.append(
            "TapTap Link uses the TapTap hostname over HTTPS. Bad DNS settings can prevent the router resolving TapTap."
        )
    elif path_value.startswith("/ip/route") or path_value.startswith("/ip/dhcp-client"):
        reasons.append(
            "This can change the router's Internet/default-route path used by TapTap Link."
        )
    elif path_value.startswith("/ip/firewall"):
        reasons.append(
            "Firewall/NAT changes can block outbound HTTPS, tunnel traffic, DNS, or the management return path."
        )
    elif path_value.startswith("/ip/address") or path_value.startswith("/interface"):
        reasons.append(
            "Interface/address changes can alter the physical or logical path that carries TapTap Link."
        )
    elif path_value.startswith("/system/ntp") or path_value.startswith("/system/clock"):
        reasons.append(
            "TapTap Link uses HTTPS certificate validation; a wrong router clock can break trusted HTTPS."
        )
    elif path_value.startswith("/ip/service"):
        reasons.append(
            "API service changes can affect TapTap Tunnel/API management even if the outbound Link poll remains available."
        )
    else:
        reasons.append("This RouterOS area can affect TapTap's management connectivity.")

    return {
        "affects": True,
        "severity": severity,
        "title": "This change may affect TapTap Link",
        "reasons": reasons,
        "confirm_word": "LINK",
    }


def link_impact(router, recipe, target, params, plan):
    """Explain whether a visual recipe can interrupt TapTap Link/Tunnel."""
    if not _link_managed(router):
        return {"affects": False, "severity": "none", "reasons": []}

    reasons = []
    severity = "warning"
    port_risk, port_risk_text = _port_link_risk(router, target)

    if port_risk:
        severity = "critical"
        reasons.append(port_risk_text)

    if recipe in {"wan_dhcp", "static_wan"}:
        severity = "critical"
        reasons.append(
            "This recipe changes the WAN/default-route/NAT path. TapTap Link needs working Internet access to keep polling."
        )

    if recipe == "dns":
        severity = "critical"
        reasons.append(
            "TapTap Link connects to TapTap by hostname over HTTPS. Incorrect DNS servers can stop Link check-ins."
        )

    if recipe == "mgmt_firewall":
        severity = "critical"
        reasons.append(
            "Firewall changes can block TapTap Link/Tunnel/API traffic or lock out management if the rules are wrong."
        )

    if recipe == "ntp":
        reasons.append(
            "TapTap Link uses HTTPS trust. Incorrect time/NTP can make certificates appear invalid."
        )

    if recipe == "port_enabled":
        disable = any(
            op.get("resource") == "/interface"
            and str((op.get("values") or {}).get("disabled", "")).lower() == "yes"
            for op in plan
        )
        if disable and port_risk:
            severity = "critical"
            reasons.append(
                "Disabling this port can immediately remove the physical path TapTap uses."
            )

    if recipe in {"bridge_member", "access_vlan", "trunk_vlan"} and port_risk:
        severity = "critical"
        reasons.append(
            "Changing bridge/VLAN membership on this protected port can move TapTap traffic into the wrong Layer-2 network."
        )

    # Defence-in-depth for genuinely global management resources.
    # Interface/bridge recipes are handled by the target-port risk check above,
    # otherwise every harmless VLAN on an unrelated LAN port would show a false warning.
    global_sensitive = (
        "/ip/route",
        "/ip/dns",
        "/ip/dhcp-client",
        "/ip/firewall",
        "/interface/wireguard",
        "/system/script",
        "/system/scheduler",
        "/system/clock",
        "/system/ntp",
        "/ip/service",
    )
    for op in plan:
        path_value = str(op.get("resource") or "")
        if not any(
            path_value == prefix or path_value.startswith(prefix + "/")
            for prefix in global_sensitive
        ):
            continue
        raw = advanced_link_impact(router, path_value)
        if raw["affects"]:
            for reason in raw["reasons"]:
                if reason not in reasons:
                    reasons.append(reason)
            if raw["severity"] == "critical":
                severity = "critical"

    return {
        "affects": bool(reasons),
        "severity": severity if reasons else "none",
        "title": "TapTap Link safety warning",
        "reasons": reasons,
        "confirm_word": "LINK",
        "backup_recommended": bool(reasons),
    }


def _snapshot(router):
    try:
        return router.config_snapshot
    except Exception:
        return None


def _redact(value):
    try:
        from .mikrotik import redact
        return redact(value)
    except Exception:
        return value


def _clean_row(row):
    hidden = {"id", ".id", ".nextid", "nextid"}
    return {
        str(k).lstrip("."): v
        for k, v in (row or {}).items()
        if k not in hidden and v not in (None, "")
    }


def _section_rows(snapshot, *needles):
    if not snapshot or not snapshot.sections:
        return []
    needles = tuple(str(x).lower() for x in needles)
    for label, section in snapshot.sections.items():
        low = str(label).lower()
        if any(n in low for n in needles):
            return list((section or {}).get("rows") or [])
    return []


def _latest_recipe_parameters(router, scope, target):
    rows = (
        router.visual_config_deployments
        .filter(status="success", scope=scope, target=target if scope == "port" else "")
        .order_by("-applied_at", "-created_at")
    )
    out = {}
    for row in rows:
        out.setdefault(row.recipe, row.parameters or {})
    return out


def _port_adjustments(router, obj, raw, bridge_port, related):
    current = _latest_recipe_parameters(router, "port", obj.name)
    bridge = str((bridge_port or {}).get("bridge") or "")
    pvid = str((bridge_port or {}).get("pvid") or "1")

    # These defaults come from the router inventory itself, not deployment history.
    current["port_enabled"] = {
        "enabled": "no" if obj.disabled else "yes",
    }
    if bridge:
        current["bridge_member"] = {"bridge": bridge}
        if pvid and pvid != "1":
            current["access_vlan"] = {"bridge": bridge, "vlan": pvid}

    trunk = []
    for section in related:
        if section.get("title") != "Bridge VLAN table":
            continue
        for row in section.get("rows") or []:
            tagged = {
                x.strip()
                for x in str(row.get("tagged") or row.get("current-tagged") or "").split(",")
                if x.strip()
            }
            if obj.name in tagged:
                vlan = row.get("vlan-ids") or row.get("vlan_ids")
                if vlan:
                    trunk.extend(str(vlan).split(","))
    if trunk:
        current["trunk_vlan"] = {
            "bridge": bridge or "bridge1",
            "vlans": ",".join(dict.fromkeys(x.strip() for x in trunk if x.strip())),
        }

    return [
        {
            "recipe": key,
            "label": CATALOG[key]["label"],
            "risk": CATALOG[key]["risk"],
            "parameters": current.get(key, {}),
        }
        for key in ("port_enabled", "bridge_member", "access_vlan", "trunk_vlan", "wan_dhcp", "static_wan")
    ]


def _router_adjustments(router, snapshot):
    current = _latest_recipe_parameters(router, "router", "")

    identity_rows = _section_rows(snapshot, "identity")
    if identity_rows:
        name = identity_rows[0].get("name")
        if name:
            current["identity"] = {"identity": name}

    dns_rows = _section_rows(snapshot, "dns")
    if dns_rows:
        row = dns_rows[0]
        current["dns"] = {
            "servers": row.get("servers", ""),
            "allow_remote": "yes"
            if str(row.get("allow-remote-requests", row.get("allow_remote_requests", ""))).lower()
            in {"yes", "true", "1"}
            else "no",
        }

    return [
        {
            "recipe": key,
            "label": CATALOG[key]["label"],
            "risk": CATALOG[key]["risk"],
            "parameters": current.get(key, {}),
        }
        for key in ("identity", "dns", "ntp", "mgmt_firewall")
    ]


@login_required
@require_POST
def designer_refresh(request, pk):
    """Refresh the configuration source used by the click inspector."""
    router = _router(request, pk)
    try:
        from .linkops import uses_link
        if uses_link(router):
            from .linkops import refresh
            created = refresh(router, request.user)
            return JsonResponse(
                {
                    "success": True,
                    "queued": True,
                    "message": (
                        "Collecting the latest configuration through TapTap Link. "
                        "The inspector will update as the router sends the sections."
                        if created
                        else "A TapTap Link configuration collection is already running."
                    ),
                }
            )

        from .mikrotik import MikroTikService
        from .models import RouterConfigSnapshot

        svc = MikroTikService(router).connect()
        try:
            cfg = svc.configuration_snapshot()
        finally:
            svc.close()

        snapshot, _ = RouterConfigSnapshot.objects.update_or_create(
            router=router,
            defaults={
                "sections": cfg["sections"],
                "load_balancing": cfg["load_balancing"],
                "captured_at": cfg["captured_at"],
            },
        )
        return JsonResponse(
            {
                "success": True,
                "queued": False,
                "message": "Current RouterOS configuration refreshed.",
                "captured_at": snapshot.captured_at.isoformat(),
            }
        )
    except Exception as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)[:300]},
            status=502,
        )


@login_required
@require_GET
def designer_inspect(request, pk):
    """Click a port/router and see the latest captured configuration + editable recipes."""
    router = _router(request, pk)
    scope = str(request.GET.get("scope") or "router")
    target = str(request.GET.get("target") or "").strip()
    snap = _snapshot(router)
    captured_at = getattr(snap, "captured_at", None)

    if scope == "port":
        if not target:
            return JsonResponse({"success": False, "message": "Choose a port."}, status=400)
        obj = router.interfaces.filter(name=target).first()
        if not obj:
            return JsonResponse(
                {"success": False, "message": f"{target} has not been discovered. Run Full Sync first."},
                status=404,
            )
        raw = obj.raw_data or {}
        iface = _clean_row({k: v for k, v in raw.items() if k not in {"ethernet", "bridge_port"}})
        ethernet = _clean_row(raw.get("ethernet") or {})
        bridge_port = _clean_row(raw.get("bridge_port") or {})

        try:
            from .views_ports import _related_config
            related = _related_config(snap, target, bridge_port.get("bridge", ""))
        except Exception:
            related = []

        sections = []
        for title, rows in (
            ("Interface", [iface]),
            ("Ethernet", [ethernet] if ethernet else []),
            ("Bridge port", [bridge_port] if bridge_port else []),
        ):
            if rows:
                sections.append({"title": title, "rows": _redact(rows)})
        sections.extend(_redact(related))

        risk, risk_text = _port_link_risk(router, target)
        return JsonResponse(
            {
                "success": True,
                "scope": "port",
                "target": target,
                "title": target,
                "source": "Latest TapTap router inventory / configuration snapshot",
                "captured_at": captured_at.isoformat() if captured_at else None,
                "summary": {
                    "type": obj.interface_type,
                    "mac": obj.mac_address,
                    "mtu": obj.mtu,
                    "running": obj.running,
                    "disabled": obj.disabled,
                    "bridge": bridge_port.get("bridge", ""),
                    "pvid": bridge_port.get("pvid", ""),
                },
                "sections": sections,
                "adjustments": _port_adjustments(router, obj, raw, bridge_port, related),
                "link": {
                    **_link_live_state(router),
                    "target_risk": risk,
                    "target_warning": risk_text,
                },
            }
        )

    # Whole-router inspector. Show a bounded but broad current snapshot.
    sections = []
    if snap and snap.sections:
        for label, section in snap.sections.items():
            rows = list((section or {}).get("rows") or [])
            count = int((section or {}).get("count") or len(rows))
            sections.append(
                {
                    "title": str(label),
                    "count": count,
                    "rows": _redact([_clean_row(row) for row in rows[:12]]),
                    "truncated": count > 12,
                    "path": (section or {}).get("path", ""),
                }
            )

    return JsonResponse(
        {
            "success": True,
            "scope": "router",
            "target": "",
            "title": router.name,
            "source": "Latest TapTap configuration snapshot",
            "captured_at": captured_at.isoformat() if captured_at else None,
            "summary": {
                "status": router.status,
                "connection_mode": router.connection_mode,
                "ip_address": router.ip_address,
                "api_port": router.api_port,
                "interfaces": router.interfaces.count(),
            },
            "sections": sections,
            "adjustments": _router_adjustments(router, snap),
            "link": _link_live_state(router),
        }
    )


def _backup_before_change(router, user):
    """Create/queue the existing TapTap full backup before a risky visual change."""
    from .linkops import uses_link
    if uses_link(router):
        from . import agent
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", f"taptap-{router.name}-{stamp}")[:60]
        cmd = agent.queue(
            router,
            "backup",
            {"file": safe},
            label="Pre-change full configuration backup",
            user=user,
            minutes=30,
        )
        return {
            "transport": "TapTap Link",
            "queued": True,
            "message": "Full RouterOS backup/export queued before the configuration change.",
            "command_id": cmd.pk,
        }

    from .mikrotik import MikroTikService
    from .portctl import backup
    svc = MikroTikService(router).connect()
    try:
        record = backup(svc, router, user=user)
    finally:
        svc.close()
    return {
        "transport": "TapTap Tunnel/API" if router.connection_mode == "agent" else "Direct API",
        "queued": False,
        "message": f"Full backup {record.name} created before the configuration change.",
        "backup_id": record.pk,
    }


def _advanced_guard_wrapper(original):
    @functools.wraps(original)
    def guarded(request, pk, *args, **kwargs):
        if request.method == "POST":
            router = _router(request, pk)
            impact = advanced_link_impact(router, request.POST.get("resource_path"))
            if impact["affects"] and request.POST.get("link_confirm") != "LINK":
                messages.error(
                    request,
                    "TapTap Link safety guard: this RouterOS area can affect TapTap connectivity. "
                    "Review the warning and type LINK before applying it.",
                )
                return redirect("router_control", pk=pk)
        return original(request, pk, *args, **kwargs)

    guarded._taptap_link_guard = True
    return guarded


def _role_guard_wrapper(original):
    @functools.wraps(original)
    def guarded(request, pk, *args, **kwargs):
        if request.method == "POST":
            router = _router(request, pk)
            interface = str(request.POST.get("interface_name") or "").strip()
            role = str(request.POST.get("role") or "").strip()
            applying = request.POST.get("apply") == "yes"
            risk, risk_text = _port_link_risk(router, interface)
            role_sensitive = role in {"wan", "disabled", "trunk"}

            if (
                _link_managed(router)
                and applying
                and (risk or role_sensitive)
                and request.POST.get("link_confirm") != "LINK"
            ):
                reasons = []
                if risk_text:
                    reasons.append(risk_text)
                if role_sensitive:
                    reasons.append(
                        f"Changing {interface} to the {role} role can change bridge/WAN behavior "
                        "and may interrupt the path used by TapTap Link."
                    )
                return JsonResponse(
                    {
                        "success": False,
                        "needs_link_confirm": True,
                        "message": " ".join(reasons),
                        "confirm_word": "LINK",
                    },
                    status=409,
                )
        return original(request, pk, *args, **kwargs)

    guarded._taptap_link_role_guard = True
    return guarded


def _install_advanced_guard():
    """Protect existing expert editor and Port Role Studio as well."""
    from . import urls

    for index, pattern_obj in enumerate(list(urls.urlpatterns)):
        name = getattr(pattern_obj, "name", None)

        if name == "router_config_apply":
            original = pattern_obj.callback
            if not getattr(original, "_taptap_link_guard", False):
                urls.urlpatterns[index] = path(
                    "routers/<int:pk>/control/apply/",
                    _advanced_guard_wrapper(original),
                    name="router_config_apply",
                )

        elif name == "router_interface_role":
            original = pattern_obj.callback
            if not getattr(original, "_taptap_link_role_guard", False):
                urls.urlpatterns[index] = path(
                    "routers/<int:pk>/control/interface-role/",
                    _role_guard_wrapper(original),
                    name="router_interface_role",
                )


def _serialize_catalog():
    return [{"key": key, **meta} for key, meta in CATALOG.items()]


@login_required
@require_GET
def designer_state(request, pk):
    router = _router(request, pk)
    history = []
    qs = router.visual_config_deployments.select_related("actor").all()[:60]
    for d in qs:
        history.append({
            "id": d.pk, "recipe": d.recipe, "label": d.recipe_label,
            "scope": d.scope, "target": d.target, "parameters": d.parameters,
            "status": d.status, "risk": d.risk, "transport": d.transport,
            "actor": (d.actor.get_full_name() or d.actor.username) if d.actor else "TapTap",
            "created_at": d.created_at.isoformat(),
            "applied_at": d.applied_at.isoformat() if d.applied_at else None,
            "error": d.error[:220],
            "can_rollback": d.status == "success" and bool(d.rollback_plan),
        })
    active = [
        x for x in history
        if x["status"] == "success"
    ]
    return JsonResponse({
        "success": True,
        "router": {"id": router.pk, "name": router.name, "status": router.status,
                   "connection_mode": router.connection_mode},
        "catalog": _serialize_catalog(),
        "history": history,
        "active": active,
    })


@login_required
@require_POST
def designer_preview(request, pk):
    router = _router(request, pk)
    try:
        data = json.loads(request.body or b"{}")
        recipe = str(data.get("recipe") or "")
        target = str(data.get("target") or "")
        params = data.get("parameters") or {}
        meta = CATALOG[recipe]
        plan = build_plan(recipe, target, params)
        impact = link_impact(router, recipe, target, params, plan)
        return JsonResponse({
            "success": True,
            "recipe": recipe, "label": meta["label"], "risk": meta["risk"],
            "scope": meta["scope"], "target": target, "plan": plan,
            "link_impact": impact,
        })
    except Exception as exc:
        return JsonResponse({"success": False, "message": str(exc)}, status=400)


@login_required
@require_POST
def designer_apply(request, pk):
    router = _router(request, pk)
    try:
        data = json.loads(request.body or b"{}")
        recipe = str(data.get("recipe") or "")
        target = str(data.get("target") or "")
        params = data.get("parameters") or {}
        meta = CATALOG[recipe]
        if meta["risk"] == "high" and str(data.get("confirm") or "") != "APPLY":
            raise ValueError("High-risk configuration requires typing APPLY.")
        plan = build_plan(recipe, target, params)
        impact = link_impact(router, recipe, target, params, plan)
        if impact["affects"] and str(data.get("link_confirm") or "") != "LINK":
            raise ValueError(
                "TapTap Link safety guard: this change can affect TapTap connectivity. "
                "Review the warning and type LINK to confirm."
            )

        backup_result = None
        if bool(data.get("backup_before")):
            backup_result = _backup_before_change(router, request.user)

        d = RouterVisualDeployment.objects.create(
            business=router.business, router=router, actor=request.user,
            scope=meta["scope"], target=target if meta["scope"] == "port" else "",
            recipe=recipe, recipe_label=meta["label"], risk=meta["risk"],
            parameters=params, plan=plan, status="draft",
        )

        from .linkops import uses_link
        if uses_link(router):
            from .agent import queue
            cmd = queue(router, "control_designer_apply",
                        {"deployment_id": d.pk}, label=f"Visual config: {meta['label']}",
                        user=request.user, minutes=60)
            d.status = "queued"
            d.transport = "TapTap Link"
            d.agent_command_id = cmd.pk
            d.save(update_fields=["status","transport","agent_command_id"])
            return JsonResponse({"success": True, "queued": True, "deployment_id": d.pk, "link_impact": impact, "backup": backup_result})

        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            before = snapshot_plan(svc, plan)
            rollback = _rollback_from(before, plan)
            d.before_state = before
            d.rollback_plan = rollback
            d.save(update_fields=["before_state","rollback_plan"])
            apply_direct(svc, plan)
            after = snapshot_plan(svc, plan)
        finally:
            svc.close()

        d.after_state = after
        d.status = "success"
        d.transport = "TapTap Tunnel/API" if router.connection_mode == "agent" else "Direct API"
        d.applied_at = timezone.now()
        d.save(update_fields=["after_state","status","transport","applied_at"])
        return JsonResponse({"success": True, "queued": False, "deployment_id": d.pk, "link_impact": impact, "backup": backup_result})
    except Exception as exc:
        try:
            if "d" in locals():
                d.status = "failed"; d.error = str(exc)[:1000]
                d.save(update_fields=["status","error"])
        except Exception:
            pass
        return JsonResponse({"success": False, "message": str(exc)}, status=400)


@login_required
@require_POST
def designer_rollback(request, pk, deployment_id):
    router = _router(request, pk)
    d = get_object_or_404(RouterVisualDeployment, pk=deployment_id, router=router, business=router.business)
    if d.status != "success":
        return JsonResponse({"success": False, "message": "Only successfully applied configurations can be rolled back."}, status=400)
    if not d.rollback_plan:
        return JsonResponse({"success": False, "message": "No automatic rollback is available for this change."}, status=400)
    if any(x.get("action") == "manual_restore" for x in d.rollback_plan):
        return JsonResponse({"success": False, "message": "This change removed pre-existing RouterOS rows. Use the stored Before state for manual restoration."}, status=400)

    try:
        from .linkops import uses_link
        if uses_link(router):
            from .agent import queue
            cmd = queue(router, "control_designer_rollback",
                        {"deployment_id": d.pk}, label=f"Rollback visual config: {d.recipe_label}",
                        user=request.user, minutes=60)
            d.transport = "TapTap Link rollback queued"
            d.agent_command_id = cmd.pk
            d.save(update_fields=["transport","agent_command_id"])
            return JsonResponse({"success": True, "queued": True})

        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            apply_direct(svc, d.rollback_plan)
        finally:
            svc.close()
        d.status = "rolled_back"
        d.rolled_back_at = timezone.now()
        d.save(update_fields=["status","rolled_back_at"])
        return JsonResponse({"success": True, "queued": False})
    except Exception as exc:
        return JsonResponse({"success": False, "message": str(exc)}, status=400)


def _install_agent():
    from . import agent
    if getattr(agent, "_control_designer_installed", False):
        return
    agent.SAFE_KINDS.update({"control_designer_apply", "control_designer_rollback"})
    agent.DEFAULT_EXPIRY.setdefault("control_designer_apply", 60)
    agent.DEFAULT_EXPIRY.setdefault("control_designer_rollback", 60)

    original_body = agent.command_body
    @functools.wraps(original_body)
    def command_body(cmd):
        if cmd.kind in {"control_designer_apply", "control_designer_rollback"}:
            did = int((cmd.params or {}).get("deployment_id") or 0)
            d = RouterVisualDeployment.objects.filter(pk=did, router=cmd.router).first()
            if not d:
                raise ValueError("Visual deployment no longer exists.")
            plan = d.plan if cmd.kind == "control_designer_apply" else d.rollback_plan
            if not plan or any(x.get("action") == "manual_restore" for x in plan):
                raise ValueError("This visual deployment cannot be applied automatically.")
            return link_script(plan)
        return original_body(cmd)
    agent.command_body = command_body

    original_ack = agent.handle_ack
    @functools.wraps(original_ack)
    def handle_ack(cmd_id, given_nonce, status, result=""):
        ok = original_ack(cmd_id, given_nonce, status, result)
        if not ok:
            return ok
        try:
            from .models import AgentCommand
            cmd = AgentCommand.objects.filter(pk=cmd_id).first()
            if cmd and cmd.kind in {"control_designer_apply","control_designer_rollback"}:
                d = RouterVisualDeployment.objects.filter(pk=(cmd.params or {}).get("deployment_id"), router=cmd.router).first()
                if d:
                    if status == "ok":
                        if cmd.kind == "control_designer_apply":
                            d.status = "success"; d.applied_at = timezone.now()
                            d.save(update_fields=["status","applied_at"])
                        else:
                            d.status = "rolled_back"; d.rolled_back_at = timezone.now()
                            d.save(update_fields=["status","rolled_back_at"])
                    else:
                        d.status = "failed"; d.error = (result or "Router rejected the configuration")[:1000]
                        d.save(update_fields=["status","error"])
        except Exception:
            pass
        return ok
    agent.handle_ack = handle_ack
    agent._control_designer_installed = True


def _install_urls():
    from . import urls
    names = {getattr(x, "name", None) for x in urls.urlpatterns}
    add = []
    if "control_designer_state" not in names:
        add.extend([
            path("routers/<int:pk>/control/designer/state/", designer_state, name="control_designer_state"),
            path("routers/<int:pk>/control/designer/refresh/", designer_refresh, name="control_designer_refresh"),
            path("routers/<int:pk>/control/designer/inspect/", designer_inspect, name="control_designer_inspect"),
            path("routers/<int:pk>/control/designer/preview/", designer_preview, name="control_designer_preview"),
            path("routers/<int:pk>/control/designer/apply/", designer_apply, name="control_designer_apply"),
            path("routers/<int:pk>/control/designer/<int:deployment_id>/rollback/", designer_rollback, name="control_designer_rollback"),
        ])
    urls.urlpatterns.extend(add)


def install():
    from . import permissions
    for name in (
        "control_designer_state",
        "control_designer_refresh",
        "control_designer_inspect",
        "control_designer_preview",
        "control_designer_apply",
        "control_designer_rollback",
    ):
        permissions.URL_PERMS[name] = "network.manage"

    _install_agent()
    _install_urls()
    _install_advanced_guard()
