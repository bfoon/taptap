# Real-browser call tests

These drive real Chromium windows against a throwaway TapTap (SQLite, the Django dev server, in-memory cache):
two logged-in people, fake cameras, real WebRTC calls and real tab capture. They are not part of
`manage.py test` because they need a browser.

    pip install playwright numpy scikit-image && python -m playwright install chromium
    tools/call_tests/run.sh                    # all four (about 6 minutes)
    tools/call_tests/run.sh test_call.py       # just one

| Test | What it proves |
|------|----------------|
| `test_effects.py` | Background blur / scenes / colour, the room stays hidden while the model loads, masks land on the face, a drawing pinned to the face follows the head, name tag, everything off. |
| `test_shared_tab.py` | The sharer's TapTap tab: ghost cursor names the button, guardian holds risky clicks (a scripted or remote click can never approve), passwords stay private, the log never records what was typed, pause, Esc, receipts, marks stay on scrolled content. |
| `test_call.py` | A real call: the viewer's click ripples on the sharer's own tab at the same spot and the viewer gets "✓ On their screen"; laser; drawing and clear; the control request reaches the sharer with reason and minutes; the viewer's blur reaches the other side; the call survives 45 s of heavy effects; Mute stays reachable with the Studio open. |
| `test_control.py` | Remote control end to end: ask → allow → ghost cursor → click → guardian → pause → +5 min → the log → the audit note saved in the chat. |

Notes
* Headless Chromium does not expose Capture Handles, so `test_control.py` hands the real tab's handle to the call
  window with a test-only init script. Everything else is real.
* A fake camera clip (a public-domain NASA portrait on a checkerboard) is generated into `TT_WORK`
  (default `/tmp/tt-call-tests`) on first run.
* Settings: `TT_CHROME=/path/to/chrome` (default: Playwright's Chromium), `PORT` (8800).
* On a machine without a GPU the face/background models run slowly (a few fps); the tests allow for that.
