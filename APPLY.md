# TapTap update 6 — new-sale sounds: real "ka-CHING", coins, counter bell (+ updates 1–5)

Cumulative: everything from updates 1–5. Changed since update 5: static/js/sales-live.js and
templates/core/sales_daily.html (script ?v=6, so browsers load the new sounds at once). No migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-6.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker live
    docker compose run --rm web python manage.py migrate
    docker compose up -d --force-recreate redis web worker live

On Daily sales, tap the speaker in the "Live" badge: Ka-ching → Coins → Counter bell → Off.
