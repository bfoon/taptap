# TapTap update 8 — calls: the sharer sees your clicks, guided control, video effects

Install update 7 first (this update is built on it). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-update-8.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

(or in a git checkout with update 7: `git apply taptap-update-8.patch`)

What changes for people
* Your click on a shared screen ripples on the sharer's own screen, with a receipt saying where it was seen;
  📍 floating pointer window for window / whole-screen shares; laser and drawing on their screen.
* Asking for control: take control or just guide, 5/15/30 min, with a reason. Ghost cursor, guardian for risky
  buttons, Pause, +5 min, a live log, and a fuller audit note in the chat.
* ✨ Effects: background blur / scenes / photo, face masks, drawing on your video (can stick to your face),
  name tag. All on the device.

Browsers load the new call code automatically (asset version call5). The effect models add about 17 MB of
static files (served by TapTap; downloaded by a browser only when someone turns an effect on).

Changed: README.md, core/chat_call.py, core/views_chat.py, static/css/chat-call.css, static/css/chat.css,
static/js/chat-call.js, static/js/chat.js, templates/core/base.html, templates/core/chat_call.html,
templates/core/chat_standalone.html, templates/core/platform/base.html.
New: static/js/call-fx.js, static/vendor/mediapipe/ (MediaPipe, Apache-2.0, see NOTICE.md),
core/tests/test_call_studio.py, tools/call_tests/ (real-browser tests).

Tests: `python manage.py test core` (252) · real browser: `tools/call_tests/run.sh` (4 suites, 73 checks).
