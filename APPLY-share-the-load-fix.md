# TapTap update — "Share the load" takes the second line off Standby

Built on 965212b. No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-share-fix.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

Then on Router control → Live load balancing press **Share the load with it** on ether5 once more
(or Internet lines → step 3 → Apply). It switches off the router's own steering rules (see below) this time.

## What kept ether5 on Standby
1. **Hand-made steering on the router.** TapTap K has its own marks / routing rules sending customers into its own
   table `starlink2` (out of ether1 only). They come first in the firewall and mark every new connection; TapTap's
   sharing only shares connections nobody has marked yet, so nothing moved. Now, when TapTap runs two or more lines,
   it switches those rules off (comment marked `~tt-wan-off`). The safety timer, Undo, and the undo script put them back.
   Rules for one destination (a VPN, an office network) and every TapTap rule are left alone.
   The Internet lines preview says how many it will switch off.
2. **The card misread TapTap's sharing.** Each line's routing table also lists the other lines as fallbacks; the card
   credited every table to the first line, so ether5 always showed "plan 0% · Standby".
3. **The card read an old copy.** The live view reads marking rules from TapTap's stored copy of the router, which was
   not refreshed after the change. It is now re-read right after an apply (live routers) and once a TapTap Link change
   is confirmed or undone.

## New on the card
* A standby line says **why** (backup line / no sharing rule / its health check does not answer through it).
* **Try another health check** when a line's check address (e.g. 8.8.8.8) does not answer through it — some 4G
  providers block it, and then RouterOS treats the line as dead. TapTap picks another address and shares again.
* A warning names any hand-made steering rules it finds.

Also fixed: a hand-made rule with no comment would not have been switched back on by Undo.

Changed: core/line_hub.py, core/routeros_analysis.py, core/views_wan.py, core/wan.py, core/wan_link.py,
static/css/netviz.css, static/js/netviz-lb.js, templates/core/router_control.html, templates/core/topology.html.
New: core/tests/test_share_load.py.

Tests: `python manage.py test core.tests.test_share_load` (7). Full `core` suite: no new failures.
