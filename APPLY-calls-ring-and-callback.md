# TapTap update: customer calls ring the phones, call back on the Wi-Fi page, voice messages that are not lost

**Migration 0120** (one new table: the customers who called). No new Python or app packages.

Built on master (8735c3d).

## 1. Calls from the Wi-Fi page did not ring the phone app

Four causes, all fixed on the server (the old app benefits too):

| What was wrong | Now |
|---|---|
| **"Ring only the people who have TapTap open"** (on by default) counted a phone in a pocket as away. When one person had TapTap open on a computer, nobody's phone got the call. | A phone signed in to the app with call notifications on counts as reachable, and rings. Portal Studio shows per person: *📱 phone app rings* / *no phone app*. |
| **One push, sent once.** If it was late or lost (a sleeping phone), nothing rang. | The app answers "ringing here". Until TapTap hears that, the push is sent again 7, 15 and 25 seconds into the call. The customer's page says *their phone is ringing* once a phone confirms. |
| **A broken call left people "busy" for up to 4 hours.** A call that was answered but never connected (app closed, network lost) was never ended, so no customer call rang that person. | Such a call ends by itself after 75 seconds; a live call where both sides went silent ends after 90 seconds. |
| **One phone could end the call for everyone.** A phone whose microphone failed hung the whole call up. | That now counts as a decline for that phone only; the others keep ringing. |

Reminder: with nobody ticked in Portal Studio → *Calls from your Wi-Fi pages*, only the owner rings.

## 2. Who is calling: the customer's record and MAC address

The router's login page now hands the call page the phone's **MAC address, IP and the voucher it is signed in with**.
While the call rings and during the call, staff see (web ring card, web call window, app call screens):

* voucher code (a link on the web), plan, **time left** or its state, and the router;
* **MAC address** (marked *private* when the phone randomises it), IP, phone type and browser;
* **why they are probably calling**, e.g. *Voucher AVA8E: its time is up — add time first* or *The router refused
  AVA8E: "user has reached uptime limit"*.

The voucher is found from the code the phone is signed in with, or from the MAC address in its device slots.
Every phone that calls, leaves its number or a voice message gets one record
(Portal Studio → **Customers who called**; app → More → **Customers who called**).

The MAC, IP and code come from the customer's page. They are shown to staff and used for nothing else.

## 3. Call the customer back on the Wi-Fi page

* **Call back** (Portal Studio list, or the green phone in the app) rings the customer's **Wi-Fi page**:
  *"Kunda Net is calling you · Answer"*. Answering opens the call page; same encrypted call, reconnect included.
* The list shows **on the Wi-Fi page now** when the customer's page is open, so you know it will ring.
* It rings for 90 seconds. Missed? For 15 minutes their page shows *"We tried to call you back — Call us"*, and the
  team room gets a note.
* Only phones that called before ask TapTap for call-backs (for 24 hours), so idle login pages add no load.
* It cannot ring a phone whose Wi-Fi page is closed. The normal phone number stays next to it.

## 4. Voice messages

* **A recording is no longer destroyed by a failed play.** Its last minute used to start when the file was
  downloaded. It now starts when sound really plays (the player confirms). A browser or phone that cannot play it
  says so, and the message stays for another device.
* **Browsers record AAC/MP4 when they can** (played by every phone, iPhone included), WebM/Opus only as a fallback.
* **Safari:** the first press on ▶ now plays (it used to need a second press).
* Older app versions keep working as before.

Not solved: a WebM/Opus recording (Firefox, older Chrome) still cannot play on an iPhone. It is now kept instead
of burnt, and plays on the web or Android.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-calls-ring-and-callback.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

**Then publish / deploy your login and status pages again** (Portal Studio). Routers keep their own copy of the
page; until it is deployed again they do not send the MAC address and do not show the call-back card.

## Phone app (1.6.0)

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

The app code was checked for syntax with the Dart SDK only; it was not built or run on a phone here.

## Tests

    python manage.py test core.tests.test_calls_steady core.tests.test_portal_call core.tests.test_chat_voice core.tests.test_mobile_api

`test_calls_steady` (16 tests): a pocketed phone rings although a colleague is online · repeat pushes stop when the
phone confirms · answered-never-connected and both-sides-gone calls end · one failing phone does not end the call ·
download alone does not start the voice minute · staff see MAC, voucher, time left and the reason · one record per
phone · a call-back rings by MAC or page key, connects, and leaves "we tried to call you" when missed.

Not tested in a browser or on a router here: the call-back answer screen and the card on the router page.

## Files

New: core/migrations/0120_portal_caller.py, core/tests/test_calls_steady.py
Changed: core/chat_call.py, chat_media.py, mobile_push.py, models_portal_call.py, permissions.py, portal_call.py, urls.py,
views_chat.py, views_mobile.py, views_portal_call.py · static/js/chat.js, chat-call.js, portal-call.js,
static/studio/portal-render.js · templates (asset versions, chat_call.html, studio/portal_list.html) ·
mobile/lib: push/push.dart, app/state.dart, calls/call_engine.dart, calls/call_screen.dart, screens/chat_voice.dart,
screens/more_screen.dart, pubspec.yaml
