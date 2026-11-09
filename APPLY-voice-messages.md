# TapTap update: voice messages that delete themselves

**Migration 0119** (one new column). **Two new app packages:** `record` and `audioplayers`.

This builds on branch `voucher-unstick`. The zip has everything since master: the voucher fix, vouchers first, the
wardrobe (migration 0118), How-to, Link rhythm and the restart schedule, and Why people drop.

## What it does

Voice messages of **up to 30 seconds** can be sent in three places:

* **TapTap on the web:** press the 🎙 button in any chat.
* **The phone app:** the 🎙 button replaces Send while the message box is empty.
* **The customer Wi-Fi page:** the call page now offers **🎙 Leave a voice message · 30 s** under **Call**.
  * It is for when nobody is free, or when the customer would rather not wait.
  * The customer records, listens back, adds their name and number (the number is optional), and sends.
  * The message lands in the team room as **"🎙️ Voice message from Fatou · +220 777 1234 (Login)"**.
  * The people who answer calls get a push notification. Tapping it opens the team room.

### Listen once, then gone for good

1. When someone **other than the sender** presses ▶ for the first time, the message gets one more minute. Every
   open chat, on the web and in the app, shows **🔥 deleted in 59 s… 58 s…**. The sender sees **Played · deleted in
   42 s**.
2. In a team room, anyone else can still play it during that minute.
3. After the minute, the recording is **deleted from the database**: the row and the bytes, not just hidden. Every
   screen then shows **🎙️ Voice message · listened**.
4. The phone app never keeps a copy.
   * It downloads the recording only when you press ▶.
   * It plays it from a temporary file and deletes that file when it ends or when the minute is up.
   * The browser plays it from memory, and that memory copy is dropped as well.
5. A voice message nobody plays is deleted after 7 days, like every other chat file.

### Details that keep it safe

* **Playing it yourself does not start the minute.** The sender can replay their own message until someone else
  plays it.
* **Only a real play counts.** The bytes are only given to a player that asks for them (`?listen=1`).
  * An older app, or an old page still open in a browser, would try to show the message as a picture.
  * That request gets nothing, so the minute never starts by accident.
* **It goes on time, even if Celery is down.**
  * A new beat job sweeps every 15 seconds.
  * The chat poll also sweeps, at most once every 10 seconds across all servers.
  * Opening an expired recording deletes it on the spot.
* **The file type is checked from its first bytes.** Only WebM/Opus, Ogg/Opus and MP4/AAC are accepted.
* **Size and rate limits:**
  * Messages longer than 30 seconds are refused.
  * Each message is capped at 1.5 MB.
  * Each person can send 40 voice messages an hour.
  * Voice messages count towards the chat storage quota.
* **Limits on the Wi-Fi page:**
  * 3 voice messages per customer every 10 minutes, and 40 an hour per business.
  * It only works while the page has its Call button (the same switch as calls).
* **Voice messages cannot be edited.** They can be deleted like any message, and deleting one removes the recording
  too.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-voice-messages.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Celery beat picks up the new 15-second job (`taptap-chat-purge-voice`, on the `live` queue) when its container is
recreated above.

The web pages load the new chat script by themselves (`?v=voice1`).

Optional settings (`.env`):

| Setting | Default | Meaning |
|---|---|---|
| `CHAT_VOICE_SECONDS` | 30 | longest voice message |
| `CHAT_VOICE_MAX_KB` | 1500 | largest file |
| `CHAT_VOICE_PER_HOUR` | 40 | voice messages per person per hour |

## Phone app

Two packages were added to `mobile/pubspec.yaml`:

    record: ^6.1.1
    audioplayers: ^6.5.0

Then:

    cd mobile
    flutter pub get
    python3 tool/configure_platforms.py      # the microphone permission is already there for calls; safe to re-run
    flutter run

* **Android:** RECORD_AUDIO is already in the manifest. The `record` package needs Android 6 (minSdk 23) or newer.
  Current Flutter projects already target 24.
* **iPhone:** the microphone text is already in Info.plist.

**Older app versions** show a voice message as text. Pressing it never counts as listening (see above).

## Tests

    python manage.py test core.tests.test_chat_voice core.tests.test_chat_media_video core.tests.test_portal_call core.tests.test_chat_edit_reply

### Checked in a real browser (fake microphone)

1. Foon recorded 3 seconds in the team room, stopped, and sent it.
2. Foon replayed it: still *"Not played yet"*.
3. Ali (phone-size screen) pressed ▶. It played and showed *deleted in 59 s*. Foon's screen showed *Played · deleted
   in 50 s*.
4. 65 seconds later, both screens showed *Voice message · listened*. The server answered **410 Gone** for the file.
5. A customer on the Wi-Fi call page recorded a message, listened back, and sent it with a name and number. It
   appeared in Foon's team room with ▶.
