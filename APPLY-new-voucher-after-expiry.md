# Fix: a customer with a new voucher was thrown off right after the old one expired

## What people saw
A voucher's time ran out. The customer bought another one and signed in. The page then said
"you are logged in", but there was no internet, and no page to type a voucher came up again.

## Why it happened
When a voucher ends, TapTap frees the phones that used it. It removes their hotspot host and drops
them from MikroTik Wi-Fi so the login page appears. That clean-up does not run only once:
- it runs again on the next pass ("free its phones once more");
- it runs every few days while the expired voucher waits to be archived;
- over TapTap Link, it runs whenever the router next checks in, which can be minutes later.

By then the same phone was often already signed in with the **new** voucher. The clean-up removed
that phone's host, which ended the new session. The phone still showed the "connected" page, had no
internet, and did not look for the login page again.

## The fix
A phone is freed only when no hotspot session is using it. The expired voucher's own sessions and
cookies are removed first. So any session still on that MAC belongs to another voucher, usually the
one the customer just bought, and that phone is left alone.
- Direct API / Tunnel: `MikroTikService.release_devices` (core/mikrotik.py).
- TapTap Link: the RouterOS `$drop` function, `DROP_DEVICE` in core/agent.py. It is used by expiry,
  voucher unstick and hotspot recovery.

Phones that are not signed in are still freed exactly as before.

## Files
- core/agent.py (changed)
- core/mikrotik.py (changed)
- core/tests/test_expiry_new_voucher.py (new, 5 tests; 3 of them fail on the old code)

## Apply
    git apply taptap-new-voucher-fix.patch     # or copy the files from the zip
    python manage.py test core.tests.test_expiry_new_voucher
Restart the web and worker processes. Nothing needs to change on the routers: Link routers get the
new script with their next command. There are no migrations.

## Test results
The full suite ran 947 tests. The same 38 tests fail as on the untouched 4b72ab5 (timing and
environment tests), and there are no new failures.
