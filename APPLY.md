# TapTap update 12 — the header pill tells the truth

Install updates 7–11 first (this update is built on them). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-12.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout with updates 7–11: `git apply taptap-update-12.patch`)

What changes
* "Live · 40s ago" right after a check: the pill now corrects for this computer's clock (the panel says if it is off).
* "1 router offline" for a router that never connected: no longer counted ("not connected yet" in the panel).
* TapTap is never blocked by its own router protection: it learns the address its connections really come from and
  trusts it on each router; the 10-second check only knocks where that is safe.

If a router is still blocked from before (up to an hour), it clears by itself; or on the router:
  /ip firewall address-list remove [find where list~"taptap-bf|taptap-ips-blocked|taptap-ddos-blocked"]

Changed: README.md, core/live.py, core/protection.py, core/router_pulse.py, static/js/live.js,
templates/core/base.html, core/tests/test_router_pulse.py. New: core/self_trust.py, core/tests/test_self_trust.py.

Tests: python manage.py test core (310).
