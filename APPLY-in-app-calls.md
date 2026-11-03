# TapTap update — in-app calls from the Wi-Fi pages (+ loudspeaker, + Member Plan bin)

Built on commit b8a2597 (1 Nov 2026). **Cumulative**: contains everything from
taptap-member-plan-bin-and-call.zip and taptap-call-button-v2.zip, so you can apply only this one.
**Two migrations: 0109 (Member Plan bin) and 0110 (who answers calls).**

    cd /opt/taptap
    unzip -o ~/taptap-in-app-calls.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Then: Portal Studio → **Calls from your Wi-Fi pages** → tick who answers → Save.
Add the **Call button (floating)** block to your login page (the eye in the block list turns it on/off).

## TURN relay — needed for calls before the customer logs in
Before login a phone can only reach the hotspot walled garden. TapTap already adds itself there; the voice
needs your TURN relay too. If you did not set it up with the earlier calls update, add to .env:

    CHAT_TURN_SECRET=<long random string>
    CHAT_TURN_URLS=turn:turn.taptapnetwork.com:3478?transport=udp,turn:turn.taptapnetwork.com:3478?transport=tcp
    TURN_EXTERNAL_IP=<server public IP>

    docker compose --profile turn up -d turn && docker compose up -d --force-recreate web

turn.taptapnetwork.com must be DNS-only (not Cloudflare-proxied); open 3478/udp+tcp and 49160-49359/udp.
When CHAT_TURN_URLS is set, the router page deploy now also adds the TURN host to the walled garden
("TapTap calls (TURN relay)"). Routers get it at the next page deploy (publishing a page triggers it).

## How it works
* Customer taps **Call us** → help card → **Call us in the app** (or "Start the call at once" mode skips the card).
  The card shows a green dot and "Online now" when someone picked has TapTap open.
* Hosted page (https): the call screen opens in an overlay. Router page (http://): browsers refuse the
  microphone on http pages, so it opens TapTap's https call page (/p/<slug>/call/) with a back link.
* The customer enters name + phone (remembered on the phone), then it rings. Staff see the green ring card
  "Customer on your Wi-Fi page" in TapTap and answer in the normal call window — same encrypted
  peer-to-peer audio, reconnect on a dropped line, and they can turn on their camera or share a TapTap tab to
  show the customer something. The customer sees who answered and a timer.
* Who rings: the people ticked in Portal Studio (owner + active team members only; nobody ticked = owner).
  "Ring only those with TapTap open" falls back to everyone ticked when nobody is online.
  Staff hear it only while a TapTap tab is open (or with browser notifications allowed).
* Every call leaves a note in the team chat: answered (length, who answered), declined, or
  "Missed Wi-Fi call from Awa · 3001122 — call them back".
* Limits: 5 calls per phone/IP per 10 minutes, 3 customers ringing one business at once, 40 s ring time.
* Calls off (no visible Call block) or nobody set up: the card falls back to a normal phone call.
* Some phones' built-in Wi-Fi sign-in sheet may not allow the microphone; the normal phone call stays offered.

## Loudspeaker (customer call screen and the TapTap call window on phones)
* The customer's **Speaker** button (was "Sound", which only re-played the audio) now switches the loudspeaker
  on and off, and shows it as pressed. Staff answering on a phone get a **Speaker** button in the call bar too.
* How: browsers that list audio outputs (Chrome/Edge) are switched to the output named speaker; otherwise the
  voice is played through the page's audio engine, which Android sends to the loudspeaker, with a little extra
  volume. Switching off goes back to the earpiece / default.
* iPhone Safari chooses the output itself and no web page can change it; the button then says so (volume
  buttons / Control Centre). Shared code: static/js/call-speaker.js.

## Member Plans → Bin (from the earlier zip)
Inactive Member Plans can be deleted to the Bin (members move to another active plan first, time kept).
See APPLY-member-plan-bin-and-call.md for details.

## Files
New:     core/portal_call.py, core/views_portal_call.py, core/models_portal_call.py,
         core/migrations/0110_portal_call_settings.py, templates/core/portal_call.html,
         static/js/portal-call.js, static/css/portal-call.css, static/js/call-speaker.js, core/tests/test_portal_call.py
         (+ member plan bin: core/member_plan_bin.py, core/migrations/0109_member_plan_bin.py, core/tests/test_member_plan_bin.py)
Changed: core/chat_call.py (guest callers), core/apps.py, core/urls.py, core/permissions.py, core/views_studio.py,
         core/portal_deploy.py (TURN in walled garden), static/js/chat.js + static/css/chat.css (portal ring card),
         static/js/chat-call.js + templates/core/chat_call.html (Speaker button), static/studio/portal-render.js, static/studio/portal-editor.js, templates/core/studio/portal_list.html,
         portal_editor.html, portal_public.html, templates/core/base.html + chat_standalone.html (asset versions)
         (+ member plan bin files)
Tests:   python manage.py test core.tests.test_portal_call core.tests.test_member_plan_bin
