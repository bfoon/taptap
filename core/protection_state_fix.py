"""Fix Security -> DDoS / IDS-IPS state reporting for TapTap Link.

Problem
-------
core.protection.status() reads On/Off from RouterConfigSnapshot.  For a Link
router, enabling/disabling protection is asynchronous: an AgentCommand is queued,
the router applies it and ACKs it, but the firewall inventory snapshot can remain
older until the next full sync.  The Security page therefore showed "Off" even
after a successful enable.

This module keeps the existing snapshot as the verification source, but uses the
newer protection AgentCommand result until a later firewall inventory supersedes
that command.

No database migration is required.
"""
from __future__ import annotations

from django.core.cache import cache


def _latest_for_feature(router, feature):
    from .models import AgentCommand

    # Avoid relying on JSONField database lookups so this remains portable.
    for cmd in (
        AgentCommand.objects
        .filter(router=router, kind='protection')
        .order_by('-created_at', '-pk')[:50]
    ):
        params = cmd.params or {}
        if params.get('feature') == feature:
            return cmd
    return None


def _newer_than_snapshot(cmd, checked_at):
    if not cmd:
        return False

    when = cmd.done_at or cmd.sent_at or cmd.created_at

    if not checked_at:
        return True

    return bool(when and when > checked_at)


def install():
    from . import protection

    if getattr(protection, '_taptap_state_fix_installed', False):
        return

    original_status = protection.status

    def reliable_status(router):
        result = original_status(router)
        checked_at = result.get('checked_at')

        for feature in protection.FEATURES:
            row = result.get(feature)
            if not row:
                continue

            cmd = _latest_for_feature(router, feature)

            # Ignore an old command once a newer RouterOS inventory has verified
            # the real firewall configuration.
            if not _newer_than_snapshot(cmd, checked_at):
                # The old implementation leaves a 10-minute cache flag around.
                # Once inventory is newer, it must no longer appear as pending.
                row['pending'] = None
                row['command_status'] = ''
                row['command_error'] = ''
                continue

            params = cmd.params or {}
            action = params.get('action')

            row['command_status'] = cmd.status
            row['command_error'] = ''
            row['command_id'] = cmd.pk

            if cmd.status in ('queued', 'sent'):
                row['pending'] = action
                row['command_error'] = ''

            elif cmd.status == 'done':
                # The MikroTik ACK is newer than TapTap's firewall snapshot, so
                # the command result is the most recent confirmed state.
                row['pending'] = None
                cache.delete(f'tt:protect:{router.pk}:{feature}')

                if action == 'enable':
                    row['on'] = True
                    # Exact rule count is verified at the next inventory.
                    row['rules'] = max(int(row.get('rules') or 0), 1)

                elif action == 'disable':
                    row['on'] = False
                    row['rules'] = 0
                    row['blocked'] = 0

                elif action == 'unblock':
                    # Unblock does not change whether protection itself is on.
                    row['blocked'] = 0

            elif cmd.status in ('failed', 'expired', 'cancelled'):
                row['pending'] = None
                cache.delete(f'tt:protect:{router.pk}:{feature}')
                row['command_error'] = (
                    cmd.result
                    or {
                        'expired': 'The command expired before the router applied it.',
                        'cancelled': 'The command was cancelled.',
                    }.get(cmd.status)
                    or 'The router reported an error.'
                )

        result['features'] = [result[k] for k in protection.FEATURES]
        return result

    protection.status = reliable_status
    protection._taptap_state_fix_installed = True
