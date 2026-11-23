# TapTap update — Config Studio deploys always finish · lines you can click and fix

Built on 9169b40 (the Line Hub update). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-studio-deploy.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout: `git apply taptap-studio-deploy.patch`)

## Config Studio: deploys always finish
**Why deploys looped on "On its way to the router… Step 1 of 2"**: every piece waited for the router's answer to the
one before it. When an answer was lost (very likely when a board changes the internet lines — the router's own
internet blinks just as it reports back), the Link queue gave up on the command quietly and Config Studio was never
told. The change stayed "sending" for ever.

Now:
* The whole change is queued at once and travels as fast as the router checks in.
* Every step checks on the router that the one before it ran (a marker kept on the router), and is skipped when it
  is already done — so any step can safely be sent again.
* An answer that goes missing is resolved by asking the router how far it got, never guessed. A broken chain is sent
  once more in a fresh round; after that it is taken back. Nothing waits longer than 20 minutes.
* The router retries its answers (5 tries) for Config Studio, Line Hub, Internet lines and Internet Doctor commands.
  Before, a failed "ok" could even report "fail" for a command that worked.
* A lost answer to "Keep" is checked on the router instead of being reported as "undone".
* Live timeline on the right: every step (backup, undo plan, safety timer, each part, keep), what is happening now,
  when the router was last heard, plus **Check now** and **Stop**.
* **Backup is made by itself** on the router as the first step (no more "make a backup first" stop).
* **Keep it by itself** (ticked by default): kept once the router checks in again on the new setup.
* Undo timer through TapTap Link is at least 8 minutes (a chain of check-ins needs the time).

## Config Studio: no more false red errors
* Internet line tiles said "without NAT no answer comes back" although NAT was there: the virtual router read routes
  like `192.168.1.1@starlink2` (RouterOS 7, gateway in another table) and TapTap's health-checked routes as port names.
  It now finds the real port, and each Internet line tile is tested through its own line.
* `/ip/dhcp-client/gateways` (a list TapTap Link makes for itself) can no longer be used in an expert row.
* An Internet line tile on a line that TapTap's Internet lines already runs no longer gives it its own default
  route — that would have sat next to TapTap's routes and quietly broken the backup line / load sharing.

## Live load balancing (Router control, Topology) and the gateway drawer
* Click a line (its card, or its node on the map).
* **Bring it online**: TapTap works out what the line needs and does it — switches the port or the line on, asks the
  modem for a fresh address, dials PPPoE again, repairs its routes, puts it back in use — then watches it come online.
* **Share the load with it** (standby / unused lines): turns the router to "Share the load" over every working line by
  speed (one line per customer, health checks), with the safety timer, and confirms by itself once TapTap still
  reaches the router. Through TapTap Link it confirms once the router stays online. Lines that are not connected are
  left out and named.
* **Bring online & share** for a line that is down.
* Each action runs as a live checklist on the card and ends with the line carrying traffic.

Changed: core/agent.py, core/config_studio.py, core/config_studio_sim.py, core/config_studio_tiles.py, core/line_hub.py,
core/views.py, core/views_wan.py, core/views_wan_doctor.py, core/wan_doctor.py, core/wan_link.py,
core/tests/test_config_studio.py, core/tests/test_line_hub.py, static/css/config-studio.css, static/css/line-hub.css,
static/css/netviz.css, static/js/config-studio.js, static/js/line-hub.js, static/js/netviz-lb.js,
templates/core/config_studio.html, templates/core/router_control.html, templates/core/topology.html,
templates/core/wan_designer.html.

Tests: `python manage.py test core.tests.test_config_studio core.tests.test_line_hub` (80). Full `core` suite: no new failures.

A change sent with the old version that is still stuck on "On its way" is closed by itself (as failed, with an
explanation) the next time the page or the router checks — after that, deploy the board again.
