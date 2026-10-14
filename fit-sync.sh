#!/usr/bin/env bash
# fit-sync.sh — make the server match GitHub, then rebuild TapTap and check Fit.
#
#   cd /opt/taptap && bash fit-sync.sh
#
# What it does
#   1. Backs up every local change (changed files + a diff) to /opt/taptap-backups/<time>/
#   2. Makes the code exactly match GitHub (origin/<branch>) — .env, the database and
#      other untracked files are NOT touched
#   3. Checks that all Fit files are there and up to date
#   4. Makes sure .env has GEMINI_API_KEY and the Gemini main + Flash fallback models
#   5. Rebuilds and restarts web / worker / live, waits for the site, runs migrations check
#   6. Runs `manage.py fit_check`
# Safe to run more than once.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(pwd)}"
BACKUP_ROOT="${BACKUP_ROOT:-$(dirname "$REPO_DIR")/taptap-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_ROOT/$STAMP"

green() { printf '\033[32m✓ %s\033[0m\n' "$*"; }
yellow() { printf '\033[33m! %s\033[0m\n' "$*"; }
red() { printf '\033[31m✗ %s\033[0m\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

cd "$REPO_DIR"
[ -d .git ] || { red "$REPO_DIR is not a git checkout. Run this inside /opt/taptap."; exit 1; }
[ -f docker-compose.yml ] || { red "No docker-compose.yml in $REPO_DIR."; exit 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "HEAD" ] && BRANCH=master

# ───────────────────────── 1. fetch + back up local changes ─────────────────────────
step "1/6  Fetching GitHub ($BRANCH) and backing up local changes"
git fetch --quiet origin "$BRANCH"
TARGET="origin/$BRANCH"

CHANGED="$(git diff --name-only HEAD; git diff --name-only --cached)"
# Untracked files that GitHub also has (they block the update)
BLOCKING="$(git ls-files --others --exclude-standard | while read -r f; do
  git cat-file -e "$TARGET:$f" 2>/dev/null && echo "$f"; done || true)"
ALL="$(printf '%s\n%s\n' "$CHANGED" "$BLOCKING" | sed '/^$/d' | sort -u)"

if [ -n "$ALL" ]; then
  mkdir -p "$BACKUP"
  git diff HEAD > "$BACKUP/local-changes.diff" || true
  git rev-parse HEAD > "$BACKUP/was-at-commit.txt"
  while read -r f; do
    [ -e "$f" ] || continue
    mkdir -p "$BACKUP/files/$(dirname "$f")"
    cp -p "$f" "$BACKUP/files/$f"
  done <<< "$ALL"
  green "Backed up $(echo "$ALL" | wc -l) file(s) to $BACKUP"
  echo "$ALL" | sed 's/^/    /'
else
  green "No local changes to back up"
fi
# .env is never in git, but keep a copy anyway — it holds your keys.
if [ -f .env ]; then mkdir -p "$BACKUP"; cp -p .env "$BACKUP/.env"; fi

# ───────────────────────── 2. match GitHub ─────────────────────────
step "2/6  Making the code match GitHub"
while read -r f; do [ -n "$f" ] && rm -f -- "$f"; done <<< "$BLOCKING"
git reset --quiet --hard "$TARGET"
green "Now at $(git log -1 --format='%h %s (%cd)' --date=format:'%d %b %H:%M')"

# ───────────────────────── 3. check Fit files ─────────────────────────
step "3/6  Checking Fit files"
MISSING=0
for f in core/fit_ai.py core/fit_tools.py core/fit_actions.py core/views_fit.py core/models_fit.py \
         core/migrations/0099_fit_ai.py core/migrations/0100_fit_monitors.py core/fit_monitors.py templates/core/fit_monitors.html \
         core/management/commands/fit_check.py static/js/fit.js static/css/fit.css; do
  if [ -f "$f" ]; then :; else red "missing $f"; MISSING=1; fi
done
check() { if grep -q "$2" "$1" 2>/dev/null; then green "$3"; else red "$4"; MISSING=1; fi; }
check core/fit_ai.py      "GEMINI_FALLBACK_MODEL" "fit_ai.py: latest (Gemini, Flash fallback)"              "fit_ai.py is an old version (still Groq)"
check core/views_fit.py   "Why:"               "views_fit.py: latest (owners see why a request failed)"      "views_fit.py is an old version"
check core/management/commands/fit_check.py "full_request" "fit_check: latest (full-size test)"          "fit_check.py is an old version"
check templates/core/base.html "ttFit"         "Fit is on every page"                                         "base.html does not load Fit"
check core/permissions.py "fit_ask"            "Fit URLs are in the permission map"                           "permissions.py is missing the Fit URLs"
if [ "$MISSING" = 1 ]; then
  red "GitHub does not have the complete Fit code. Push the latest Fit files to GitHub, then run this again."
  red "Your previous files are safe in $BACKUP"
  exit 1
fi
[ -f taptap-fit-diagnostics.patch ] && yellow "taptap-fit-diagnostics.patch is still in the repo (harmless). Remove it with: git rm taptap-fit-diagnostics.patch && git commit -m 'remove patch file' && git push"

# ───────────────────────── 4. .env ─────────────────────────
step "4/6  Checking .env"
touch .env
if grep -qE '^GEMINI_API_KEY=.+' .env; then
  green "GEMINI_API_KEY is set"
  if grep -qE '^GEMINI_API_KEY=["'"'"' ]|^GEMINI_API_KEY=.*[ "'"'"']$' .env; then
    yellow "GEMINI_API_KEY has quotes or spaces around it — remove them: GEMINI_API_KEY=AIza..."
  fi
else
  red "GEMINI_API_KEY is missing in .env. Add a line:  GEMINI_API_KEY=AIza_your_key   then run this script again."
  exit 1
fi
# Main model and Flash fallback (gemini-2.5-* shut down on 16 Oct 2026 — move off them)
if grep -qE '^GEMINI_MODEL=gemini-2\.5-' .env; then
  sed -i 's/^GEMINI_MODEL=.*/GEMINI_MODEL=gemini-3.1-pro-preview/' .env; green "Changed GEMINI_MODEL to gemini-3.1-pro-preview (Gemini 2.5 is retired)"
elif ! grep -qE '^GEMINI_MODEL=' .env; then
  printf '\nGEMINI_MODEL=gemini-3.1-pro-preview\n' >> .env; green "Added GEMINI_MODEL=gemini-3.1-pro-preview"
else
  green "$(grep -E '^GEMINI_MODEL=' .env)"
fi
if grep -qE '^GEMINI_FALLBACK_MODEL=gemini-2\.5-' .env; then
  sed -i 's/^GEMINI_FALLBACK_MODEL=.*/GEMINI_FALLBACK_MODEL=gemini-3.5-flash/' .env; green "Changed GEMINI_FALLBACK_MODEL to gemini-3.5-flash (Gemini 2.5 is retired)"
elif ! grep -qE '^GEMINI_FALLBACK_MODEL=' .env; then
  printf 'GEMINI_FALLBACK_MODEL=gemini-3.5-flash\n' >> .env; green "Added GEMINI_FALLBACK_MODEL=gemini-3.5-flash"
else
  green "$(grep -E '^GEMINI_FALLBACK_MODEL=' .env)"
fi
if ! grep -qE '^GEMINI_LITE_MODEL=' .env; then
  printf 'GEMINI_LITE_MODEL=gemini-3.1-flash-lite\n' >> .env; green "Added GEMINI_LITE_MODEL=gemini-3.1-flash-lite (last fallback, most free quota)"
else
  green "$(grep -E '^GEMINI_LITE_MODEL=' .env)"
fi
if grep -qE '^GEMINI_MODEL=gemini-[0-9.]+-pro' .env; then
  yellow "GEMINI_MODEL is a Pro model. On Google's free plan Pro has no quota: set GEMINI_MODEL=gemini-3.5-flash (Fit skips Pro for 12 h after the first refusal anyway)."
fi
grep -qE '^GROQ_' .env && yellow "GROQ_* lines in .env are no longer used by Fit (safe to delete)."

# ───────────────────────── 5. rebuild + restart ─────────────────────────
step "5/6  Rebuilding and restarting TapTap"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
SERVICES="$($DC config --services | grep -xE 'web|worker|live' | tr '\n' ' ')"
# --force-recreate: containers re-read .env (a plain restart does not)
$DC up -d --build --force-recreate $SERVICES
printf 'Waiting for the web container'
for i in $(seq 1 60); do
  if $DC exec -T web python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${PORT:-8000}/',timeout=3)" >/dev/null 2>&1; then
    echo; green "Web is up"; break
  fi
  printf '.'; sleep 2
  [ "$i" = 60 ] && { echo; red "Web did not come up in 2 minutes. See: $DC logs --tail=80 web"; exit 1; }
done
if $DC exec -T web python manage.py migrate --check >/dev/null 2>&1; then
  green "Database migrations are applied (including 0099_fit_ai)"
else
  yellow "Applying migrations…"; $DC exec -T web python manage.py migrate --noinput | tail -3
fi

# ───────────────────────── 6. Fit check ─────────────────────────
step "6/6  Checking Fit's connection to Gemini"
$DC exec -T web python manage.py fit_check 2>&1 | grep -vE 'enabled|installed|aligned' || true

echo
green "Done. Open TapTap, click Fit (the robot above the chat button) and ask a question."
[ -d "$BACKUP" ] && echo "  Your previous files are in $BACKUP (restore one with: cp $BACKUP/files/<path> <path>)"
