# TapTap update — Speed per device: every device gets the whole plan speed

**Migration `0123_device_lanes`.** No new Python or app packages. Built on master (`1cd1759`).

This zip holds the **complete files**, and it **includes the Connection lab update (build 2)**. Unzip it over a
server with or without that update: everything it needs is inside.

## How a speed limit is issued today

* A **plan's** speed (e.g. `2M/2M`), a **member plan's** speed, or a **custom voucher's** own speed is written to the
  router as the hotspot **user profile's** `rate-limit`, together with `shared-users` = the number of devices.
  Members work the same way: their member plan sets the profile and the speed.
* RouterOS then makes **one queue per signed-in device**, targeting only that device's address.

So **by design, 2M/2M with 3 devices is already 3 × 2M/2M** — each device its own 2M/2M, not one 2M/2M split three
ways. That holds *when nothing on the router gets in the way*. Five things do, and until now TapTap checked none of them:

| What | What the customer gets |
|---|---|
| A **parent queue** on the profile (Mikhmon's "Parent queue" field, or set by hand) | every device of that profile shares the parent's limit |
| A **hand-made queue above** the hotspot's, targeting a whole network or an interface | it catches the devices first: they all share it |
| A profile **without a speed** while the hotspot **server** profile has one | everybody on the hotspot shares one server queue |
| The plan's speed **changed while devices were online** | they keep the old speed until they sign in again (RouterOS reads the profile only at login) |
| **FastTrack** in the firewall | fast-tracked traffic skips *every* queue: devices get **more** than their plan |

There was also a TapTap bug: on **TapTap Link** routers, speed-rule queues (Traffic → Speed control) were added at the
bottom of the queue list, *below* the hotspot's own per-device queues, so they never reached a signed-in device
(Direct API routers were right). **Fixed** — see *Behaviour change* below.

## What this update adds

### 1. Speed per device on the voucher page and the member page

One lane per device slot, drawn as a road: each online device with its own speed live against **its own** plan
speed, free slots shown as dashed lanes ("gets its own 2 Mb/s when it signs in"), and the plain math:

> Plan speed **2M/2M** is per device. Each of its 3 devices gets 2.0 Mb/s down · 2.0 Mb/s up of its own — up to
> 6.0 Mb/s down for the whole voucher when all 3 are busy.

* **Check on the router** — asks the router which queue really carries each of its devices, and colours each lane:
  *own lane*, *sharing*, *no limit*, *old speed*, *squeezed* (under a parent queue).
* **Guarantee each device its own lane** — TapTap itself puts each device in its own queue at the plan speed,
  above everything else on the router, at once — also for devices already online. It ends when the voucher's time
  ends, or with **Stop the guarantee**.

### 2. Traffic → Speed per device

* **Always guarantee it** (one switch): every device of every voucher **or member allowed 2+ devices** gets its own
  lane at the plan speed, enforced by TapTap on every router, all the time. One small queue per device online.
* **Check every shared voucher** per router: devices with their own lane, sharing a queue, with no limit at all,
  at an old speed or squeezed — and the reason for each in plain words.
* **Fix it**, with a tick box for each part:
  * guarantee the vouchers that were found sharing (each device its own queue now);
  * fix the router profiles — TapTap's speed written back, a squeezing parent queue removed from TapTap's own
    profiles — so every new sign-in gets its own lane;
  * keep hotspot customers out of **FastTrack** (two accept rules for *established/related* traffic of the hotspot
    network just above the fasttrack rule — new connections still meet every drop rule; the router works a
    little harder).
  The fix is read back from the router. TapTap never deletes a queue you made yourself: lanes go above it.
* **Vouchers guaranteed one by one**, with Stop.
* **What each shared plan really gives**: each device, and the whole voucher with all devices busy.

### 3. On the plan forms

Under the speed box of voucher plans and member plans, as you type:

> Per device: each device gets ↓ 2.0 Mb/s · ↑ 2.0 Mb/s of its own — **up to ↓ 6.0 Mb/s** for the whole voucher when
> all 3 devices are busy.

Plan lists and the plan page now say **each device** next to the speed.

## How the guarantee works (and why it is safe)

Lanes ride on the engine that already runs Speed control on every live-sync pass (`core/traffic_speed.py`): one
`TTSPD-…` queue per device, above the hotspot's own queues, removed when the device leaves; batched and
change-only on TapTap Link. Order of precedence is unchanged:

**fair usage cap (TTFUP) → your own speed rule (TTSPD) → lane at the plan speed (TTSPD) → the router profile's queue**

So a lane never overrides fair usage or a rule you made. A voucher with no speed limit gets no lane.

## Behaviour change — Speed control rules on TapTap Link routers

Speed-rule queues on **TapTap Link** routers are now moved above the hotspot's own queues (fair usage stays first),
as Direct API routers already did. A rule such as "Day plan: 1 Mb/s" therefore now **really applies to signed-in
devices** on Link routers too — before, it only reached bypass devices there. Check your rules in
Traffic → Speed control after updating.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-speed-per-device.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core          # REQUIRED — 0122 (if not run yet) and 0123
    docker compose up -d --force-recreate web worker vouchers live

Check it worked:

    docker compose exec web python manage.py showmigrations core | tail -3     # 0122_flow_lab and 0123_device_lanes: [X]

Then reload pages once with **Ctrl+F5** (new `lanes.js`, `flow.js?v=3`, `flow.css?v=3`).
Nothing needs changing on the routers.

**If something looks wrong:** a yellow note "…has not run its database step yet" means `migrate` did not run — run
it and reload; speeds keep working meanwhile, only guarantees wait. Any other error is shown as a sentence on the
page, with the details in `docker compose logs --tail=200 web`.

New: `core/device_lanes.py`, `core/migrations/0123_device_lanes.py`, `static/js/lanes.js`, `core/tests/test_device_lanes.py`.
Changed (on top of the Connection lab update): `core/models_flow.py`, `core/models.py`, `core/models_nettools.py`,
`core/conn_lab.py`, `core/nettools.py`, `core/traffic_speed.py`, `core/agent.py`, `core/views_flow.py`,
`core/views_link.py`, `core/urls.py`, `core/permissions.py`, `static/js/flow.js`, `static/css/flow.css`,
`templates/core/traffic.html`, `voucher_detail.html`, `member_detail.html`, `plans.html`, `members.html`,
`catalog_plan_edit.html`, `plan_detail.html`, `README.md`.

Tests: `python manage.py test core.tests.test_device_lanes core.tests.test_flow_lab` (126).
