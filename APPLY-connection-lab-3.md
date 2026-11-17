# TapTap update — Connection lab on busy routers (build 3)

**Migration `0124_lab_steps`.** No new packages. Built on master (`1cd1759`).

This zip holds the **complete files** and **includes the two earlier updates** (Connection lab build 2, Speed per
device). Unzip it over a server with or without them: everything is inside.

## What went wrong (seen on "TapTap K")

Both router checks failed after 6 minutes, and the lab had nothing to show:

* **"The speed check did not finish — could not hand the test to the router."** A busy TapTap Link router's check-ins
  are full of voucher changes. Behind them TapTap keeps room for **one** Network-tools test, but the lab sent **two**,
  so the second one never got on board.
* **"The device check … took the test but did not answer."** That check was ~4,200 characters. Commands over 3,600
  travel **unprotected**: if the router's RouterOS refuses anything in one, the whole check-in stops silently — no
  answer and no error, just six minutes of waiting.
* **An empty lab.** TapTap already knew the live speed, the limit, the internet line and the last sync, but showed
  none of it until the router answered, and a failure became the red headline.

## What changes

### 1. Two small, protected steps instead of two big checks

* **Quick check** — does the phone answer (5 pings), its speed limits with dropped packets, which internet line.
* **Details** — Wi-Fi signal, port, address lease, the router's own internet, CPU, its last sign-outs.

Each step fits one **protected** command even in the worst case (long code, long server name, four internet lines),
so a router that refuses part of a step answers **"failed"** with the reason instead of going silent. The quick step
looks up this voucher's queues **by name** instead of walking every queue on the router — much lighter on a router
with thousands of customers. Wi-Fi is read only from the menu the router really has (known from its last sync).

### 2. An express seat for tests a person is watching

On **RouterOS 7**, a lab step rides **behind** whatever the check-in carries — vouchers still go first and are never
delayed. Your test reaches the router at its **next** check-in instead of waiting for a free one. RouterOS 6 keeps
the old rule.

### 3. A lab that is never empty

The road fills **at once** from what TapTap already knows, drawn with **dashed** rings:

* **Phone** — online now (live sync), quiet time.
* **Wi-Fi / cable** — the port and Wi-Fi signal from the router's **last sync** ("−83 dBm · 15 min ago").
* **Speed limit** — the plan / fair-usage / rule limit, with the live speed against it.
* **Internet line** — from the traffic sample.
* **Internet** — on TapTap Link, the router reached TapTap seconds ago, so its internet works.

As each router step answers, its stations turn **solid** (*measured*). The headline says which it is based on.

### 4. A router-check strip that tells you where things are

Instead of a spinner and a red box, each step shows a line of facts:

> **Quick check** — Waiting for TapTap K's next check-in — last one 6 s ago, usually every ~12 s · 14 voucher changes
> go first · express seat · 18 s
> **Details** — Handed to TapTap K 3 s ago — measuring now
> **Quick check** — Measured in 9 s

A step that does not come back is **retried once automatically**; after that it says why, with facts, and offers
**Try again** — while the verdict stays on what TapTap knows.

### 5. Every late test explains itself (Network tools too)

> TapTap could not hand the test to TapTap K: it checked in 9 times in 6 minutes, but every check-in was already full
> (31 voucher changes went first — customers waiting at the login page).

> TapTap could not hand the test to TapTap K: it did not check in during those 6 minutes (last heard at 14:01) …

> TapTap K took the test at 14:02:13 but its answer never reached TapTap (4 check-ins since, so the router is alive) …

> TapTap K took the test but stopped with an error (this RouterOS version refused part of it) …

And if a router's answer arrives but cannot be read, the test now says so (the details go to the server log)
instead of the router being told to retry for six minutes.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-connection-lab-3.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core          # 0122, 0123 (if not run yet) and 0124
    docker compose up -d --force-recreate web worker vouchers live

Check it worked:

    docker compose exec web python manage.py showmigrations core | tail -4     # 0122 … 0124 all [X]

Then reload with **Ctrl+F5** (new `flow.js?v=4`, `flow.css?v=4`). Nothing needs changing on the routers.

Changed in this build: `core/conn_lab.py`, `core/nettools.py`, `core/agent.py` (express seat), `core/views_flow.py`,
`core/models_nettools.py`, `static/js/flow.js`, `static/css/flow.css`, the templates' version numbers;
new `core/migrations/0124_lab_steps.py`, `core/tests/test_lab_steps.py`.

Tests: `python manage.py test core.tests.test_lab_steps core.tests.test_flow_lab core.tests.test_device_lanes` (149).
