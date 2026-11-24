# Sync control: you decide when and how TapTap syncs each router

**Migration 0130** (one new table: `core_routersyncpolicy`). No new packages.

## What you get
Open it from **Routers › ⋯ › Sync control**, or with the **Sync control** button on a router's TapTap Link page.
There is one tab per router.

| Control | Choices | What it does |
|---|---|---|
| **Full sync** | Automatic (every 15 min … 1 day) · Only in quiet hours · Only when I press Sync | Sets when TapTap reads all of the router's tables. **Sync now** always works. |
| **Busy guard** | on/off + a CPU level (40–95 %) | Automatic syncs wait while the router's CPU is at or above the level. A sync that has waited 6 hours runs anyway, gently, so TapTap's copy of the router never goes stale. |
| **Pace** | Normal · Gentle | Gentle sends one table per check-in, and the next only after the router delivered the last one. TapTap also goes gentle by itself while the router is busy. |
| **New voucher batches** | At once · In quiet hours · When I press Send | Generated batches wait, marked "Waiting", and are sent in quiet hours or with **Send N waiting vouchers now**. |
| **Apps & sites samples** | Follow the router's load · Only in quiet hours · Off | Controls the DNS and connection samples used by the Traffic pages. |
| **Quiet hours** | any window, also across midnight | **Use my quietest hours** picks the 4 hours with the least traffic over the last 14 days. |

**Same for my other routers** copies the choices to every router of the business.

### Never held back
- **Customers waiting:** a voucher a customer types at the login page goes at once, even when it is waiting
  (login guarantee).
- **Customer-facing vouchers:** single vouchers, online sales and Bonanza rewards go at once.
- **Voucher changes:** switch off, freeze, extend and the like go at once.
- **Explicit actions:** Sync now, Send now, and a router's very first check-in.

### The page
- **The 24-hour dial** shows:
  - quiet hours as a green arc;
  - traffic for each hour;
  - recent syncs as dots (yours, automatic, failed in red);
  - a needle for now.
- **It also shows:**
  - the router's live CPU;
  - what TapTap decided and why ("waits: the router is busy (CPU 88 %)", "next one at about 16:16");
  - a running sync's progress;
  - the last 12 syncs with how long they took and who started them.

## The disconnections
While the router prints a big table (hotspot users, hosts, leases…), it is too busy to check in on time. TapTap
counted that silence as **offline**, so a sync could produce "stopped calling in" and "connection unstable".
- **Busy, not offline:** while a sync table is being printed, up to 5 minutes of silence count as busy.
- **No more pile-up:** the busy guard and the gentle pace stop the router being pushed to 100 % in the first place.
- **No more half-finished gentle syncs:** a gentle sync's tables now wait up to 90 minutes, instead of expiring
  half-way through after 30.

## Defaults (nothing changes until you save)
- **Routers:** TapTap Link routers keep syncing every 30 minutes, with the busy guard at 75 %. Direct API routers
  sync only when you press Sync, unless you choose Automatic.
- **Vouchers** go at once.

## Old code removed
- The old fixed-interval logic in `agent.auto_sync`. It now asks `sync_control.start_if_due`.
- The early retry after a failed sync bypassed every rule. It now goes through the same check, so it respects
  manual mode, quiet hours and the busy guard.
- Unused imports in `link_breath.py`, `link_sync_reliability.py` and `agent_inventory.py`.

## Files
- **New:**
  - core/models_sync.py
  - core/migrations/0130_router_sync_policy.py
  - core/sync_control.py
  - core/views_sync_control.py
  - templates/core/sync_control.html
  - static/js/sync-control.js
  - core/tests/test_sync_control.py
- **Changed:**
  - core/agent.py: auto sync, gentle pace, samples, Held excluded
  - core/agent_inventory.py
  - core/apps.py
  - core/link_breath.py
  - core/link_sync_reliability.py
  - core/live.py
  - core/models.py: busy grace, model import
  - core/sync.py
  - core/sync_doctor.py
  - core/tasks.py
  - core/voucher_fastlane.py
  - core/voucher_make.py
  - templates: routers.html, router_link.html, vouchers.html, voucher_detail.html, voucher_card.html

## Apply
    git apply taptap-sync-control.patch        # or copy the files from the zip
    python manage.py migrate
    # restart web, worker and beat

## Tests
There are 24 new tests. The full suite passes: **988 tests, 0 failures**, run with a local cache
(`CACHE_URL=locmem://`). Without a reachable Redis, 38 older tests fail the same way with or without this change.
