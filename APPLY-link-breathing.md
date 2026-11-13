# TapTap update: less TapTap traffic on the router ("breathing" check-ins)

**No migration.** No new packages. Nothing to change on the routers: the script on the router is not touched.

This zip is cumulative: it also contains the calls / call-back / voice update (migration 0120,
APPLY-calls-ring-and-callback.md). If you applied that one already, copying it again changes nothing.

## Where the traffic came from

A TapTap Link router checked in **every 10 seconds, day and night** — 8 640 times a day. Each check-in makes the router:

* open a new secure connection to TapTap (the costly part for a small MikroTik CPU);
* read up to 300 hotspot sessions, 200 bypassed hosts and 60 interfaces, one by one;
* upload that list (about 20 kB with 300 customers online — roughly 170 MB a day).

On top of that, a **full sync ran every 30 minutes**, even when the last ones found nothing to repair.

## What changed

### 1. The check-in pace follows the work

At the end of each check-in TapTap decides how soon it needs the next one. When that differs from what the router
does now, it adds one line to its answer that changes the interval of the `taptap-link` scheduler.

| Pace | Check-in every | When |
|---|---|---|
| **Fast** | 10 s (the router's own interval) | a customer is at the login page · a voucher or command is waiting, or was sent in the last 2 minutes · a full sync is running · someone has the router's Link page open |
| **Calm** | 20 s | customers online, nothing waiting |
| **Resting** | 30 s | nobody online, nothing waiting (night) |
| **Stepping back** | 30 s | the router's CPU is at 85 % or more |

* The next check-in after work appears puts the router back on Fast.
* The wanted pace is sent again every 10 minutes, so a router whose script was reinstalled falls back in line.
* A router is allowed four intervals of silence before it counts as offline, so the slower pace causes no false
  "offline" alerts.

**The trade-off:** when the router is on Calm, a brand-new voucher or command waits for the next check-in — up to
20 seconds instead of 10 (30 when nobody is online). After that first one it is on Fast again.

### 2. Full syncs spread out while they find nothing

After 2, 3, 4 automatic full syncs in a row that healed nothing, the next waits **60, then 120 minutes** (never
more). It goes straight back to 30 minutes when a sync heals something, a sync fails, or the router restarted.
A sync you press always runs at once.

### 3. You can see it

The router's TapTap Link page has a new line, for example:

> TapTap's load on this router: **Calm** — a check-in every **20 s** (nothing is waiting), back to 10 s the moment
> there is work · today **4 610** check-ins, about **96 MB** of data — **3 120** check-ins saved (40 %, about 65 MB)
> · full sync every **120 min** (the last 4 syncs found nothing to repair)

The numbers are measured on TapTap's side from the real check-ins. The data figure includes an estimate of 5.5 kB
per check-in for the secure connection.

## Settings (`.env`, all optional)

| Setting | Default | Meaning |
|---|---|---|
| `LINK_BREATHING` | 1 | 0 switches all of this off (routers are put back to their own interval) |
| `LINK_CALM_FACTOR` / `LINK_CALM_MAX` | 2 / 30 | Calm = interval × factor, at most this many seconds |
| `LINK_REST_FACTOR` / `LINK_REST_MAX` | 3 / 45 | Resting and Stepping back |
| `LINK_EASE_CPU` | 85 | CPU % from which TapTap steps back |
| `LINK_FAST_HOLD` | 120 | seconds a router stays Fast after work |
| `LINK_SYNC_MAX_MINUTES` | 120 | longest wait between automatic full syncs |

## What was not tested, and what to watch

* The line TapTap sends (`/system scheduler set [find name="taptap-link"] interval=20s`, inside `:do {…} on-error={}`)
  was **not run on a real router here**. If a router refuses it, nothing breaks: it simply stays at 10 seconds.
  After the update, open one router's Link page: within a few minutes the line should say Calm or Resting, and
  "Check-ins usually every" should move to about 20 s. On the router: `/system scheduler print where name=taptap-link`.
* It needs heartbeat script v4 or newer (older ones are left alone).
* If a login feels slower right after selling a voucher, set `LINK_CALM_MAX=15`, or `LINK_BREATHING=0` to go back.
* Direct API and Tunnel routers are not changed by this update.

## A bigger saving, not done yet

The check-in script on the router still reads each session one by one. Making the script itself lighter (reading
the session list only when TapTap asks for it) would save more, but it changes the script on every router and has
to be tried on a real MikroTik first.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-link-breathing.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

## Tests

    python manage.py test core.tests.test_link_breath core.tests.test_link_rhythm

The full suite passes (607 tests).

## Files

New: core/link_breath.py, core/tests/test_link_breath.py
Changed: core/agent.py (the check-in answer, automatic sync spacing), core/models.py (offline allowance),
core/views_link.py, templates/core/router_link.html
