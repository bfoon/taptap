# TapTap update 4 — Daily sales updates itself (+ updates 1, 2 and 3)

Cumulative: letter case, feature modules, country pricing, daily sales drill-down, login addresses,
the fix for random 500 errors, and now live Daily sales.

    cd /opt/taptap
    unzip -o ~/taptap-update-4.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker live
    docker compose run --rm web python manage.py migrate
    docker compose up -d --force-recreate redis web worker live

No new migration in this update. collectstatic runs at container start (entrypoint), so the new
static/js/sales-live.js is served automatically.
