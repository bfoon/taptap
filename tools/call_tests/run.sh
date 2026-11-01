#!/bin/bash
# Real-browser call tests: a fresh SQLite database, the Django dev server, then each test.
#   tools/call_tests/run.sh                 all of them
#   tools/call_tests/run.sh test_call.py    one
# Env: TT_CHROME=/path/to/chrome (default: Playwright's Chromium), PORT (8800), TT_WORK (/tmp/tt-call-tests)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
PORT="${PORT:-8800}"; export TT_BASE="http://127.0.0.1:$PORT" TT_WORK="${TT_WORK:-/tmp/tt-call-tests}"
mkdir -p "$TT_WORK"
export DB_ENGINE=sqlite CELERY_EAGER=1 SQLITE_PATH="$TT_WORK/db.sqlite3" DEBUG=1 CACHE_URL= ALLOWED_HOSTS=localhost,127.0.0.1   # eager Celery + no CACHE_URL: in-memory cache in the one server process
cd "$HERE" && python3 make_media.py >/dev/null || { echo "needs: pip install playwright numpy scikit-image"; exit 2; }
TESTS="${*:-test_effects.py test_shared_tab.py test_call.py test_control.py}"
RC=0
for T in $TESTS; do
  echo "=== $T"
  if curl -s -o /dev/null "$TT_BASE/"; then echo "port $PORT is already in use — stop that server or set PORT=…"; exit 2; fi
  rm -f "$SQLITE_PATH"; (cd "$REPO" && python3 manage.py migrate -v0) || exit 1
  export SETUP="$(cd "$REPO" && python3 "$HERE/setup_call.py" 2>/dev/null | tail -1)"
  (cd "$REPO" && exec python3 manage.py runserver "127.0.0.1:$PORT" --noreload >"$TT_WORK/server.log" 2>&1) & SRV=$!
  for i in $(seq 1 60); do curl -s -o /dev/null "$TT_BASE/static/js/chat.js" && break; sleep 0.3; done
  (cd "$HERE" && timeout 600 python3 "$T") || RC=1
  kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
done
exit $RC
