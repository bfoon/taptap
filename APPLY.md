# TapTap update 7 — vouchers ready before the login, self-healing logins, no voucher waits for a sync

Fixes "the voucher shows on TapTap with the phone's MAC, but the customer cannot browse". See README →
"Ready to browse". Based on bfoon/taptap master (5953a51). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-7.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout: `git apply taptap-update-7.patch`)

After the restart
* Make sure SITE_URL is set in .env: routers then get the new login page within the hour by themselves. To do it
  now: Portal Studio → Put on routers.
* The voucher worker now runs 4 at a time (VOUCHER_WORKER_CONCURRENCY). The sweeper and the faster TapTap Link
  check-ins start by themselves.
* Check any code from the server:  docker compose exec web python manage.py ready_check ABC123   (add --fix)

Changed: config/settings.py, docker-compose.yml, README.md, static/studio/portal-render.js, core/agent.py,
core/apps.py, core/member_sync.py, core/tasks.py, core/urls.py, core/views_ads.py, core/views_agents.py,
core/views_studio.py, core/voucher_fastlane.py, core/voucher_push.py.
New: core/voucher_ready.py, core/url_alignment.py, core/management/commands/ready_check.py,
core/tests/test_voucher_ready.py.

New .env settings (all optional): VOUCHER_SWEEP_SECONDS=10, READY_API_WAIT=2.0, PORTAL_AUTO_REFRESH=1,
LINK_VOUCHER_CHUNK=25, LINK_VOUCHER_CHUNK_V6=8, LINK_TURBO_PER_MINUTE=12, VOUCHER_WORKER_CONCURRENCY=4.
