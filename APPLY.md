# TapTap update 10 — router connection checked every 10 seconds

Install updates 7, 8 and 9 first (this update is built on them). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-10.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout with updates 7–9: `git apply taptap-update-10.patch`)

Important: recreate the **live** service (as above). Its command changed: it now runs only live work, with 3
processes, and the scheduler adds the 10-second router pulse and the 30-second housekeeping task.

What changes
* Every Direct API router's connection is checked every 10 s (a quick knock on its API port, all routers at once,
  no login). An offline router that answers is re-checked immediately.
* The header pill shows when your routers last answered, so it stays "Live · a few s ago" while they are reachable.
* One slow router no longer delays the checks of the others; the live worker no longer runs voucher pushes.
* A sync stuck "running" after a restart is cleared after 30 minutes.

Optional .env: ROUTER_PULSE_SECONDS=10, ROUTER_PULSE_TIMEOUT=4, LIVE_PARALLEL=8, LIVE_HOUSEKEEPING_SECONDS=30,
LIVE_WORKER_CONCURRENCY=3.

Changed: README.md, config/settings.py, docker-compose.yml, core/live.py, core/tasks.py, core/views_live.py,
static/js/live.js, templates/core/base.html. New: core/router_pulse.py, core/tests/test_router_pulse.py.

Tests: python manage.py test core (285).
