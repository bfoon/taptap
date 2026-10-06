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

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
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
        return JsonResponse({
            "success": True,
            "recipe": recipe, "label": meta["label"], "risk": meta["risk"],
            "scope": meta["scope"], "target": target, "plan": plan,
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
            return JsonResponse({"success": True, "queued": True, "deployment_id": d.pk})

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
        return JsonResponse({"success": True, "queued": False, "deployment_id": d.pk})
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
            path("routers/<int:pk>/control/designer/preview/", designer_preview, name="control_designer_preview"),
            path("routers/<int:pk>/control/designer/apply/", designer_apply, name="control_designer_apply"),
            path("routers/<int:pk>/control/designer/<int:deployment_id>/rollback/", designer_rollback, name="control_designer_rollback"),
        ])
    urls.urlpatterns.extend(add)


def install():
    _install_agent()
    _install_urls()
