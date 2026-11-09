# TapTap update — "Why people drop … did not answer within 6 minutes"

**No migration.** No new packages.

This builds on branch `voucher-unstick`. The zip has everything since master: the voucher fix, vouchers first, the
wardrobe (migration 0118), How-to and the Link rhythm and restart-schedule fix.

## Why it failed

There were two problems.

### 1. The test read the router's log in a way some RouterOS versions refuse

* The first version filtered the log on its `topics` list and read each line with `/log get`. Some RouterOS builds
  refuse one of those.
* The script then stopped **before** it sent its answer, so TapTap waited the full 6 minutes and then said "did not
  answer".
* **Now** the log is read the same way every TapTap sync reads a table (`/log print as-value where message~"logged
  out"`), and every step is guarded.
  * If the router will not let TapTap read the log, the test says exactly that.
  * The answer is sent once more if the first upload fails (a busy line at that second).

### 2. On a busy router, a test could not get on the router at all

* "Vouchers first" (the earlier update) filled each check-in with voucher changes. A test that a person is waiting for
  could not fit behind them, check-in after check-in.
* The same could happen to a big sync table on the other work's turn.
* **Now** vouchers still lead every check-in, but room is kept behind them for:
  * a Network tools test someone is waiting for — on every check-in;
  * the oldest other job (sync tables and so on) — on the other work's turn.

### Clearer message if it still fails

If a test still cannot finish, the message now says where it got stuck:

* *TapTap could not hand the test to the router* — the router did not check in, or was busy with other commands; or
* *The router took the test but did not answer* — it was very busy, or its answer could not reach TapTap.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-why-people-drop.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Nothing needs changing on the routers. Run **Why people drop** again after the update.

## Tests

    python manage.py test core.tests.test_link_rhythm core.tests.test_tools_shield_leave core.tests.test_voucher_priority
