# TapTap update — the mobile app (+ in-app customer calls, loudspeaker, Member Plan bin)

Built on commit b8a2597 (1 Nov 2026). **Cumulative**: contains everything from the earlier zips
(member-plan bin, call button, in-app calls), so apply only this one.
**Migrations: 0109 (Member Plan bin), 0110 (who answers customer calls), 0111 (phones signed in to the app).**

## Server

    cd /opt/taptap
    unzip -o ~/taptap-mobile-app.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Optional, for ringing when the app is closed and for notifications (see mobile/README.md §2):

    FCM_SERVICE_ACCOUNT=/app/secrets/firebase-taptap.json     # in .env, then restart web + worker

Without it nothing is pushed and the app works while it is open.

## The app

    cd mobile
    flutter create --org gm.easysolutions --project-name taptap_app --platforms android,ios .
    python3 tool/configure_platforms.py
    flutter pub get
    flutter run

Full guide, Firebase steps and file map: mobile/README.md

## What changed on the server for the app
* Sign-in with a token (core/mobile_auth.py): each phone gets its own normal Django session behind the token, so
  every existing view and permission check runs unchanged. The session key never reaches the phone; signing out,
  a password change or removing the phone ends it. CSRF does not apply to token requests (no cookies); it still
  applies to the website. Email-code sign-in and the "new sign-in" security email work as on the web.
* /api/mobile/v1/ (core/views_mobile.py): auth (login · verify · resend · logout), me, business switch, push token,
  home (today at a glance), sales (Daily sales as data, with plan / seller detail and a cheap "unchanged" check),
  complaints (cases + fix · close · reopen), customer-calls, devices.
* Chat, calls and Fit: the app uses the website's own /chat/, /chat/call/ and /fit/ endpoints — Fit can do on the
  phone exactly what it can do for that person on the web.
* Push (core/mobile_push.py, optional): incoming call (also customer calls for the people picked to answer), stop
  ringing when answered / hung up, chat messages, new customer complaints, missed customer calls.
  No new Python packages.

## Tests
    python manage.py test core.tests.test_mobile_api core.tests.test_portal_call core.tests.test_member_plan_bin
Full suite: same results as before this update (the 34 failures + 2 errors that already fail in a test
environment without Redis are unchanged).

## Earlier parts (in this zip too)
APPLY-in-app-calls.md, APPLY-call-button-v2.md, APPLY-member-plan-bin-and-call.md
