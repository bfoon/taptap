# TapTap update — where a voucher's traffic goes, and why it is slow

**Migration `0122_flow_lab`.** No new Python or app packages. Built on master (`1cd1759`).

> **Build 2** (this zip) fixes the Connection lab answering **"TapTap answered 500."** on a server where the update's
> database step had not run yet. The lab now opens either way, tells you what to run, and no Connection lab / Traffic
> tab ever shows a bare 500 again — any error comes back as a sentence in the page, with the details in the server log.

Five things, all driven from what live sync already collects, so nothing new is polled on a router unless somebody
presses a button.

## 1. Which internet line a voucher goes through — and Switch

On a router that shares customers over two lines (PCC, "Share the load"), every connection carries that line's
connection mark. TapTap already samples the connection table once a minute for the Traffic page, so it now also
records the mark: **which line each customer is on costs nothing and is already there.**

* **Voucher page / device page / Traffic → Internet lines**: the lines, the share you designed next to the share
  that is really there, and who is on each one.
* **Switch** moves one customer to the other line. It adds **one** mangle rule above the sharing rules — a copy of
  the chosen line's own rule, for that phone's MAC only (its IP when no MAC is known) — then closes that device's
  open connections so it changes line at once instead of connection by connection. Routes, routing tables, NAT and
  the sharing rules themselves are never touched, so a switch cannot take a line down.
* **Back to automatic** removes the rule. A rule removed in WinBox, or wiped by re-applying Internet lines, is
  noticed at the next check and TapTap's record is cleared.
* The rule's comment is `TapTap line-pin <key> <code> to <line>`, so it is yours to find and remove by hand.
* A router with one line, a backup line or combined lines says why there is nothing to switch.

## 2. Idle vouchers (Traffic → Idle vouchers)

Two kinds of waste, both read from the session counters live sync already reads:

* **Signed in, no traffic** — TapTap now remembers, per session, since when it has been *silent* (under 1 kb/s) or
  merely *calm* (under 10 kb/s: background chatter only). Pick the level and "for 10 minutes or more".
* **Time running, not online** — vouchers that were started, whose time is ticking away, with nobody signed in.
  These are customers who paid and are getting nothing; each row goes straight to **Can't connect?**.

## 3. Slow vouchers (Traffic → Slow vouchers)

Everyone being held back right now, worst first: by fair usage, by a speed rule, or because they are using all of
their plan's speed — with the speed they are doing against that limit.

**Scan the routers for drops** asks each router which speed limits have *dropped packets*. A limiter drops packets
when the customer asks for more than the limit gives: that is what "slow" feels like, and it tells you whether the
plan is too small or something else is wrong.

## 4. DHCP lease time, in two easy places

**Traffic → DHCP lease** and **Network tools** (bottom of the page) list every DHCP server of a router with its
lease time, how full its address pool is, and advice:

* under 30 minutes on a hotspot → sleeping phones lose their address and are signed out ("lost dhcp lease" in
  *Why people drop*);
* a pool 85% full → the addresses of people who left are still held; a shorter lease frees them before new
  customers are refused.

One click sets 10 min … 3 days (or any number of minutes) on one network or on all of them at once, and the value
is **read back from the router**. Nobody is disconnected: devices take the new time when they next renew.

## 5. Connection lab — "why is this voucher slow?"

On the **voucher page**, the **device page** (a live strip with speed, quiet time and line, plus the button), from
**Network tools** by typing a code, and from every row of the new Traffic tabs.

A road from the phone to the internet fills in as the router answers:

**Phone → Wi-Fi/cable → Speed limit → Internet line → Internet**

* **Device check**: pings the phone from the router reply by reply, its Wi-Fi signal and rate (wireless, wifi and
  CAPsMAN), the port it comes in on, its address lease, its hotspot session.
* **Speed check**: every speed limit on its address with the speed it is doing *and the packets that limit has
  dropped*; which line its connections use; how the router's own internet answers at that second; router CPU; its
  last sign-outs from the router's log.
* **The verdict** names the slow part in plain words — the phone's Wi-Fi, a weak signal, the plan's speed being
  fully used ("a faster plan is the fix, not the router"), fair usage, the internet line — and offers the one
  button that helps: Switch the line, DHCP lease, Can't connect?, or give full speed back.
* Live speed against the limit, drawn every 5 seconds with the limit as a dashed line.

Every router check is an ordinary TapTap Link / API test, so it also appears in **Network tools → Recent tests**
with who ran it. Direct API and Tunnel answer at once; TapTap Link answers at the router's next check-in and the
page waits, as it already does for the other tests.

## Safety

* Lab commands are built by TapTap, never from typed text, and `core/agent.queue` checks each one again before it
  reaches a router: only private addresses, only this router's own line marks, only the fields of a sharing rule,
  and the comment must be TapTap's own. A tampered switch is refused and nothing is sent.
* Reading needs voucher-support or network rights; the Traffic tabs and DHCP need network rights.

## Server

The zip holds the **complete files** (not a patch), under `taptap/`, in the same places as in the repository.

    cd /opt/taptap
    unzip -o ~/taptap-flow-lab.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core          # REQUIRED — creates the line-switch table
    docker compose up -d --force-recreate web worker vouchers live

Check it worked:

    docker compose exec web python manage.py showmigrations core | tail -3     # 0122_flow_lab must show [X]

Then reload the page with **Ctrl+F5** once (the browser must take the new `flow.js?v=2`).
Nothing needs changing on the routers.

### If something is still wrong

* **The lab shows a yellow note "…has not run its database step yet"** — the `migrate` line above did not run (or
  ran in another container). Run it, then reload. Everything except *Switch* works until then.
* **"TapTap had an error (…)"** — the sentence names the error; the full traceback is in
  `docker compose logs --tail=200 web`. Send that part and it can be fixed precisely.
* **Old behaviour after the update** — the browser kept the old script: Ctrl+F5.

New: `core/line_steer.py`, `core/dhcp_lease.py`, `core/conn_lab.py`, `core/traffic_idle.py`, `core/views_flow.py`,
`core/models_flow.py`, `core/migrations/0122_flow_lab.py`, `static/js/flow.js`, `static/css/flow.css`,
`core/tests/test_flow_lab.py`.
Changed: `core/traffic.py`, `core/nettools.py`, `core/agent.py`, `core/models.py`, `core/models_nettools.py`,
`core/urls.py`, `core/permissions.py`, `templates/core/traffic.html`, `templates/core/voucher_detail.html`,
`templates/core/device_detail.html`, `templates/core/network_tools.html`, `README.md`.

Tests: `python manage.py test core.tests.test_flow_lab` (83).
