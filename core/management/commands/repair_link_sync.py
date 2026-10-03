from django.core.management.base import BaseCommand

from core.models import Router
from core.link_sync_reliability import _reconcile_rows, _heal_missing_after_inventory


class Command(BaseCommand):
    help = "Repair stale TapTap Link binding queue states and requeue current desired bypass/IP-binding state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--router",
            type=int,
            default=0,
            help="Only repair one Router ID.",
        )
        parser.add_argument(
            "--no-heal",
            action="store_true",
            help="Only reconcile statuses; do not queue repair commands.",
        )

    def handle(self, *args, **options):
        qs = Router.objects.filter(connection_mode="agent").order_by("business_id", "name")
        if options["router"]:
            qs = qs.filter(pk=options["router"])

        routers = list(qs)
        if not routers:
            self.stdout.write(self.style.WARNING("No TapTap Link routers found."))
            return

        total_healed = 0
        total_missing = 0

        for router in routers:
            healed = _reconcile_rows(router, heal=not options["no_heal"])
            missing = 0
            if not options["no_heal"]:
                missing = _heal_missing_after_inventory(router)

            total_healed += healed
            total_missing += missing

            self.stdout.write(
                f"{router.pk} {router.name}: "
                f"{healed} stale/error binding(s) requeued, "
                f"{missing} missing TapTap binding(s) restored."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished. Requeued {total_healed}; restored missing {total_missing}."
            )
        )
