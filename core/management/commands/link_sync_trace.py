"""Why does the TapTap Link full sync fail? Shows where the tables of the last syncs got stuck.

    docker compose exec web python manage.py link_sync_trace            # every Link router
    docker compose exec web python manage.py link_sync_trace --router 3

For each recent sync job: how many tables were sent to the router, run by it, uploaded to TapTap,
and imported by the worker — plus how many Celery tasks are waiting in each queue right now.

  sent 0                       → the router is not asking for commands (check-in problem)
  sent > 0, ran 0              → the router takes the commands but fails them (router log: "TapTap")
  ran > 0, uploaded 0          → uploads fail: router clock / HTTPS / firewall (router log: "fetch")
  uploaded > 0, imported low   → TapTap's worker is too busy: restart worker + live, raise concurrency
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Explain failing TapTap Link full syncs.'

    def add_arguments(self, parser):
        parser.add_argument('--router', type=int)
        parser.add_argument('--jobs', type=int, default=3)

    def handle(self, router=None, jobs=3, **opts):
        from django.conf import settings
        from core.agent_inventory import job_pieces
        from core.models import Router
        w = self.stdout.write

        try:
            import redis
            r = redis.Redis.from_url(settings.CELERY_BROKER_URL)
            w('Celery queues waiting now: ' + ', '.join(f'{q}={r.llen(q)}' for q in ('celery', 'live')))
        except Exception as exc:
            w(f'Celery queues: could not read ({exc})')

        routers = Router.objects.filter(connection_mode='agent')
        if router:
            routers = routers.filter(pk=router)
        for rt in routers:
            try:
                agent = rt.agent
                w(f'\n{rt.name} (#{rt.pk}) — Link last seen {agent.last_seen_at}, online={agent.online}')
            except Exception:
                w(f'\n{rt.name} (#{rt.pk}) — no Link enrolled')
            for job in rt.sync_jobs.order_by('-created_at')[:jobs]:
                pieces = job_pieces(rt, job.pk)
                sent = sum(1 for c in pieces if c.attempts or c.status in ('sent', 'done'))
                ran = sum(1 for c in pieces if c.status == 'done' or (c.params or {}).get('uploaded'))
                up = sum(1 for c in pieces if (c.params or {}).get('uploaded'))
                imp = sum(1 for c in pieces if (c.params or {}).get('received'))
                w(f'  job #{job.pk} {job.created_at:%d %b %H:%M} {job.status}: tables={len(pieces)} sent={sent} '
                  f'ran={ran} uploaded={up} imported={imp}')
                w(f'    {job.phase[:200]}')
                statuses = {}
                for c in pieces:
                    key = (c.status, (c.result or '')[:60])
                    statuses[key] = statuses.get(key, 0) + 1
                for (st, res), n in sorted(statuses.items(), key=lambda x: -x[1])[:6]:
                    w(f'    {n:3d} × {st:9s} {res}')
