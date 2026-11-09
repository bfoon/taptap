# TapTap update — "connection unstable" hours after each restart, and scheduled restarts that failed

**No migration.** No new packages.

This builds on branch `voucher-unstick`. The zip has everything since master: the voucher fix, vouchers first, the
wardrobe (migration 0118) and How-to. If you already applied those, copying them again changes nothing.

## 1. "Connection unstable" a while after every restart

### Why

Every 2 minutes TapTap asked each Link router for an *apps & sites* sample. That meant two whole tables, printed in
one go and uploaded:

* the entire DNS cache
* every connection with more than 20 kB

Right after a restart both tables are nearly empty, so everything looks fine. A few hours later, with hundreds of
customers, the DNS cache holds thousands of names and the connection table tens of thousands of entries. Then, every
2 minutes:

1. The router spends most of its CPU and memory printing and uploading them.
2. Its own check-ins to TapTap come late.
3. TapTap marked it offline after 90 s; a moment later it was back.
4. Three of those drops in 30 minutes gave **"connection unstable"**.

Restarting emptied the tables, and the cycle started again.

### Fix

* **The samples are much lighter.** The router now sends only the newest 600 DNS names and the newest 400 busy
  connections. It reads them one by one, instead of printing whole tables.
* **The samples follow the router's load.**

  | Router is | Samples every |
  |---|---|
  | relaxed | 2 min |
  | busy (CPU 60 %+, 250+ customers, a slow last sample, late check-ins) | 5 min |
  | struggling (CPU 85 %+, a sample took 90 s+, very late check-ins) | 15 min |

  No sample is taken while a voucher change is waiting.
* **Late is not offline.**
  * TapTap learns each router's own check-in rhythm from its last 60 check-ins. A router that is always slower in
    the evening is allowed up to 2.5× its usual slowest gap before it counts as offline. That allowance never goes
    beyond 5 minutes.
  * An answer to a command, or an upload, now proves the router is alive just like a check-in. Answers to a restart
    or shutdown do not count.
* **You can see the rhythm.** The router's TapTap Link page now shows a line like this:

  > Check-ins usually every **11 s**, slowest **38 s** · counted online for up to **95 s** of silence · apps & sites
  > samples every **5 min** (CPU 72 %, last took 24 s)

## 2. The restart schedule always failed

There were three causes:

1. **The restart was stuck behind vouchers.**
   * On a busy TapTap Link router there are always voucher changes waiting: disconnects, switch-offs, fair usage. A
     restart must travel alone, so it waited behind them, expired after 5 minutes, and the schedule said *"the router
     never picked up the command"*.
   * **Now** a restart or shutdown goes **alone at the very next check-in, ahead of everything**. It also waits up to
     15 minutes for a router that checks in late.
2. **A late router was called offline.**
   * If the router happened to be late at that minute, the schedule refused straight away.
   * **Now** a router heard from in the last 15 minutes still gets the restart. The schedule notes that it runs at the
     next check-in.
3. **Direct API routers reported "did not answer" even when they restarted.**
   * A router going down drops the connection in many ways: *reset by peer*, *broken pipe*, a timeout. Anything except
     the word "closed" was reported as a failure, although the router was already restarting.
   * **Now** only a failed login is "did not answer". Once the restart command has gone through, a dropped connection
     counts as success.

**It also keeps trying.** If the router really does not answer, the schedule tries again every minute for 8 minutes
after its time or window, instead of giving up at the first attempt. Only then does it alert you: *"… TapTap tried
every minute until 03:38"*.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-link-rhythm.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Nothing needs changing on the routers.

## Tests

    python manage.py test core.tests.test_link_rhythm core.tests.test_router_schedules

The full suite gives the same results as before (the failures that already fail without Redis are unchanged).
