# Sync control is now easy to find on the MikroTik Control page

No migration, no new packages. Builds on 33ded0a (Sync control is already in it).

## What changed on Routers (MikroTik Control)
- **A "Sync control" button at the top**, next to "Full sync all routers". It opens the first router; the other
  routers are tabs on that page.
- **A chip on every router card** that shows how that router syncs and opens its Sync control:
  - `Sync: Auto · every 30 min` (blue)
  - `Sync: Quiet hours · 01:00–05:00` (green)
  - `Sync: Manual` (purple)
  - `· gentle` is added when the gentle pace is on, and an orange **"N waiting"** badge appears when new vouchers
    are held for that router.
- The **⋯ menu** item "Sync control" is still there.

The chips come from two database queries for the whole page, so the page does not get slower with more routers.
If anything about Sync control fails, the chip is left out and the page opens as before.

## Files
- core/sync_control.py: `card_summaries()`
- core/views.py: the routers view adds `router.sync_ctl`, in a try/except
- templates/core/routers.html: the top button, the card chip and its styles (light and dark)
- core/tests/test_sync_control.py: 2 new tests, one of them checks the page still opens when Sync control errors

## Apply
    git apply taptap-sync-control-routers.patch    # or copy the files from the zip
    # restart web

## Tests
Full suite: 990 tests, 0 failures, run with a local cache (`CACHE_URL=locmem://`).
