# TapTap update 5 — "money" sound for new sales (+ updates 1–4)

Cumulative: everything from updates 1–4, plus the new cash-register sound on Daily sales.

    cd /opt/taptap
    unzip -o ~/taptap-update-5.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker live
    docker compose run --rm web python manage.py migrate
    docker compose up -d --force-recreate redis web worker live

Only changed since update 4: static/js/sales-live.js and templates/core/sales_daily.html
(script version bumped to ?v=5 so browsers fetch the new sound straight away). No migration.
