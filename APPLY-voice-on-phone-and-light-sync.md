# TapTap update: voice messages kept on the phone, and lighter router syncs that put vouchers first

**No migration.** No new Python or app packages. Built on master **2100b23**. The zip holds the **complete files**
that change.

## 1. Voice messages kept on the phone (app 1.7.0)

* **The first ▶ downloads it into the app's own storage** on the phone (private to the app), and it plays from
  there. TapTap is told once the sound really starts, and **TapTap deletes its server copy 1 minute later, as
  before.**
* **The phone keeps its copy.** It stays playable in the conversation ("📱 On this phone · deleted from TapTap"),
  without internet, after TapTap's copy is gone.
* **Listed:** More → **Voice messages on this phone**. It shows who sent each one, in which conversation, when, its
  length and size. Play from the list, swipe one away or press its bin, or **Delete all**.
* **Delete it like a message:** long-press it in the chat → **Delete from this phone**. That deletes only this
  phone's copy.
* A phone that **cannot play** a recording keeps nothing, and TapTap's copy is not used up (as before).
* **Signing out deletes them all**, like everything else the app keeps for an account.
* **The web is unchanged:** it plays from TapTap and the message is gone 1 minute after it is played.

**Wording:** the recorder, the web and the customer's Wi-Fi page used to say "deleted for everyone 1 minute after it
is played". That is no longer true once a phone keeps a copy. They now say *deleted from TapTap 1 minute after it is
played*, and that the phone app keeps the listener's copy.

## 2. Voucher time: corrected at 50 % and 75 %, not at every sync

TapTap keeps each voucher's time and **ends the voucher itself** at its end. The router's time limit is only a safety
net for a router that loses contact. Until now, every live pass (direct-API routers, every 15 s) and every inventory
(TapTap Link) compared each voucher in use with the router, and "corrected" it whenever they disagreed. A family
voucher used by one phone, for example, has more time on the router than it needs, and was re-written all day. Every
correction was one more write on a busy router.

Now, while a voucher is **in use**:

| The router… | What TapTap does |
|---|---|
| agrees | nothing |
| would let the customer stay **longer**, or has **no limit** | **nothing until the voucher passes 50 % of its time**, then corrects it once; again once at **75 %**. At **100 %** TapTap ends it, as always |
| would cut the customer **before** TapTap's end | corrects it **at once** (urgent), as before |

* **Before usage:** unused vouchers are kept right at once, as before.
* **Vouchers with no end** that have a limit on the router are fixed at once (the router would cut them).
* **Members:** each renewal starts fresh 50 % / 75 % checkpoints.
* **The same rule everywhere:** live passes, TapTap Link inventory, member alignment, the full-sync heal and Fit all
  use the one drift test (`core/router_time.py`).
* **Explicit fixes still run at once:** *Fix it*, *Send to the router first*, and extending a voucher's time.

## 3. Lighter syncs, vouchers first

### Direct API / TapTap Tunnel routers

* **The whole voucher table is no longer read every 15 seconds.** The *live check* still runs every 15 s, but most
  passes are now **light**: active sessions and IP bindings only. Everything urgent shows there:
  * a voucher's first login (it starts from its session);
  * a voucher whose time is up still online (switched off at once);
  * fair usage and device locks.
* Expired vouchers are switched off from TapTap's own copy of the router's table.
* **The full table is read at the router's own pace:**

  | Router has | Full read |
  |---|---|
  | up to 300 users | every pass (unchanged) |
  | up to 1 500 | every 1 min |
  | up to 5 000 | every 2 min |
  | more | every 3 min |

  It is read **half as often** when the last read took 3 s or more (the router is busy), a quarter as often at 8 s
  or more. Never more than 5 minutes apart.
* **It is read at once** when a code TapTap doesn't know logs in (a voucher made on the router, to import it), or
  when someone presses Sync.
* **The list of every connected device** is read only when the router has bypassed IP bindings, the only thing it
  was read for.
* **At most 25 time corrections per pass,** urgent ones first: never a burst of writes on the router.

### TapTap Link routers

* Urgent time corrections stay in the **voucher lane**, ahead of everything.
* **50 % / 75 % corrections travel in a new background lane** (`hotspot_users_time`): the same verified write, sent
  behind every voucher change and urgent command. A routine check never takes a new sale's place in a check-in.
* Together with the checkpoints, far fewer correction commands reach the router at all.

### You can see it

Each card on the **Routers** page has a new line, for example:

> 🪶 Voucher table read every **2 min** · 2 400 users · last read 1.1 s · voucher time checked at 50 % & 75 % · today
> **6** checkpoint fixes, **1** urgent · **38** left alone till their checkpoint

When a router answers slowly: *router slow to answer — TapTap reads less*.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-voice-on-phone-light-sync.zip -d /tmp && cp -r /tmp/taptap/. /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

No migration. Reload TapTap with **Ctrl+F5** (new `chat.js?v=vk1`, `portal-call.js?v=8`). Nothing changes on the
routers' scripts; TapTap Link routers pick up the new background command on their next check-in.

## Phone app (1.7.0)

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

The app code was checked by eye only: there is no Dart SDK in this workspace, and its download site is not reachable
from here. It uses no new package: the files live next to the app's database (`getDatabasesPath`, from `sqflite`,
which the app already uses). The GitHub build (`flutter analyze`, `flutter test`, APK) is the real test.

## Tests

    python manage.py test core.tests.test_sync_light

`test_sync_light` (22 tests):

* **Checkpoints:** more time or no limit waits until 50 %; corrected once at 50 % and once at 75 %; a late check
  goes straight to 75 %. Fixed at once: a router that would cut early, unused vouchers, a voucher with no end. A
  member renewal starts new checkpoints.
* **Direct API repairs:** only urgent and checkpoint corrections are written, urgent first, at most 25 per pass;
  nothing to do does not even open the user menu.
* **Link lanes:** urgent in the voucher lane, checkpoint in the background lane. A handled checkpoint is not corrected
  again. The background command is the same verified write, validated the same way, and is delivered after vouchers.
* **Light passes:** a small router is read every pass, a big one every 2 minutes, a slow one less often. The host
  list is read only with bypassed bindings. A real live pass with a fake router skips the table and the host list,
  yet still starts a logging-in voucher and switches off an expired one. An unknown code online forces one full
  read, once per code. The Routers page shows the line.

Full suite: 858 tests. The only failures are the 38 that already fail on master (they need a Redis cache this
workspace does not have).

## Files

New: `core/router_load.py`, `core/tests/test_sync_light.py`, `mobile/lib/data/voice_store.dart`,
`mobile/lib/screens/saved_voice_screen.dart`
Changed: `core/router_time.py`, `core/live.py`, `core/expiry.py`, `core/agent.py`, `core/member_time_stability.py`,
`core/sync_doctor.py`, `core/views.py`, `templates/core/routers.html`, `static/js/chat.js`, `static/js/portal-call.js`,
`templates/core/{base,chat_standalone,portal_call}.html`, `templates/core/platform/base.html`,
`mobile/lib/screens/{chat_voice,chat_screens,more_screen}.dart`, `mobile/lib/app/state.dart`, `mobile/pubspec.yaml`
