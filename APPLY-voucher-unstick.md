# TapTap update — vouchers cut off early, then refused ("Can't connect?" + Fix it)

Built on master (937de1e). **No migration.** No new Python or app packages.

## What was wrong

A customer was online, got cut off, and then the router refused every login, although TapTap showed the voucher as
active with hours left. Reset devices and a priority sync did not help.

**Cause:** RouterOS stops a hotspot user when its `uptime` counter reaches `limit-uptime`. TapTap wrote that limit as
a fixed number, worked out from **its own copy** of the router's counter. On a busy TapTap Link router like
KerrSering (thousands of users), that copy can be hours old:

* The user list comes in pieces.
* A check-in lists at most 300 sessions.

So the limit could land **at or below** what the router had really counted:

1. The session ended early ("uptime limit reached").
2. The next login was refused.
3. Every re-send wrote the same too-small number again: Reset devices, priority sync and the login self-heal all
   did this.

## The fix

### 1. The router counts, TapTap adds the time left

For every used voucher and member, TapTap now sends **the time left**, and the router writes:

    limit-uptime = its own live counter (uptime + sessions still running) + time left × device slots

This applies to the voucher pushes, Reset devices and the automatic router-time corrections on TapTap Link. Direct
API and Tunnel routers do the same: TapTap reads the counter from the router just before writing.

* The fixed number is still written first, as a fallback.
* Unused, unlimited, imported and time-up vouchers are unchanged.
* TapTap still ends every voucher at exactly its end time.

### 2. "Can't connect?" — on the voucher page, in Network tools and in the app

The page lists everything that stops the voucher, worst first, and says what fixes each one:

* **Switched off in TapTap:** frozen, disabled, time up or in the bin. Shown as **You decide**; it is never "fixed"
  behind your back.
* **The router's copy of the voucher:** missing, switched off, out of time (*"used 9h of 8h"*), or locked to one
  phone's MAC.
* **Devices:** all device slots taken, a phone locked out for wrong codes, a phone blocked for Internet sharing, or a
  phone on the blacklist.
* **Waiting commands:** an old *disable* or *sign-out* still on its way to the router.
* **The router's own refusal**, as the login page reported it, e.g. *"has reached uptime limit, 3× in 15 min"*.

### 3. Fix it

One button does all of this:

1. **Reset devices**, done properly.
2. Lifts the sharing block on this voucher's own phones.
3. Sends one router command that switches the user on, frees it for any phone, sets its time from the router's own
   counter, and ends stuck sessions, cookies and hosts.
4. **Reads the router back.** You see what the router really has, for example:

   > ✔ Switched on · Any phone may use it · The router allows 20h more (used 4h of 1d) · 1 stuck session ended

   If something is still wrong, it says so, for example: *"1 of its phones is BLOCKED in Hotspot › IP Bindings"*.

* **Where to find it:**
  * Voucher page: **Can't connect?**
  * Network tools: **A voucher can't connect?** (type the code)
  * App, on the voucher: **Can't connect?**
* **Who can use it:** people with voucher-support rights (the same as Reset devices).
* **A router that is only late** (busy) still gets the fix. It runs at the router's next check-in, and the page waits
  for the read-back.

### 4. It also fixes itself

If the router refuses the same voucher again at the login page, TapTap runs the router part of Fix it by itself. It
does the same straight away if the refusal is *"uptime limit"*. This happens at most once every 10 minutes per
voucher. It does not sign anyone out and does not forget any devices.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-voucher-unstick.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

## The app

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

## Tests

    python manage.py test core.tests.test_voucher_unstick core.tests.test_voucher_fastlane core.tests.test_voucher_ready

The full suite gives the same results as before. The tests that already fail without Redis are unchanged.
