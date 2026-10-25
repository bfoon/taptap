# TapTap update 2 — login addresses (+ everything from update 1)

This zip is cumulative: letter case, feature modules, country pricing, daily sales drill-down
AND the new Settings → Login addresses. Deploy it on its own.

    cd /opt/taptap
    git status                      # make sure nothing uncommitted would be lost
    unzip -o ~/taptap-update-2.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/   # or: git apply taptap-update-2.patch
    docker compose build web worker live
    docker compose run --rm web python manage.py migrate      # 0104 (if not applied yet) + 0105_login_addresses
    docker compose up -d

After deploying:
1. Settings → Login addresses → add e.g. yourbusiness.wifi (opens the online voucher sign-in).
2. Portal Studio → put the login page on your routers again (it now contains the forwarding).
   Saving an address re-sends it automatically for routers that already have the portal.
3. On a phone joined to the Wi-Fi (not logged in), open http://yourbusiness.wifi/

Note: your repo already had model changes without a migration (memberpaymentintent / memberportalevent);
`makemigrations` will now create those as 0106 — unrelated to this update.
