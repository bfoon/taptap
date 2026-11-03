"""Trace TapTap Link sync stages for each MikroTik without exposing tokens.

Examples::
    python manage.py link_sync_trace --jobs 2
    python manage.py link_sync_trace --router 3 --jobs 5

Sent=0: Link delivery / scheduler / queue; uploaded=0: router execution/upload;
uploaded>0 and imported=0: worker/Redis. A heartbeat alone is not sync success.
"""
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = 'Explain stalled TapTap Link synchronization, vouchers and traffic, per router.'

    def add_arguments(self, parser):
        parser.add_argument('--router', type=int, default=None)
        parser.add_argument('--jobs', type=int, default=3)

    def handle(self, *args, **options):
        from core.agent_inventory import job_pieces
        from core.models import AgentCommand, Router, RouterAgent, Voucher
        out = self.stdout.write
        try:
            import redis
            broker = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=3)
            out('Celery queues: ' + ', '.join(
                f'{q}={broker.llen(q)}' for q in ('celery', 'live', 'vouchers')
            ))
        except Exception as exc:
            out(f'Celery queues unavailable: {type(exc).__name__}')
        routers = Router.objects.filter(connection_mode='agent').order_by('business_id', 'pk')
        if options['router']:
            routers = routers.filter(pk=options['router'])
        for router in routers:
            try:
                agent = router.agent
            except RouterAgent.DoesNotExist:
                agent = None
            if agent:
                out(f'\n{router.name} (#{router.pk}) — last seen {agent.last_seen_at}, '
                    f'online={agent.online}, version={agent.script_version}, RouterOS={agent.ros_version}, '
                    f'identity={agent.identity}')
                matches = RouterAgent.objects.filter(router__business=router.business,
                    token_hash=agent.token_hash, revoked=False).exclude(pk=agent.pk).count()
                if matches:
                    out(f'  WARNING: same Link token hash assigned to {matches} other router(s)')
            else:
                out(f'\n{router.name} (#{router.pk}) — no Link enrolled')
            if router.last_error:
                out('  Last error: ' + router.last_error[:250])
            sessions = cache.get(f'tt:tr:now:{router.pk}') or {}
            rates = cache.get(f'tt:linkrates:{router.pk}') or {}
            counters = cache.get(f'tt:linkctr:{router.pk}') or {}
            out(f"  Traffic: sessions_seen={sessions.get('at', 'never')} "
                f"sessions={sessions.get('count', 0)} rates_seen={rates.get('at', 'never')} "
                f"interfaces={len(counters)} rates={len(rates.get('rates') or {})}")
            vouchers = list(Voucher.objects.filter(router=router)
                            .values('mikrotik_sync_status')
                            .annotate(n=Count('pk')).order_by('-n'))
            out(f'  Vouchers (by sync state): {vouchers}')
            out(f'  RouterOS users mirrored: {router.hotspot_users.filter(is_present=True).count()}')
            cmds = list(AgentCommand.objects.filter(router=router, status__in=['queued', 'sent'])
                        .values('kind', 'status').annotate(n=Count('pk')).order_by('-n')[:12])
            out(f'  Active command backlog: {cmds}')
            for job in router.sync_jobs.order_by('-created_at')[:max(0, options['jobs'])]:
                pieces = job_pieces(router, job.pk)
                sent = sum(1 for c in pieces if c.attempts or c.status in ('sent', 'done'))
                ran = sum(1 for c in pieces if c.status == 'done' or (c.params or {}).get('uploaded'))
                uploaded = sum(1 for c in pieces if (c.params or {}).get('uploaded'))
                imported = sum(1 for c in pieces if (c.params or {}).get('received'))
                out(f'  job #{job.pk} {job.created_at:%d %b %H:%M} {job.status}: '
                    f'tables={len(pieces)} sent={sent} ran={ran} uploaded={uploaded} imported={imported}')
                out(f'    {job.phase[:200]}')
                statuses = {}
                for c in pieces:
                    key = (c.status, (c.result or '')[:60])
                    statuses[key] = statuses.get(key, 0) + 1
                for (status, result), n in sorted(statuses.items(), key=lambda v: -v[1])[:6]:
                    out(f'    {n:3d} x {status:9s} {result}')
