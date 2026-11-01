# TapTap update 11 — syncs get through; phones always find the login page

Install updates 7–10 first (this update is built on them). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-11.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout with updates 7–10: `git apply taptap-update-11.patch`)

What changes
* TapTap Link syncs no longer starve behind voucher batches: check-ins take turns. Your failed syncs retry by
  themselves and should now complete.
* A voucher code the router refuses 3 times is no longer re-sent at every check-in (it is marked "Refused by the
  router"); pressing Sync tries it again once.
* A phone logged back in by its MAC cookie is no longer left in a sign-in window showing only the redirect page.
* Phones' connection tests always need a login, so the login sheet opens even when free-access websites include
  Google or Apple. Routers get these rules within minutes (housekeeping) and with every portal install.
* The status page goes to the login page as soon as the time is up.

Router pages carry the new page code: routers with TapTap's portal installed get it within the hour by themselves
(needs SITE_URL), or now with Portal Studio › Put on routers.

Changed: README.md, core/agent.py, core/agent_inventory.py, core/apps.py, core/free_access.py, core/live.py,
core/portal_deploy.py, core/views_studio.py, static/studio/portal-render.js.
New: core/probe_guard.py, core/tests/test_sync_turns.py.

Tests: python manage.py test core (301).
