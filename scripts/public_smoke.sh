#!/usr/bin/env bash
#
# Read-only public smoke test for a DEPLOYED Mini App (run after manual
# deploy, against the public base URL). It only performs GET requests and
# never writes: it verifies the entry redirect, the app shell, and the
# required static assets all respond successfully.
#
#   BASE_URL=https://your-host ./scripts/public_smoke.sh
#
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
BASE_URL="${BASE_URL%/}"
fail=0

status_of() {
  # -o /dev/null: body discarded; -w: status code only; -s: silent.
  curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$1"
}

check() {
  local label="$1" url="$2" want="$3"
  local got
  got="$(status_of "$url")"
  if [[ "$got" == "$want" ]]; then
    printf '  ok   %-28s %s -> %s\n' "$label" "$url" "$got"
  else
    printf '  FAIL %-28s %s -> %s (wanted %s)\n' "$label" "$url" "$got" "$want"
    fail=1
  fi
}

echo "Public smoke test: $BASE_URL"

# 1. Root redirects to the Mini App entry point (307 keep-method, or 302).
code="$(status_of "$BASE_URL/")"
case "$code" in
  307 | 302)
    printf '  ok   %-28s / -> %s (redirect)\n' "root redirect" "$code"
    ;;
  *)
    printf '  FAIL %-28s / -> %s (wanted 307/302)\n' "root redirect" "$code"
    fail=1
    ;;
esac

# 2. Mini App entry point + required assets.
check "app shell" "$BASE_URL/miniapp" "200"
check "styles.css" "$BASE_URL/miniapp/styles.css" "200"
check "app.js" "$BASE_URL/miniapp/app.js" "200"
check "js/api.js" "$BASE_URL/miniapp/js/api.js" "200"
check "js/ui.js" "$BASE_URL/miniapp/js/ui.js" "200"
check "js/state.js" "$BASE_URL/miniapp/js/state.js" "200"
check "js/telegram.js" "$BASE_URL/miniapp/js/telegram.js" "200"
check "flatpickr.js" "$BASE_URL/miniapp/vendor/flatpickr.min.js" "200"
check "flatpickr.css" "$BASE_URL/miniapp/vendor/flatpickr.min.css" "200"
check "flatpickr-ru.js" "$BASE_URL/miniapp/vendor/flatpickr-russian.js" "200"

# 3. Health endpoint (read-only).
check "health" "$BASE_URL/healthz" "200"

# 4. Optional non-authenticated page load: fetch the app shell HTML and make
#    sure it references the entry modules (cheap sanity, no login needed).
html="$(curl -s --max-time 15 "$BASE_URL/miniapp")"
if printf '%s' "$html" | grep -q 'app.js' && printf '%s' "$html" | grep -q 'styles.css'; then
  printf '  ok   %-28s shell references app.js + styles.css\n' "page load"
else
  printf '  FAIL %-28s shell missing app.js/styles.css reference\n' "page load"
  fail=1
fi

if [[ "$fail" -eq 0 ]]; then
  echo "PASS"
else
  echo "FAIL"
  exit 1
fi
