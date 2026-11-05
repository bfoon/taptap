# TapTap app (Flutter)

For the business owner and the team. Signs in with the same email and password as TapTap on the web.

| Tab | What it does |
|---|---|
| **Today** (first page) | Live daily sales: the day's total, pace against your usual day, hour by hour, last 7 days, plans, sellers, payment methods, latest sales. Swipe for other days. A new sale pops up with a buzz. Cards for open complaints, unread messages and missed customer calls (call back in one tap). |
| **Vouchers** | Every voucher, like the website's Vouchers page: tabs (In stock · Sold · Used · Frozen · Disabled / expired), search by code, serial, customer or batch (`C*` wildcards work), plan filter. Open one to see its time left, devices, sale, router and full history, send the code by **WhatsApp / SMS**, and — as your role allows — **add time, enable, disable, reset devices** or **delete** it (to the bin, with a reason). **Batches** shows how many are left in each. **New** makes a batch (plan, how many, router, agent on credit or paid upfront, code format) or one voucher for a customer (optional own code, paid now). |
| **Complaints** | Vouchers customers sent from the Wi-Fi page, one case per voucher, hottest first: what TapTap found, **Fix it**, close with a note, reopen, call / WhatsApp the customer, or let Fit sort them. |
| **Chat** | Team room, direct messages and TapTap support — photos, typing, who is online, and calls. |
| **Calls** | Voice (and camera) calls with the team — the same call system as the website, so phone ↔ web ↔ Wi-Fi-page customers all work. Speaker, mute, camera, flip, reconnect on a dropped line. If the owner picked you to answer **customer calls** (Portal Studio → Calls from your Wi-Fi pages), they ring this phone too. |
| **Fit** | Your AI — the very same Fit as on the web, so it can do exactly what it may do for you there (same tools, permissions, “Allowed” groups and confirmations). Voice input, charts, action cards with Run / Dismiss, “Brief me”. |
| **More** | Customer-call log, **Notifications** (what pops up on this phone, a test, what is still missing), switch business, phones signed in (sign one out), sign out. |

What a person sees follows their TapTap role: a *View only* person gets Today, Chat and More; Vouchers, Complaints and Fit appear when the role and the subscription include them. Inside Vouchers the buttons follow the same four rights as the website: *See vouchers* (list, details), *Generate* (New), *Voucher support* (add time, enable, disable, reset devices) and *Manage vouchers* (delete).

---

## 1. Build it

Needs Flutter 3.24 or newer (`flutter --version`).

```bash
cd mobile
flutter create --org gm.easysolutions --project-name taptap_app --platforms android,ios .
python3 tool/configure_platforms.py      # microphone, camera, notifications, background calls (safe to re-run)
flutter pub get
flutter run                              # a real phone: calls need a microphone
```

`flutter create .` only adds the missing android/ and ios/ folders — it does not touch `lib/` or `pubspec.yaml`.

On first start, tap **Using your own TapTap server?** if your TapTap is not at `https://taptapnetwork.com`.

Release builds: `flutter build apk --release` (or `appbundle`) and `flutter build ipa`.

### Build on GitHub (no Flutter on your computer)

The workflow `.github/workflows/mobile-app.yml` builds the app on every push that touches `mobile/`:

1. Push the repo (with `.github/workflows/mobile-app.yml` and `mobile/`) to GitHub.
2. Open **Actions → TapTap mobile app**; when the run is green, download **TapTap-android-N** from *Artifacts* —
   it holds `TapTap-N.apk`, ready to install on Android (allow "install unknown apps" once).
3. iPhone: **Run workflow** → tick *Also build iPhone* for an unsigned build (a compile check; sign it in Xcode).

Optional repository secrets (Settings → Secrets and variables → Actions):

| Secret | What it does |
|---|---|
| `GOOGLE_SERVICES_JSON` | Paste the whole `google-services.json` from Firebase → push + ringing when the app is closed |
| `TAPTAP_SERVER` | Your TapTap address (e.g. `https://taptapnetwork.com`) — the address the app starts with |

The release APK is signed with the build's debug key: fine for installing on your team's phones. For the Play Store,
add your own signing key (Flutter's *Build and release an Android app* guide).

If a run fails, open the failed step and paste the red lines — the *Analyze* step lists every problem in the code
even when the build continues.

## 2. Ringing when the app is closed (optional — Firebase)

Without this the app works fully while it is open (it checks every few seconds, like the website). With it, calls
show the phone's own incoming-call screen even when TapTap is closed, and these arrive as notifications that **pop up
on the screen**:

| Kind | Who gets it | Android channel |
|---|---|---|
| Chat messages | everyone in the conversation (not the sender, not if muted) | Chat messages |
| Customer complaints | people who can see customer reports | Customer complaints |
| Missed customer calls | the people picked to answer calls from the Wi-Fi pages | Missed customer calls |
| Business alerts | every bell alert that asks for a pop-up — your rules on the website's Alerts page (*Stock below 20 vouchers*, *Router offline*, agents…) to the people whose role covers it, and **Track** alerts to the person tracking | Business alerts |
| Vouchers made by the team | the owner and people who manage vouchers, when someone else makes vouchers (website or app) | Vouchers made by the team |

Each phone chooses its own kinds under **More → Notifications**, which also shows what is still missing (the phone's
permission, this app build's Firebase file, the server's key) and sends a test. While TapTap is open, notifications
pop up inside the app instead (tap to open, swipe up to dismiss). Quiet hours on an alert rule keep the phone quiet too.

**Why they pop up:** Android only shows a notification on top of the screen when its channel has *high* importance.
`tool/configure_platforms.py` writes the five channels above into `MainActivity.kt` at high importance (and makes
*Business alerts* the default for anything else). People can still tune each one in the phone's settings —
More → Notifications → *Pop-up, sound and lock screen* opens them.

1. Create a Firebase project (free) at console.firebase.google.com and add an Android app with package
   `gm.easysolutions.taptap_app` and an iOS app with the same bundle id.
2. Easiest: `dart pub global activate flutterfire_cli` then `flutterfire configure` in `mobile/` — it downloads
   `google-services.json` / `GoogleService-Info.plist` and wires the Gradle plugin. (The app calls
   `Firebase.initializeApp()` and reads those files; the generated `firebase_options.dart` is not needed.)
3. **iPhone:** in Xcode → Runner → Signing & Capabilities add *Push Notifications* and *Background Modes* (Voice over IP,
   Remote notifications, Audio). Upload your APNs key in Firebase → Project settings → Cloud Messaging.
4. **Server:** Firebase → Project settings → Service accounts → *Generate new private key*. Put the file on the
   server and add to TapTap's `.env`:
   ```
   FCM_SERVICE_ACCOUNT=/app/secrets/firebase-taptap.json
   ```
   (or paste the JSON itself as the value), then restart `web` and `worker`.

Android 14+: the first time, allow *full-screen notifications* for TapTap when asked, so calls can show over the lock
screen.

**Battery saver (important on Tecno, Infinix, itel, Xiaomi, Oppo, Samsung…):** these phones stop apps in the background,
and then no call rings and no notification arrives until TapTap is opened. After sign-in the app asks once to *run in
the background*; More → Notifications shows it as a check with an **Allow** button. On some phones also turn on
*Auto-start* for TapTap in the phone's settings.

### Sounds

* Incoming call while the app is open: the phone's own ringtone (looped) and a buzz, until you answer or decline.
* Calling someone: a soft ringback tone (`assets/sounds/ringback.wav`) until they answer.
* A notification while the app is open: the in-app pop-up plays the phone's notification sound; chat messages do too
  when push is not set up.
* When the app is closed the phone's own call screen and the notification channels make the sound.

### Slow internet

Calls are built to survive a weak line: every signalling step (accept, offer, answer, hang-up) is tried again instead of
ending the call, the app waits for the TURN relay path, and a first connection that fails is retried with fresh paths
for up to ~55 s before it gives up (the website and the Wi-Fi page call do the same). For calls that must work on any
network — mobile data ↔ hotspot Wi-Fi especially — run the TURN relay (`docker compose --profile turn up -d turn`, see
`docker-compose.yml`) and set `CHAT_TURN_URLS` / `CHAT_TURN_SECRET`.

## 3. How it talks to TapTap

* Sign-in: `POST /api/mobile/v1/auth/login/` (+ `/auth/verify/` when the email code is on) → a token kept in the
  phone's secure storage. TapTap stores only its hash; signing out, a password change or "Sign out" on another phone
  ends it.
* App data: `/api/mobile/v1/` — `me`, `home`, `sales`, `complaints`, `customer-calls`, `devices`, `push`, `business`,
  `notifications` (this phone's kinds + test).
* Vouchers: `/api/mobile/v1/vouchers/` (list), `vouchers/<id>/` (detail; POST disable · enable · add_time · reset_devices ·
  delete), `vouchers/new/` (form data; POST a batch or a single voucher), `batches/` — server: `core/views_mobile_vouchers.py`.
  A batch is made by `core/voucher_make.py`, the same code as the website's Generate page.
* Routers: `/api/mobile/v1/routers/` (each MikroTik on its own card), `routers/<id>/` (who is online, its own activity;
  POST sync · test · kick · reboot), `routers/<id>/move/` (preview; POST moves it to another business) — server:
  `core/views_mobile_routers.py`, moving: `core/router_move.py`.
* Quick sell: `vouchers/new/` with `kind=single`, `paid=true` — one paid voucher on one router, the code shown large
  with WhatsApp / SMS.
* Chat, calls and Fit: the website's own `/chat/…`, `/chat/call/…` and `/fit/…` endpoints, with the token.

## Files

```
lib/main.dart                 app start, theme from the business brand colour
lib/app/api.dart              HTTP + token
lib/app/state.dart            who is signed in, chat + ring polling
lib/screens/today_screen.dart daily sales (first page)
lib/screens/sales_detail_screen.dart   one plan / one seller
lib/screens/complaints_screen.dart
lib/screens/vouchers_screen.dart      vouchers + batches lists
lib/screens/voucher_detail_screen.dart one voucher: time, devices, sale, history, actions, WhatsApp / SMS
lib/screens/new_vouchers_screen.dart  make a batch or one voucher; the codes afterwards
lib/screens/notifications_screen.dart what this phone gets, what is missing, test
lib/screens/routers_screen.dart       Routers: All + one tab per MikroTik; people online, kick, sync, test, reboot
lib/screens/router_move_sheet.dart    move a router to another business (preview, confirm by name)
lib/screens/quick_sell.dart           Quick sell: plan → Sell → big code, WhatsApp / SMS, next customer
lib/widgets/quick_actions.dart        Quick sell · New batch · Routers on the Today page
lib/widgets/voucher_widgets.dart      voucher row, batch card, state colours
lib/widgets/push_banner.dart          the pop-up while the app is open
lib/screens/chat_screens.dart
lib/screens/fit_screen.dart
lib/screens/more_screen.dart
lib/screens/shell.dart        tabs, incoming calls, notification taps
lib/calls/call_engine.dart    WebRTC — same protocol as static/js/chat-call.js
lib/calls/call_screen.dart    in-call + incoming-call screens
lib/push/push.dart            Firebase + native call screen (flutter_callkit_incoming), battery-saver check
lib/app/sounds.dart           ringtone, ringback and notification sounds while the app is open
tool/configure_platforms.py   Android / iOS permissions and Android notification channels after `flutter create`
```
