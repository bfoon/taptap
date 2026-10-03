from django.core.management.base import BaseCommand

from core.models import Router, AgentCommand
from core.protection import status


class Command(BaseCommand):
    help = "Show DDoS/IDS-IPS state, latest Link command and snapshot-aware result."

    def add_arguments(self, parser):
        parser.add_argument("--router", type=int, default=0)

    def handle(self, *args, **options):
        qs = Router.objects.order_by("business_id", "name")
        if options["router"]:
            qs = qs.filter(pk=options["router"])

        for router in qs:
            st = status(router)
            self.stdout.write(f"\nRouter {router.pk}: {router.name}")
            self.stdout.write(f"Snapshot checked: {st.get('checked_at') or 'never'}")

            for feature in ("ddos", "ips"):
                row = st.get(feature) or {}
                self.stdout.write(
                    f"  {feature}: on={row.get('on')} "
                    f"pending={row.get('pending')} "
                    f"command_status={row.get('command_status', '')} "
                    f"error={row.get('command_error', '')}"
                )

            commands = (
                AgentCommand.objects
                .filter(router=router, kind="protection")
                .order_by("-created_at")[:10]
            )
            for cmd in commands:
                self.stdout.write(
                    f"    cmd#{cmd.pk} {cmd.status} "
                    f"{(cmd.params or {}).get('feature')}/"
                    f"{(cmd.params or {}).get('action')} "
                    f"result={cmd.result!r}"
                )
