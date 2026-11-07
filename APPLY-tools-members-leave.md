# TapTap update — Network tools on busy routers, members stay connected, leave a business

Built on commit 71f2fb7 (master). **Migration: 0117** (one new test type in Network tools; no table changes).
No new Python or app packages.

## 1. "Router offline" when it is not — Network tools could not reach a TapTap Link router

**Cause:** TapTap had two rules for "is this Link router online?":
* The router card used "no check-in for 90 s" (or 6 check-ins).
* The buttons that send commands (Network tools, member profile repairs, schedules…) used 45 s (or 4 check-ins).

A busy router checks in late. Yours has thousands of users, and a long test (a speed test runs 40 s) also delays the
next check-in. So the router showed **Online** while Network tools said it had *not checked in since …* and refused to
run.

**Fix:**
* **One rule everywhere** (`RouterAgent.grace_seconds`): 90 s, or 6 check-ins. The router card, offline alerts and
  every button now agree.
* **Network tools never refuse a router that is only late.** If it checked in within the last 15 minutes, the test
  is sent anyway. The page shows *"last checked in 140 s ago (busy or a slow line) — the test runs at its next
  check-in"*. If no check-in comes within 6 minutes, the test says so plainly.

## 2. Members disconnecting a lot

**New tool — Network tools → "Why people drop".** It reads the router's own log of the last 60 hotspot sign-outs and
the reason RouterOS gave for each. It groups them, gives the fix for each reason, and lists who dropped most, with
members marked:

| Reason in the router's log | What it means / what to change |
|---|---|
| keepalive timeout | The phone stopped answering (asleep, out of range). Check the Wi-Fi signal; raise "Keep a dropped Wi-Fi session for" in Settings → Voucher behaviour. |
| lost dhcp lease | The DHCP lease time is too short. Set the hotspot network's lease time to 1 h or more. |
| idle / session timeout | A profile still has a timeout. Run a full sync to repair it. |
| admin | A session was removed by hand or by TapTap. TapTap's automatic ones are now stopped for members (below). |
| user request / uptime / traffic limit | The customer signed out, or their time or data ran out. Expected. |

**Member shield** (`core/member_shield.py`) enforces your rule *"members whose time is not finished must never be
disconnected by TapTap"* in one place, for every TapTap Link command. When an automatic check (sharing, an old MAC
address being released, session checks, monitors…) tries to sign out or kick a member who still has time:
* the command is stopped and kept as *Cancelled — Member shield* on the TapTap Link page
* a line goes to the Activity log
* the member's page shows *"Member shield kept X online N times this week"*

A member is still disconnected when a **person** does it (the Disconnect button, a password change), when their
**time is over**, or when they are **disabled / frozen / deleted** in TapTap.

## 3. Leave a business you were added to

* **Website:** click your avatar (top right). Each business where you are team, not owner, has **Leave <business>…**.
  That opens a page explaining what happens; type the business name to confirm.
* **App:** More → **Leave <business>**, then type the name to confirm.
* **What happens:** your membership is **switched off, not deleted**, so your sales, pay records and messages stay
  with the business. Your login and your other businesses stay. You move to your next business; if it was your only
  one, you are signed out.
* **The owner is told:** a note in the team room, a bell alert (and a push to their phone) and the Activity log. They
  can switch you back on from the Team page. An owner cannot leave their own business.
* **Also fixed:** the avatar menu showed team members as **Owner** of the business they were working in. It now shows
  their real role (e.g. View only).

## Server

    cd /opt/taptap
    unzip -o ~/taptap-tools-members-leave.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

## The app

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

## Tests

    python manage.py test core.tests.test_tools_shield_leave core.tests.test_router_schedules

The full suite gives the same results as before (the failures that already fail in a test environment without Redis
are unchanged).
