# TapTap update: customer calls that ring (not just "missed"), and calling customers back on their Wi-Fi page

**Migration `0126_chat_message_meta`** (one new column on chat messages). No new Python packages.
Built on master **fbd0692**. The zip holds the **complete files** that change; everything else is as on master.

## 1. Why customer calls only showed "Missed call"

I ran real calls in two browsers here (staff on TapTap, a customer on the Wi-Fi call page, with a fake microphone).
When the staff TapTap tab is in front, the call rings within 3 seconds and connects. The calls got missed
because of **where TapTap usually is: in a background tab.**

| What happened | Now |
|---|---|
| TapTap checked for ringing calls with the page's own timer. **Browsers slow a background tab's timers**, and Chrome slows them to about **once a minute** after a few minutes. A call rang 40 s, so it often came and went between two checks. All that was left was "📶 Missed Wi-Fi call". | The check runs in a **background worker** (`static/js/ring-watch.js`). Browsers don't slow those, so it still checks every 3 seconds from a background tab. If the worker cannot start, the old check takes over. |
| A ring in a background tab was **silent** (browsers only allow sound after the page has been touched) and easy to miss. | Sound is unlocked on the first click or key press in TapTap. While a call rings in the background, the **tab title blinks** "📞 Customer calling · Awa…", the tone repeats, and the **desktop alert** shows the customer's voucher and why they are probably calling. The ring card and missed-call notes offer **"🔔 Ring me in the background"** to switch desktop alerts on in one click. |
| **"Online first"** (on by default) rang only the people with TapTap open. A TapTap tab left open in the background counts as open, even when nobody is at that computer. | After **15 s** with nobody picking up, the call **also rings everyone else picked** in Portal Studio → *Calls from your Wi-Fi pages*. |
| A customer call rang **40 s**. | It rings **60 s**: web, and the phone app too (its push and native call screen now last as long as the ring). Chat calls between staff keep 40 s. |

## 2. A missed call says why, and offers the call-back

The note in the team room now says how long it rang and how it reached each person:

> 📶 Missed Wi-Fi call from Awa · 3001122 — call them back (Login)
> Rang 60 s: Baboucarr (TapTap rang), Fatou (phone rang), Musa (TapTap not open)

or *"They hung up after 6 s: …"* when the customer gave up early. Each person shows one of: **TapTap rang** (their
TapTap showed it), **phone rang** (the app confirmed), **declined**, **phone did not confirm** (the push never
arrived) or **TapTap not open**.

Under every customer-call note (missed calls, "call me back" requests, voice messages, call-backs) there are
buttons:

* **📞 Call back on their Wi-Fi page.** It shows **"on it now"** when their page is open at that moment.
* **☎ their number** (a normal phone call) and **WhatsApp**.

## 3. Calling customers back on their Wi-Fi portal

You can now call back from:

* the **missed-call note** in chat (one tap, above);
* the **voucher page** and the **member page**: a new **Call this customer** card appears when that voucher or member's
  phone has called before. It is found by the code it signed in with, or by a device slot's MAC address. The card
  shows *on the Wi-Fi page now*, their number and WhatsApp;
* Portal Studio → *Customers who called*, and the app's green phone, as before.

When you call back:

* The call window says straight away whether their page is open ("ringing their Wi-Fi page — it is open right now"),
  or not ("it rings if they open it in the next 90 s, then asks them to call you back").
* **Missed call-back?** For **2 hours** (was 15 minutes) their call page says *"Fatou from Kunda Net tried to call you
  back. Tap Call — it rings Fatou first."*
* **Their return call rings the person who tried first**, even if that person is not picked for Wi-Fi calls. The ring
  says **"↩ Returning Fatou's call-back"**. Once you are connected, the "we tried" message goes away.

Checked end to end in two browsers here. The customer called and nobody answered: the ring lasted 60 s and the note
appeared with its buttons. Staff pressed *Call back on their Wi-Fi page*, the customer's page rang, and they talked
1 second after the customer pressed Answer. The note then read "Baboucarr called Awa back on the Wi-Fi page · 0:03".

## Server

    cd /opt/taptap
    unzip -o ~/taptap-customer-calls-ring-back.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core          # 0126_chat_message_meta
    docker compose up -d --force-recreate web worker vouchers live

Check: `docker compose exec web python manage.py showmigrations core | tail -2` shows `0126_chat_message_meta [X]`.

Then reload TapTap with **Ctrl+F5** (new `chat.js` / `chat.css?v=calls1`, `chat-call.js?v=calls1`,
`portal-call.js?v=7`, and the new `ring-watch.js`). The router login pages do **not** need redeploying: their script
did not change.

**Tip for the people who answer:** click anywhere in TapTap once after opening it (that unlocks the ring sound), and
press **🔔 Ring me in the background** the first time it appears.

## Phone app (1.6.1)

The app now rings as long as the call does (`duration` from the push, 60 s for customer calls). Older apps keep
working: they ignore the new field and ring 40 s.

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

The app change is three lines, checked by eye only: there was no Dart SDK here, and its download site is not
reachable from this workspace. I checked the call-screen plugin's source: `CallKitParams.duration` is an `int`.

## Tests

    python manage.py test core.tests.test_calls_ring_back core.tests.test_calls_steady core.tests.test_portal_call core.tests.test_mobile_push

`test_calls_ring_back` (22 tests): a customer call rings 60 s, a chat call 40 s · the push lives as long ·
it widens after 15 s, once, and only to people picked · a widened call answers and clears like any other · the
missed note says who it rang and how, and "hung up after N s" · the note carries the customer for the
buttons, with "on it now" · call-back says whether their page is open · a return call rings the person who tried
first, labelled · a connected return call clears "we tried" · old "tried" entries still read · voucher and member
pages offer the call (by code and by MAC address).

One existing test (`test_calls_steady` repeat pushes) was adjusted. After 15 s the call now widens to the owner, who
gets a first push of their own, so the test now checks that the phone which confirmed gets no more pushes.

Full suite: 836 tests. The only failures are the 38 that already fail on master.

## Files

New: `core/migrations/0126_chat_message_meta.py`, `core/tests/test_calls_ring_back.py`, `static/js/ring-watch.js`,
`templates/core/partials/wifi_callers.html`
Changed: `core/chat.py`, `core/chat_call.py`, `core/mobile_push.py`, `core/models_chat.py`, `core/portal_call.py`,
`core/views.py`, `core/views_chat.py`, `core/views_member_detail.py`, `core/tests/test_calls_steady.py`,
`static/js/chat.js`, `static/js/chat-call.js`, `static/js/portal-call.js`, `static/css/chat.css`,
`templates/core/{base,chat_call,chat_standalone,member_detail,portal_call,voucher_detail}.html`,
`templates/core/platform/base.html`, `mobile/lib/push/push.dart`, `mobile/lib/calls/call_screen.dart`,
`mobile/pubspec.yaml`
