import logging
import os
import time

from django.core.management.base import BaseCommand

from core.tunnel import manager_iteration, tunnel_enabled

logger = logging.getLogger("taptap.tunnel")


class Command(BaseCommand):
    help = "Run the TapTap WireGuard server/peer manager."

    def handle(self, *args, **options):
        if not tunnel_enabled():
            self.stdout.write(
                self.style.WARNING(
                    "TAPTAP_TUNNEL_ENABLED is off. Set it to 1 in the tunnel deployment."
                )
            )
            return

        interval = max(5, int(os.getenv("TAPTAP_WG_MANAGER_SECONDS", "10")))
        self.stdout.write(self.style.SUCCESS("TapTap Tunnel manager starting"))
        last_public = ""

        while True:
            try:
                public_key = manager_iteration()
                if public_key != last_public:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"TapTap WireGuard server ready; public key: {public_key}"
                        )
                    )
                    last_public = public_key
            except KeyboardInterrupt:
                self.stdout.write("TapTap Tunnel manager stopped")
                return
            except Exception as exc:
                logger.exception("TapTap Tunnel manager iteration failed")
                self.stderr.write(f"TapTap Tunnel manager: {exc}")
            time.sleep(interval)
