# TapTap update — vouchers go to the router first, "sync this voucher now" keeps trying

Built on the voucher fix update (branch `voucher-unstick`, which includes it). **No migration.** No new packages.
If you have not applied `taptap-voucher-unstick.zip` yet, this zip contains everything from it too.

## What was wrong

On the TapTap Link page, a *Traffic sample (apps & sites)* went to the router right away, while *Disconnect AFE3R*,
*Switch off 1 voucher whose time ran out* and *Remove 2 deleted vouchers* had been **Waiting for the router** for
minutes.

* **Why the voucher changes waited:** each check-in takes turns. Once the "other work" had waited 15 seconds, it got
  the whole next check-in, and the voucher commands went to the back of that check-in. The traffic samples are big,
  so they filled it and the vouchers did not fit. A busy router repeated this again and again.
* **Why "sync this voucher now" (Fit, priority sync) was slow:** it only marked the voucher *Pending*. If another
  voucher batch was already waiting, it went behind it, and nobody checked that it ever arrived.

## What changed

### Vouchers always go first (every check-in)

* Voucher commands lead every check-in: disconnects, switch-offs, removals and voucher sends. When it is the other
  work's turn, vouchers keep up to half of the check-in, so a full sync still gets through during a long backlog.
* **Traffic samples step aside.** They are not created or sent while a voucher change is waiting, and they come back
  at the next 2-minute round.
* **Waiting vouchers go in the order customers feel it:**
  1. the ones you asked to go first
  2. vouchers in use
  3. sold vouchers
  4. the newest stock
* **A busy router is called back fast:** if it is still running the last voucher batch and more are waiting, it is
  told to check in again in 2 seconds instead of at its normal interval.

### "Send to the router first", and it keeps trying

* **Fit:** saying *"sync AVA8E now"*, *"put this voucher first"* or *"keep trying to put it on the router"* makes Fit
  offer a sync for those codes. When you approve it:
  1. Each voucher gets its own small command at the very front of the router's line (up to 50 at a time).
  2. The router is asked to check in every few seconds.
  3. **TapTap watches each voucher until the router confirms it.** Each check-in it looks again:
     * **confirmed** → done. The voucher history says *"On the router — confirmed N s after it was sent first"* and
       the live feed shows it.
     * **lost, failed or expired on the way** → sent again, at most every 20 seconds.
     * **no confirmation after 15 minutes** → it stops and says so in the history and the live feed.
* **Voucher page:** More → **Send to the router first**.
* **App:** on the voucher, **Send to router first**.
* **Direct API / Tunnel routers:** the voucher is written at once, and retried every few seconds until it works.
* **What is never forced:** expired, deleted and router-made vouchers are reported instead of sent.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-vouchers-first.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

## The app

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

## Tests

    python manage.py test core.tests.test_voucher_priority core.tests.test_voucher_unstick core.tests.test_sync_turns

The full suite gives the same results as before (the failures that already fail in a test environment without Redis
are unchanged). `test_voucher_priority` also runs the fair-turn tests with a working cache.
