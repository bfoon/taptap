# TapTap app (Flutter)

For the business owner and the team. Signs in with the same email and password as TapTap on the web.

| Tab | What it does |
|---|---|
| **Today** (first page) | Live daily sales: the day's total, pace against your usual day, hour by hour, last 7 days, plans, sellers, payment methods, latest sales. Swipe for other days. A new sale pops up with a buzz. Cards for open complaints, unread messages and missed customer calls (call back in one tap). |
| **Complaints** | Vouchers customers sent from the Wi-Fi page, one case per voucher, hottest first: what TapTap found, **Fix it**, close with a note, reopen, call / WhatsApp the customer, or let Fit sort them. |
| **Chat** | Team room, direct messages and TapTap support — photos, typing, who is online, and calls. |
| **Calls** | Voice (and camera) calls with the team — the same call system as the website, so phone ↔ web ↔ Wi-Fi-page customers all work. Speaker, mute, camera, flip, reconnect on a dropped line. If the owner picked you to answer **customer calls** (Portal Studio → Calls from your Wi-Fi pages), they ring this phone too. |
| **Fit** | Your AI — the very same Fit as on the web, so it can do exactly what it may do for you there (same tools, permissions, “Allowed” groups and confirmations). Voice input, charts, action cards with Run / Dismiss, “Brief me”. |
| **More** | Customer-call log, switch business, phones signed in (sign one out), sign out. |

What a person sees follows their TapTap role: a *View only* person gets Today, Chat and More; Complaints and Fit appear when the role and the subscription include them.

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
show the phone's own incoming-call screen even when TapTap is closed, and chat messages, new complaints and missed
customer calls arrive as notifications.

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

## 3. How it talks to TapTap

* Sign-in: `POST /api/mobile/v1/auth/login/` (+ `/auth/verify/` when the email code is on) → a token kept in the
  phone's secure storage. TapTap stores only its hash; signing out, a password change or "Sign out" on another phone
  ends it.
* App data: `/api/mobile/v1/` — `me`, `home`, `sales`, `complaints`, `customer-calls`, `devices`, `push`, `business`.
* Chat, calls and Fit: the website's own `/chat/…`, `/chat/call/…` and `/fit/…` endpoints, with the token.

## Files

```
lib/main.dart                 app start, theme from the business brand colour
lib/app/api.dart              HTTP + token
lib/app/state.dart            who is signed in, chat + ring polling
lib/screens/today_screen.dart daily sales (first page)
lib/screens/sales_detail_screen.dart   one plan / one seller
lib/screens/complaints_screen.dart
lib/screens/chat_screens.dart
lib/screens/fit_screen.dart
lib/screens/more_screen.dart
lib/screens/shell.dart        tabs, incoming calls, notification taps
lib/calls/call_engine.dart    WebRTC — same protocol as static/js/chat-call.js
lib/calls/call_screen.dart    in-call + incoming-call screens
lib/push/push.dart            Firebase + native call screen (flutter_callkit_incoming)
tool/configure_platforms.py   Android / iOS permissions after `flutter create`
```
