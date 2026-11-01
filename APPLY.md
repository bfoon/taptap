# TapTap update 9 — no more "internet down / back" every few seconds

Install updates 7 and 8 first (this update is built on them). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-9.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout with updates 7 and 8: `git apply taptap-update-9.patch`)

What changes
* TapTap Link routers get heartbeat v9 by themselves within a few check-ins (no reinstall): a customer logging
  out, a slow TapTap answer or a TapTap-side error is no longer counted as a missed check-in; the internet-route
  watch stops restarting the tunnel on routes that merely flicker.
* Offline / back-online: shown after several failed checks, emailed only after 3 minutes; "back online" only after
  an "offline" email; several drops → one "connection unstable" email; a TapTap restart never marks routers offline.
* Notifications page: new rows "A router's connection is unstable" and "A Link router could not reach TapTap for a
  while" (digest by default — switch it off there if you prefer).
* See what happened: `docker compose exec web python manage.py connection_history`

Optional .env: ROUTER_OFFLINE_ALERT_MINUTES=3, ROUTER_OFFLINE_CHECKS=3, ROUTER_OFFLINE_SECONDS=40.

Changed: README.md, config/settings.py, core/agent.py, core/live.py, core/notify.py, core/tasks.py, core/views.py,
core/views_link.py. New: core/uptime_guard.py, core/management/commands/connection_history.py,
core/tests/test_flapping.py.

Tests: python manage.py test core (271).
