#!/usr/bin/env python3
"""Apply non-destructive TapTap two-site isolation / diagnostics patch.

Usage:
    python3 apply_multisite_fix.py --repo /opt/taptap --check
    python3 apply_multisite_fix.py --repo /opt/taptap --diff
    python3 apply_multisite_fix.py --repo /opt/taptap --apply

This modifies ONLY local source files, NOT the database or MikroTik routers.
All replacements are checked for an exact match and every resulting Python
file must parse before anything is written. Originals are backed up.
Two complete, copy-ready new files live under core/ in the ZIP.
"""
import argparse
import ast
import difflib
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone


def replace_once(text, old, new, location):
    if new in text:
        return text  # already applied (the original may be a prefix of the replacement)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{location}: expected one original block, found {count}; aborting safely')
    return text.replace(old, new, 1)


def transform(path, text):
    if path == 'core/agent_inventory.py':
        text = replace_once(
            text,
            "and existing_voucher.source == 'taptap' else 'mikrotik'",
            "and existing_voucher.source == 'taptap' and existing_voucher.router_id in (None, router.pk) else 'mikrotik'",
            path + ': source of cross-site mirror',
        )
        original = """            summary['duplicate_vouchers_skipped'] += 1
            fields = ['mikrotik_sync_status', 'mikrotik_sync_error']"""
        replacement = """            # The code is globally unique, but two physical sites may both have a
            # RouterOS user with that name. Mirror this site's user above without
            # claiming the voucher assigned to the other MikroTik is synchronized.
            if existing_voucher.router_id not in (None, router.pk):
                summary['duplicate_vouchers_skipped'] += 1
                summary['errors'].append(
                    f'Voucher {username} is assigned to router #{existing_voucher.router_id} '
                    f'but also exists on {router.name}. This site was mirrored, not imported: '
                    'use different voucher codes per site or explicitly reassign the voucher.'
                )
                continue
            summary['duplicate_vouchers_skipped'] += 1
            fields = ['mikrotik_sync_status', 'mikrotik_sync_error']"""
        text = replace_once(text, original, replacement, path + ': foreign-router voucher')
    elif path == 'core/sync.py':
        source_old = """                    and existing_voucher.source
                    == 'taptap'
"""
        source_new = source_old + "                    and existing_voucher.router_id in (None, router.pk)\n"
        text = replace_once(text, source_old, source_new, path + ': source classification')
        original = """                summary[
                    'duplicate_vouchers_skipped'
                ] += 1"""
        replacement = """                # A globally unique voucher code must never be marked Synced by
                # the OTHER site's RouterOS inventory. The RouterHotspotUser
                # mirror above remains available for troubleshooting.
                if existing_voucher.router_id not in (None, router.pk):
                    summary['duplicate_vouchers_skipped'] += 1
                    summary['errors'].append(
                        f'Voucher {username} is assigned to router #{existing_voucher.router_id} '
                        f'but also exists on {router.name}. This site was mirrored, not imported: '
                        'use different voucher codes per site or explicitly reassign the voucher.'
                    )
                    continue

                summary[
                    'duplicate_vouchers_skipped'
                ] += 1"""
        text = replace_once(text, original, replacement, path + ': foreign-router voucher')
    elif path == 'core/agent.py':
        text = replace_once(
            text,
            "        ids = cmd.params.get('ids', [])",
            "        # An old command may acknowledge after the voucher has moved to another router.\n"
            "        # Only this router's vouchers can be marked Synced by its acknowledgement.\n"
            "        ids = list(Voucher.objects.filter(pk__in=cmd.params.get('ids', []), router=r)\n"
            "                   .values_list('pk', flat=True))",
            path + ': stale voucher command acknowledgements',
        )
        text = replace_once(
            text,
            'Voucher.objects.filter(business=router.business, code__in=codes)',
            'Voucher.objects.filter(business=router.business, router=router, code__in=codes)',
            path + ': cross-site live session isolation',
        )
    elif path == 'core/views_link.py':
        text = replace_once(
            text,
            "    except Exception:\n        # A TapTap-side error must never look like",
            "    except Exception as exc:\n        # A TapTap-side error must never look like",
            path + ': catching check-in exception',
        )
        old = """            RouterAgent.objects.filter(pk=agent.pk).update(last_seen_at=_tz.now(), last_ip=ip)
        except Exception:
            pass
        script = ''"""
        new = """            RouterAgent.objects.filter(pk=agent.pk).update(last_seen_at=_tz.now(), last_ip=ip)
            # A heartbeat arriving is not proof that command delivery or traffic
            # ingestion succeeded. Keep the failure visible beside 'Online'.
            Router.objects.filter(pk=agent.router_id).update(
                last_error=f'TapTap Link server processing failed ({type(exc).__name__}); check web logs'
            )
        except Exception:
            pass
        script = ''"""
        text = replace_once(text, old, new, path + ': report degraded Link status')
        text = replace_once(
            text,
            "        'sessions': a.active_sessions, 'polls': a.polls, 'commands': cmds,",
            "        'sessions': a.active_sessions, 'polls': a.polls, 'commands': cmds,\n"
            "        'last_error': router.last_error,\n"
            "        'traffic_seen_at': (cache.get(f'tt:tr:now:{router.pk}') or {}).get('at'),\n"
            "        'interface_rates_at': (cache.get(f'tt:linkrates:{router.pk}') or {}).get('at'),",
            path + ': visible status diagnostics',
        )
    elif path == 'core/traffic_report.py':
        text = replace_once(
            text,
            "    wan_routers = set(qs.exclude(interface='*users').values_list('router_id', flat=True).distinct())",
            "    # A zero-byte WAN row can mean the Link heartbeat returned empty interface\n"
            "    # counters; it must not hide non-zero HotSpot session usage on that site.\n"
            "    wan_routers = set(qs.exclude(interface='*users')\n"
            "                      .filter(Q(rx_bytes__gt=0) | Q(tx_bytes__gt=0))\n"
            "                      .values_list('router_id', flat=True).distinct())",
            path + ': WAN fallback for missing traffic counters',
        )
    elif path == 'core/management/commands/link_sync_trace.py':
        text = replace_once(text, "('celery', 'live')", "('celery', 'live', 'vouchers')", path + ': voucher queue')
        text = replace_once(
            text,
            "        from core.models import Router\n        w = self.stdout.write",
            "        from core.models import Router, Voucher, RouterAgent, AgentCommand\n"
            "        from django.core.cache import cache\n"
            "        from django.db.models import Count\n"
            "        w = self.stdout.write",
            path + ': diagnostics imports',
        )
        old = """        for rt in routers:
            try:
                agent = rt.agent
                w(f'\\n{rt.name} (#{rt.pk}) — Link last seen {agent.last_seen_at}, online={agent.online}')
            except Exception:
                w(f'\\n{rt.name} (#{rt.pk}) — no Link enrolled')
            for job in rt.sync_jobs.order_by('-created_at')[:jobs]:"""
        new = """        for rt in routers:
            agent = None
            try:
                agent = rt.agent
                w(f'\\n{rt.name} (#{rt.pk}) — Link last seen {agent.last_seen_at}, online={agent.online}, '
                  f'script=v{agent.script_version}, RouterOS={agent.ros_version}, identity={agent.identity}')
            except Exception:
                w(f'\\n{rt.name} (#{rt.pk}) — no Link enrolled')
            if agent and agent.identity:
                matches = list(RouterAgent.objects.filter(identity=agent.identity, revoked=False)
                               .exclude(pk=agent.pk).values_list('router__name', flat=True)[:5])
                if matches:
                    w('  WARNING: duplicate router identity on other Link registrations: ' + ', '.join(matches))
            if rt.last_error:
                w('  Last router error: ' + rt.last_error[:250])
            now_data = cache.get(f'tt:tr:now:{rt.pk}') or {}
            rates_data = cache.get(f'tt:linkrates:{rt.pk}') or {}
            w(f"  Traffic: sessions_seen={now_data.get('at', 'never')}, "
              f"sessions={now_data.get('count', 0)}, "
              f"interfaces_seen={rates_data.get('at', 'never')}, "
              f"interface_rates={len(rates_data.get('rates') or {})}")
            vcounts = list(Voucher.objects.filter(router=rt).values('mikrotik_sync_status')
                           .annotate(n=Count('pk')).order_by('-n'))
            w(f'  Vouchers assigned to router: {vcounts}; '
              f'present hotspot users mirrored: {rt.hotspot_users.filter(is_present=True).count()}')
            commands = list(AgentCommand.objects.filter(router=rt, status__in=['queued', 'sent'])
                            .values('kind', 'status').annotate(n=Count('pk')).order_by('-n')[:10])
            w(f'  Queued/sent Link commands (top 10): {commands}')
            for job in rt.sync_jobs.order_by('-created_at')[:jobs]:"""
        text = replace_once(text, old, new, path + ': per-router diagnostic details')
    else:
        raise RuntimeError(f'Unexpected patch path: {path}')
    return text


PATHS = (
    'core/agent_inventory.py',
    'core/sync.py',
    'core/agent.py',
    'core/views_link.py',
    'core/traffic_report.py',
)
EXTRA_PATHS = (
    'core/tests/test_multisite_link.py',
    'core/management/commands/multisite_status.py',
    'core/management/commands/link_sync_trace.py',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', default='.', help='TapTap repo root, e.g. /opt/taptap')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true', help='Verify compatibility without writing')
    modes.add_argument('--diff', action='store_true', help='Display patch without writing')
    modes.add_argument('--apply', action='store_true', help='Back up and write source files')
    args = parser.parse_args()
    root = Path(args.repo).resolve()
    if not (root / 'manage.py').exists():
        parser.error(f'{root} is not a TapTap Django project (manage.py missing)')
    changes = {}
    for rel in PATHS:
        file = root / rel
        before = file.read_text(encoding='utf-8')
        after = transform(rel, before)
        ast.parse(after, filename=rel)
        if before != after:
            changes[rel] = (before, after)
    # New, complete files are physically included in the ZIP at their exact
    # TapTap paths. Never overwrite a differently customized version silently.
    package_root = Path(__file__).resolve().parent.parent
    for rel in EXTRA_PATHS:
        payload = (package_root / rel).read_text(encoding='utf-8')
        ast.parse(payload, filename=rel)
        target = root / rel
        existing = target.read_text(encoding='utf-8') if target.exists() else ''
        if existing != payload:
            # link_sync_trace is a complete, backward-compatible replacement
            # for the existing diagnostics command; back it up before replacing.
            if existing and rel != 'core/management/commands/link_sync_trace.py':
                raise RuntimeError(f'{rel} exists with different content; refusing to overwrite')
            if existing and rel.endswith('/link_sync_trace.py') and 'def link_sync_trace(' in existing:
                raise RuntimeError(f'{rel} appears customized; refusing to overwrite')
            changes[rel] = (existing, payload)
    print(f'Checked {len(PATHS)} existing TapTap files and {len(EXTRA_PATHS)} packaged source files; {len(changes)} would change; syntax validation passed.')
    if args.diff:
        for rel, (before, after) in changes.items():
            sys.stdout.writelines(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile='a/' + rel, tofile='b/' + rel,
            ))
    elif args.apply and changes:
        backups = root / '.taptap-patch-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backups.mkdir(parents=True, exist_ok=False)
        for rel, (before, after) in changes.items():
            target = root / rel
            if target.exists():
                dst = backups / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dst)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.taptap-new')
            temp.write_text(after, encoding='utf-8')
            if target.exists():
                os.chmod(temp, target.stat().st_mode)
            os.replace(temp, target)
            print('Updated:', rel)
        print('Backups:', backups)
        print('Next: python manage.py test core.tests.test_multisite_link')
        print('Do not deploy until tests and diagnostics pass.')
    elif args.apply:
        print('Patch is already applied; nothing changed.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Patch aborted:', exc, file=sys.stderr)
        sys.exit(1)
