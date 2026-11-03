"""Read-only diagnostics for independent TapTap Link sites.

Run after copying into core/management/commands::

    python manage.py multisite_status
    python manage.py multisite_status --router 1

Never prints Link tokens, login passwords or voucher codes.
"""
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.utils import timezone

from core.models import AgentCommand, Router, RouterAgent, TrafficSample, Voucher


class Command(BaseCommand):
    help = "Read-only health, voucher, traffic and command queue checks for all MikroTik sites"

    def add_arguments(self, parser):
        parser.add_argument("--router", type=int, default=None, help="Limit to Router ID")
        parser.add_argument("--jobs", type=int, default=2, help="Recent sync jobs shown per router")

    def handle(self, *args, **options):
        now = timezone.now()
        routers = Router.objects.select_related("business").order_by("business_id", "pk")
        if options["router"]:
            routers = routers.filter(pk=options["router"])

        total = 0
        for router in routers:
            total += 1
            self.stdout.write("\n" + "=" * 68)
            self.stdout.write(f"Router #{router.pk}: {router.name} | business #{router.business_id}")
            self.stdout.write(f"Connection: {router.connection_mode} | shown status: {router.status}")
            if router.last_error:
                self.stdout.write("Router last_error: " + router.last_error[:250])
            agent = None
            try:
                agent = router.agent
            except RouterAgent.DoesNotExist:
                pass
            if router.connection_mode == "agent":
                if agent is None:
                    self.stdout.write(self.style.WARNING("LINK NOT ENROLLED"))
                else:
                    seconds = int((now - agent.last_seen_at).total_seconds()) if agent.last_seen_at else None
                    self.stdout.write(
                        f"Link: online={agent.online} revoked={agent.revoked} "
                        f"last_poll_age={seconds if seconds is not None else 'never'}s "
                        f"polls={agent.polls} script=v{agent.script_version} RouterOS={agent.ros_version}"
                    )
                    same_token = RouterAgent.objects.filter(
                        router__business_id=router.business_id,
                        token_hash=agent.token_hash, revoked=False,
                    ).exclude(pk=agent.pk).count()
                    if same_token:
                        self.stdout.write(self.style.ERROR(
                            f"WARNING: {same_token} other router registration(s) use this Link token hash"
                        ))
                    if agent.identity:
                        same_identity = RouterAgent.objects.filter(
                            router__business_id=router.business_id, identity=agent.identity,
                            revoked=False,
                        ).exclude(pk=agent.pk).count()
                        if same_identity:
                            self.stdout.write(self.style.WARNING(
                                f"NOTE: {same_identity} other site(s) report RouterOS identity "
                                f"{agent.identity!r}; confirm the router registrations"
                            ))
            active = cache.get(f"tt:tr:now:{router.pk}") or {}
            rates = cache.get(f"tt:linkrates:{router.pk}") or {}
            counters = cache.get(f"tt:linkctr:{router.pk}") or {}
            self.stdout.write(
                f"Live traffic: sessions_at={active.get('at', 'never')} "
                f"sessions={active.get('count', 0)} "
                f"rates_at={rates.get('at', 'never')} "
                f"rates={len(rates.get('rates') or {})} "
                f"counter_interfaces={len(counters)}"
            )
            if agent and agent.online and not counters:
                self.stdout.write(self.style.WARNING(
                    "ONLINE BUT NO INTERFACE COUNTERS: check Link script version, "
                    "HTTP poll payload (ifc), and web logs"
                ))
            latest = TrafficSample.objects.filter(router=router).aggregate(last=Max("bucket"))["last"]
            recent = TrafficSample.objects.filter(router=router, bucket__gte=now - timedelta(hours=1))
            samples = list(recent.values("interface").annotate(n=Count("pk")).order_by("-n")[:8])
            self.stdout.write(f"Persisted traffic: latest_bucket={latest or 'never'} last_hour={samples}")
            vouchers = list(
                Voucher.objects.filter(router=router).values("source", "mikrotik_sync_status")
                .annotate(n=Count("pk")).order_by("source", "mikrotik_sync_status")
            )
            self.stdout.write(f"Assigned vouchers (counts, not codes): {vouchers}")
            self.stdout.write(f"RouterOS users mirrored: {router.hotspot_users.filter(is_present=True).count()}")
            pending = list(
                AgentCommand.objects.filter(router=router, status__in=["queued", "sent"])
                .values("kind", "status").annotate(n=Count("pk")).order_by("-n")[:12]
            )
            self.stdout.write(f"Link command backlog: {pending}")
            for job in router.sync_jobs.order_by("-created_at")[:max(0, options["jobs"])]:
                summary = job.summary or {}
                self.stdout.write(
                    f"Sync job #{job.pk}: {job.status} | {job.phase[:120]} | "
                    f"received={summary.get('inventory_received', '?')}/"
                    f"{summary.get('inventory_parts', '?')} "
                    f"errors={len(summary.get('errors') or [])}"
                )
            if agent and agent.online and latest and latest < now - timedelta(minutes=20):
                self.stdout.write(self.style.WARNING(
                    "ONLINE BUT TRAFFIC SAMPLES ARE STALE: inspect Link counters and worker logs"
                ))
        if not total:
            self.stdout.write(self.style.WARNING("No router matches the selection"))
        else:
            self.stdout.write("\nRead-only checks completed for " + str(total) + " router(s).")
