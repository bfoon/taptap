# TapTap update: Full sync heals every sync issue

**No migration.** No new packages.

This builds on branch `voucher-unstick`. The zip has everything since master, including voice messages (migration
0119) and the wardrobe (migration 0118). If you already applied those, copying them again changes nothing.

## Why "Full sync" did not fix things

A full sync **read** the router and imported what it found. It did not check TapTap's own vouchers against what it
had just read.

* **On TapTap Link routers this mattered most.** A voucher TapTap had marked *Synced* but that was no longer on the
  router stayed missing for good. Nothing ever sent it again. This happened when:
  * the router was reset, or restored from an old backup;
  * a user was deleted in WinBox;
  * a batch was cut short.
* **The other repairs were scattered.** Profiles, time limits and expired vouchers each had a repair in a different
  place. None of them ran together at the moment TapTap had just read the router.

## What a full sync does now

Every full sync now ends with a **check of every voucher and member** on that router. This covers Direct API,
Tunnel and TapTap Link, whether you pressed **Sync**, **Full sync all routers**, asked Fit, or it ran automatically
every 30 minutes.

| Found | What TapTap does |
|---|---|
| In TapTap, **missing on the router** | Sends it again (members use the exact member alignment). |
| **On/off differs** (frozen, paused, switched off) | Sends TapTap's state. |
| **Wrong profile** | Moves it back to its plan's profile. |
| **Wrong time limit** | Corrects it. |
| **Time is up in TapTap but still on** on the router | Switches it off. |
| **Right on the router, but stuck on Pending/Error** in TapTap | Marks it *Synced*. |
| A **plan's profile is missing** on the router | Creates the plan profiles. |
| **Lost in the Link queue** (*Queued*, but no command carries it) | Releases it and sends it again. |
| **Made on the router and gone from it** | Flags the voucher (it is never deleted). |
| **Most vouchers missing** at once | Says the router looks reset or restored, and sends them all back. |

Vouchers in use and sold vouchers go first.

### Where you see it

* **Routers page, Background synchronization:** each finished sync now has a line such as:

  > 🩹 Healed 14: 6 missing · 2 on/off · 1 profile · 2 time limit · 1 expired still on · 2 stuck on Pending ·
  > **9/11 confirmed by the router**

  The *confirmed* count updates live as the router answers. **What was checked** opens the details: how many vouchers
  were compared, how many were already in sync, every kind of fix, example codes and notes. It stays open while the
  list refreshes.

  When nothing differs, the line is green: *All 412 voucher(s) match the router*.
* **Phone app, router card:** *Last full sync: Healed 3: 3 missing*.
* **Fit:** asking about a router's sync status now includes what the last full sync healed.
* **Activity log:** one line for every sync that healed something.

### Safe by design

* **Only TapTap's own vouchers and members are corrected.** Users made on the router are never overwritten, only
  flagged when they disappear.
* **Nothing is guessed.** If the router's user list did not arrive in a Link sync, or a Direct API router cannot be
  read again, vouchers are not compared. The line says why.
* **Direct API routers are read again after the sync has written.** The check sees the final state, not the state
  from before.
* **Vouchers are left alone when:**
  * they are already on their way in a Link batch;
  * they were made after the router was read;
  * their code was changed in the last 2 days (the rename moves them).
* **Codes the router refused 3 times are not retried by automatic syncs.** A sync you press retries them, as before.
* **No double work.**
  * On Link routers, time limits are already corrected by the time repair that runs right after the import.
  * Within a few seconds, a voucher is never sent twice in the same state.
* **Limits per run:** up to 3000 vouchers and 300 members. The next sync carries on.
* **Speed:** about half a second for 5000 vouchers. Shared-users for all profiles are read once, instead of with two
  queries per voucher.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-full-sync-heals.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Then press **Full sync all routers** once. Each router's line shows what was healed.

## Phone app

There is one small change: the router card shows the last sync's result. Rebuild the app as usual; no new packages.

## Tests

    python manage.py test core.tests.test_sync_doctor

They cover:

* a router in sync needs nothing;
* *Synced* but missing on the router is sent again;
* the report is saved on the job, and the live *confirmed* count works;
* Pending/Error that is right on the router becomes *Synced*;
* frozen but still on, wrong profile, and expired but still on are corrected;
* a user list that did not arrive is not guessed;
* vouchers already in a Link batch, and vouchers made after the read, are skipped;
* a router-made voucher that disappeared is flagged, not deleted;
* a router reset is recognised;
* refused codes are retried only when someone presses Sync;
* renamed codes are left to the rename;
* a missing plan profile is created;
* members use the member alignment;
* the check runs once per sync;
* Direct API: the router is read again after the sync, and an unreachable router is reported;
* the phone card and Fit show the result.
