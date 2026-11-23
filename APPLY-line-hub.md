# TapTap update — Line Hub: every gateway, who is on which line, one click to move them

No database migration. Built on the current `main` (3f944cd).

    cd /opt/taptap
    unzip -o ~/taptap-line-hub.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout: `git apply taptap-line-hub.patch`)

## What changes

**Internet lines page → new "Gateway control room" at the top**
* Live stage: customers → router → one card per gateway, with traffic flowing along the wires (busier line = more
  and faster dots). Each card: customers on it, ↓/↑ speed now, real share vs designed share, state
  (in use / standing by / out of use / off / not connected / port missing), TapTap and NAT badges.
* Click a gateway: switch it on/off, in/out of use, NAT on/off, make it carry TapTap Link, rename / speed /
  PPPoE login / DNS from modem, move its customers (some or all), make it the main line, delete it.
* Tabs: **Customers on lines** (drag-and-drop board) · **Gateways** (WAN list + add a new line on a free port:
  DHCP / static / PPPoE) · **NAT** · **Firewall** (3 one-switch protections + switch any non-TapTap rule) ·
  **DHCP** (lease time, DNS, on/off) · **TapTap line** · **Load sharing**.

**Traffic → Internet lines** uses the same board for every router: drag one customer, or tick many (or
"Select all" on a line) and use the bar at the bottom. Hand-kept customers are listed with "Back to automatic".

**Who is on which line now works for any setup.** The once-a-minute traffic sample also records the address each
connection leaves from (the line's own address after NAT), so customers are placed on their real line — with
TapTap's setup or a hand-made one.

**Moving customers now works with "Backup line" and "Combine lines" too** (before: only "Share the load").
Every line's routing table now holds the other lines as fallbacks, so a moved customer keeps the internet if
their line fails. **Apply your setup once more (step 3)** so existing routers get those fallbacks.

## Fixes
* `“*2” is not a valid interface name`: a DHCP client left on a deleted port is shown as "port missing", never
  ticked, and can be deleted with one click (designer card or its gateway).
* `192.168.1.1@starlink2: type the gateway IP address`: RouterOS 7 gateways written as `ip@table` were taken for a
  port name. The table part is now ignored and the real port is found.
* A healthy backup line (route waiting its turn) showed as "out of use" — now "Standing by".
* PPPoE lines were never recognised by the Internet Doctor (their check route has no `%port`), so they always
  looked out of use and "Put in use" failed. Fixed for live routers and TapTap Link.
* Lines you unticked in step 1 came back ticked after a reload.

## Safety
* Every change is a small structured operation checked against an allow-list when made and again when TapTap Link
  turns it into RouterOS text; nothing from the browser runs as RouterOS text.
* Only rows marked "TapTap Line Hub" are ever removed (plus a line's own DHCP/PPPoE client when you delete it).
* A line customers use can't be deleted or lose NAT — take it out of use first. TapTap's own firewall rules are
  locked. Switching off DHCP on a HotSpot network asks twice.
* Over the API, changes that could cut TapTap off (NAT off, input firewall rules, TapTap's line) install an undo
  timer on the router first; TapTap removes it after reaching the router again.
* On TapTap Link, risky changes are refused while TapTap's copy of the router is out of date.

Changed: core/apps.py, core/line_steer.py, core/routeros_analysis.py, core/traffic.py, core/wan.py,
core/wan_doctor.py, static/js/flow.js, static/js/wan-designer.js, templates/core/traffic.html,
templates/core/wan_designer.html.
New: core/line_hub.py, core/tests/test_line_hub.py, static/js/line-board.js, static/js/line-hub.js,
static/css/line-hub.css.

Tests: `python manage.py test core.tests.test_line_hub` (27 new). Full `core` suite: no new failures.
