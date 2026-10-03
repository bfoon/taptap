"""TapTap Link sync reliability improvements.

Fixes:
- Binding ACKs now update SyncedIPBinding immediately.
- Duplicate unsent binding commands for the same MAC are coalesced/cancelled.
- Binding commands are prioritised with voucher commands.
- "Queued" bindings are reconciled against the real AgentCommand state.
- Failed/expired binding commands get a bounded self-heal retry.
- TapTap-authored bindings missing from an inventory snapshot are re-pushed.
- Failed full Link syncs get a bounded early retry instead of waiting for the
  normal periodic sync window.

This module deliberately patches the existing Link pipeline at startup instead
of replacing the very large core/agent.py and core/agent_inventory.py files.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("taptap.link.reliability")

BINDING_KINDS = ("binding_upsert", "binding_set", "binding_remove")
MAX_HEALS_PER_PASS = 12
HEAL_COOLDOWN_SECONDS = 90
SYNC_RETRY_SECONDS = 300


def _norm_mac(value):
    raw = "".join(c for c in str(value or "").upper() if c in "0123456789ABCDEF")
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2)) if len(raw) == 12 else ""


def _cmd_mac(cmd):
    return _norm_mac((cmd.params or {}).get("mac"))


def _binding_queryset(router, mac):
    from .models import SyncedIPBinding
    return SyncedIPBinding.objects.filter(router=router, mac_address__iexact=mac)


def _active_commands(router, mac):
    from .models import AgentCommand
    return AgentCommand.objects.filter(
        router=router,
        kind__in=BINDING_KINDS,
        status__in=["queued", "sent"],
    ).order_by("created_at", "pk")


def _all_commands(router, mac):
    from .models import AgentCommand
    rows = []
    for cmd in AgentCommand.objects.filter(
        router=router,
        kind__in=BINDING_KINDS,
    ).order_by("-created_at", "-pk")[:300]:
        if _cmd_mac(cmd) == mac:
            rows.append(cmd)
    return rows


def _cancel_older_queued(router, mac, keep_id=None):
    """Cancel obsolete *unsent* binding commands for one MAC.

    Sent commands are intentionally never cancelled because the router may
    already be executing them. The newest desired state will run after them.
    """
    from .models import AgentCommand

    ids = []
    for cmd in AgentCommand.objects.filter(
        router=router,
        kind__in=BINDING_KINDS,
        status="queued",
    ).order_by("created_at", "pk"):
        if keep_id and cmd.pk == keep_id:
            continue
        if _cmd_mac(cmd) == mac:
            ids.append(cmd.pk)

    if ids:
        AgentCommand.objects.filter(pk__in=ids).update(
            status="cancelled",
            done_at=timezone.now(),
            result="Superseded by a newer binding state for the same MAC",
        )
    return len(ids)


def _desired_upsert_params(binding):
    return {
        "mac": _norm_mac(binding.mac_address),
        "type": binding.binding_type or "bypassed",
        "address": binding.address or "",
        "server": binding.server or "all",
        "comment": binding.comment or "TapTap",
        "disabled": bool(binding.disabled),
    }


def _mark_from_command(cmd, ok):
    """Reflect one binding ACK back into the TapTap mirror immediately.

    If a newer command for the same MAC is still queued/sent, do not let an
    older ACK overwrite the newer desired state kept in TapTap.
    """
    mac = _cmd_mac(cmd)
    if not mac:
        return

    qs = _binding_queryset(cmd.router, mac)

    newer_active = False
    for other in _active_commands(cmd.router, mac):
        if other.pk != cmd.pk and other.created_at >= cmd.created_at and _cmd_mac(other) == mac:
            newer_active = True
            break

    if newer_active:
        qs.update(sync_status="Queued", sync_error="")
        return

    if cmd.kind == "binding_remove":
        # Delete actions already remove the TapTap row optimistically.
        # If one still exists, mark it absent on success or failed on failure.
        if ok:
            qs.update(
                is_present=False,
                sync_status="Synced",
                sync_error="",
                last_seen_at=timezone.now(),
            )
        else:
            qs.update(
                sync_status="Error",
                sync_error=(cmd.result or "Router rejected binding removal")[:500],
            )
        return

    if ok:
        updates = {
            "sync_status": "Synced",
            "sync_error": "",
            "is_present": True,
            "last_seen_at": timezone.now(),
        }
        if cmd.kind == "binding_set":
            enabled = bool((cmd.params or {}).get("enabled", True))
            updates["disabled"] = not enabled
        elif cmd.kind == "binding_upsert":
            p = cmd.params or {}
            updates.update(
                disabled=bool(p.get("disabled", False)),
                binding_type=str(p.get("type") or "bypassed"),
                comment=str(p.get("comment") or "")[:255],
                address=str(p.get("address") or "")[:120],
                server=str(p.get("server") or "all")[:120],
            )
        qs.update(**updates)
    else:
        qs.update(
            sync_status="Error",
            sync_error=(cmd.result or "Router rejected binding command")[:500],
        )


def _reconcile_rows(router, heal=True):
    """Repair stale Queued/Error binding states from command history."""
    from .models import SyncedIPBinding

    rows = list(
        SyncedIPBinding.objects.filter(router=router)
        .exclude(mac_address="")
        .filter(sync_status__in=["Queued", "Pending", "Error"])
        .order_by("updated_at", "pk")[:300]
    )
    if not rows:
        return 0

    healed = 0
    now = timezone.now()

    for row in rows:
        mac = _norm_mac(row.mac_address)
        if not mac:
            continue

        commands = _all_commands(router, mac)
        latest = commands[0] if commands else None

        if latest and latest.status in ("queued", "sent"):
            if row.sync_status != "Queued":
                SyncedIPBinding.objects.filter(pk=row.pk).update(
                    sync_status="Queued",
                    sync_error="",
                )
            continue

        if latest and latest.status == "done":
            SyncedIPBinding.objects.filter(pk=row.pk).update(
                sync_status="Synced",
                sync_error="",
                is_present=True,
                last_seen_at=now,
            )
            continue

        if latest and latest.status in ("failed", "expired", "cancelled"):
            # "cancelled" because a newer command superseded it is harmless if
            # another active/newer command exists, which was handled above.
            reason = latest.result or latest.status.capitalize()
            SyncedIPBinding.objects.filter(pk=row.pk).update(
                sync_status="Error",
                sync_error=reason[:500],
            )

        if not heal:
            continue

        # Self-heal only the current desired TapTap mirror. The cache prevents
        # hammering a router that is genuinely having trouble.
        if healed >= MAX_HEALS_PER_PASS:
            break
        if not row.is_present and row.source != "taptap":
            continue
        if not cache.add(
            f"tt:binding-heal:{router.pk}:{row.pk}",
            1,
            HEAL_COOLDOWN_SECONDS,
        ):
            continue

        # Limit repeated repair attempts to a small number per hour.
        attempts_key = f"tt:binding-heal-count:{router.pk}:{row.pk}"
        attempts = int(cache.get(attempts_key) or 0)
        if attempts >= 4:
            continue

        try:
            from . import agent as link

            params = _desired_upsert_params(row)
            if not params["mac"]:
                continue

            link.queue(
                router,
                "binding_upsert",
                params,
                label=f"Repair binding {row.comment or params['mac']}",
                minutes=60,
            )
            cache.set(attempts_key, attempts + 1, 3600)
            SyncedIPBinding.objects.filter(pk=row.pk).update(
                sync_status="Queued",
                sync_error="",
            )
            healed += 1
        except Exception as exc:
            SyncedIPBinding.objects.filter(pk=row.pk).update(
                sync_status="Error",
                sync_error=str(exc)[:500],
            )

    return healed


def _heal_missing_after_inventory(router):
    """Re-push TapTap-authored bindings that disappeared from RouterOS."""
    from .models import SyncedIPBinding

    missing = list(
        SyncedIPBinding.objects.filter(
            router=router,
            source="taptap",
            is_present=False,
        )
        .exclude(mac_address="")
        .order_by("updated_at", "pk")[:MAX_HEALS_PER_PASS]
    )

    count = 0
    for row in missing:
        if not cache.add(
            f"tt:binding-missing-heal:{router.pk}:{row.pk}",
            1,
            HEAL_COOLDOWN_SECONDS,
        ):
            continue

        mac = _norm_mac(row.mac_address)
        if not mac:
            continue

        active = False
        for cmd in _active_commands(router, mac):
            if _cmd_mac(cmd) == mac:
                active = True
                break
        if active:
            continue

        try:
            from . import agent as link

            link.queue(
                router,
                "binding_upsert",
                _desired_upsert_params(row),
                label=f"Restore missing binding {row.comment or mac}",
                minutes=60,
            )
            SyncedIPBinding.objects.filter(pk=row.pk).update(
                sync_status="Queued",
                sync_error="",
            )
            count += 1
        except Exception as exc:
            SyncedIPBinding.objects.filter(pk=row.pk).update(
                sync_status="Error",
                sync_error=str(exc)[:500],
            )

    return count


def install():
    from . import agent as link
    from . import agent_inventory as inventory

    if getattr(link, "_taptap_sync_reliability_installed", False):
        return

    # Binding changes are customer-facing just like voucher changes.
    binding_priority = tuple(k for k in BINDING_KINDS if k not in link.VOUCHER_KINDS)
    if binding_priority:
        link.VOUCHER_KINDS = tuple(link.VOUCHER_KINDS) + binding_priority

    original_queue = link.queue
    original_handle_ack = link.handle_ack
    original_build_response = link.build_response
    original_handle_poll = link.handle_poll
    original_bindings = inventory._bindings
    original_auto_sync = link.auto_sync

    def reliable_queue(router, kind, params=None, label="", user=None, minutes=None):
        params = dict(params or {})

        if kind in BINDING_KINDS:
            mac = _norm_mac(params.get("mac"))
            if mac:
                params["mac"] = mac

                # If an identical active command already exists, reuse it.
                for cmd in _active_commands(router, mac):
                    if (
                        _cmd_mac(cmd) == mac
                        and cmd.kind == kind
                        and dict(cmd.params or {}) == params
                    ):
                        return cmd

                # The newest unsent desired state wins.
                _cancel_older_queued(router, mac)

        return original_queue(
            router,
            kind,
            params,
            label=label,
            user=user,
            minutes=minutes,
        )

    def reliable_handle_ack(cmd_id, given_nonce, status, result=""):
        from .models import AgentCommand

        cmd = (
            AgentCommand.objects.select_related("router__business")
            .filter(pk=cmd_id)
            .first()
        )
        outcome = original_handle_ack(cmd_id, given_nonce, status, result)

        if outcome and cmd and cmd.kind in BINDING_KINDS:
            cmd.refresh_from_db()
            _mark_from_command(cmd, cmd.status == "done")
            mac = _cmd_mac(cmd)
            if mac:
                # Clear the bounded self-heal counter after a successful ACK.
                if cmd.status == "done":
                    for row in _binding_queryset(cmd.router, mac):
                        cache.delete(
                            f"tt:binding-heal-count:{cmd.router_id}:{row.pk}"
                        )

        return outcome

    def reliable_build_response(router, url):
        text = original_build_response(router, url)
        try:
            _reconcile_rows(router, heal=True)
        except Exception:
            logger.exception("Binding queue reconciliation failed for %s", router)
        return text

    def reliable_handle_poll(agent, data, ip, url):
        result = original_handle_poll(agent, data, ip, url)
        try:
            _reconcile_rows(agent.router, heal=True)
        except Exception:
            logger.exception("Binding poll reconciliation failed for %s", agent.router)
        return result

    def reliable_bindings(router, rows, now):
        summary = original_bindings(router, rows, now)
        try:
            restored = _heal_missing_after_inventory(router)
            if restored:
                summary["bindings_requeued"] = restored
        except Exception as exc:
            summary.setdefault("errors", []).append(
                f"Binding self-heal: {exc}"
            )
        return summary

    def reliable_auto_sync(router, first=False):
        result = original_auto_sync(router, first=first)

        # If the latest full Link sync failed after the last success, retry
        # sooner than LINK_SYNC_MINUTES, but never continuously.
        try:
            from .tasks import enqueue_router_sync

            if router.sync_jobs.filter(
                status__in=["queued", "running"]
            ).exists():
                return result

            latest = router.sync_jobs.order_by("-created_at").first()
            if not latest or latest.status != "failed":
                return result

            age = (
                timezone.now() - (latest.finished_at or latest.updated_at or latest.created_at)
            ).total_seconds()

            if age < 60:
                return result

            if cache.add(
                f"tt:link:failed-sync-retry:{router.pk}",
                1,
                SYNC_RETRY_SECONDS,
            ):
                enqueue_router_sync(router)
        except Exception:
            logger.exception("Early failed-sync retry failed for %s", router)

        return result

    link.queue = reliable_queue
    link.handle_ack = reliable_handle_ack
    link.build_response = reliable_build_response
    link.handle_poll = reliable_handle_poll
    link.auto_sync = reliable_auto_sync
    inventory._bindings = reliable_bindings

    link._taptap_sync_reliability_installed = True
    logger.info("TapTap Link sync reliability fixes installed")
