#!/usr/bin/env bash
# Verify headroom-ollama install is healthy
# Checks: venv, env file, all 3 services + 1 timer, /readyz, auth probe, journal

set -euo pipefail

HEADROOM_HOME="${HEADROOM_HOME:-$HOME/.headroom}"
HEADROOM_VENV="${HEADROOM_VENV:-$HOME/.local/venvs/headroom}"
PASS=0
FAIL=0

GRN='\033[0;32m'
RED='\033[0;31m'
YEL='\033[1;33m'
NC='\033[0m'

ok()   { printf "${GRN}✓${NC} %s\n" "$*"; PASS=$((PASS+1)); }
fail() { printf "${RED}✗${NC} %s\n" "$*"; FAIL=$((FAIL+1)); }
warn() { printf "${YEL}!${NC} %s\n" "$*"; }

echo "=== headroom-ollama verify ==="
echo

# 1. State dir
[[ -d "$HEADROOM_HOME" ]] && ok "state dir exists: $HEADROOM_HOME" || fail "missing state dir: $HEADROOM_HOME"

# 2. Env file (no real key check, just presence)
if [[ -f "$HEADROOM_HOME/headroom.env" ]]; then
    chmod 600 "$HEADROOM_HOME/headroom.env"
    if grep -q '__PEND\|your_o...here\|your-ollama-cloud-api-key' "$HEADROOM_HOME/headroom.env"; then
        fail "env file has placeholder key — edit $HEADROOM_HOME/headroom.env"
    else
        ok "env file present + key looks real"
    fi
else
    fail "missing env file: $HEADROOM_HOME/headroom.env"
fi

# 3. Venv
if [[ -x "$HEADROOM_VENV/bin/headroom" ]]; then
    # Check shebang points at this venv
    shebang="$(head -1 "$HEADROOM_VENV/bin/headroom")"
    if [[ "$shebang" == "#!$HEADROOM_VENV/bin/python"* ]]; then
        ok "venv installed + shebang OK"
    else
        warn "venv installed but shebang is wrong: $shebang"
        warn "  patch with: sed -i '1c\\#!/usr/bin/env python3' $HEADROOM_VENV/bin/headroom"
        FAIL=$((FAIL+1))
    fi
else
    fail "missing venv binary: $HEADROOM_VENV/bin/headroom"
fi

# 4. Services
for svc in headroom-proxy.service headroom-watchdog.service headroom-failsafe.service; do
    if systemctl --user is-active --quiet "$svc" 2>/dev/null; then
        ok "$svc is active"
    else
        state="$(systemctl --user is-active "$svc" 2>&1 || echo unknown)"
        fail "$svc is $state"
    fi
done

# 5. Watchdog timer
if systemctl --user is-enabled --quiet headroom-watchdog.timer 2>/dev/null; then
    next_fire="$(systemctl --user list-timers headroom-watchdog.timer --no-pager 2>/dev/null | awk '/headroom-watchdog.timer/ {print $1, $2}')"
    ok "watchdog timer enabled (next fire: ${next_fire:-unknown})"
else
    fail "watchdog timer not enabled"
fi

# 6. readyz
if curl -sf --max-time 5 http://127.0.0.1:8787/readyz >/dev/null 2>&1; then
    ok "/readyz reachable"
else
    fail "/readyz unreachable"
fi

# 7. Auth probe (real LLM call)
auth_response="$(curl -sf --max-time 20 -X POST http://127.0.0.1:8787/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"kimi-k2.6","messages":[{"role":"user","content":"."}],"max_tokens":1}' 2>&1 || echo "FAIL")"
if echo "$auth_response" | grep -q '"error"'; then
    fail "auth probe returned error envelope: $(echo "$auth_response" | head -c 200)"
elif echo "$auth_response" | grep -q '"choices"'; then
    ok "auth probe succeeded"
else
    fail "auth probe returned unexpected: $(echo "$auth_response" | head -c 200)"
fi

# 8. Routing
routing="$(hermes config show 2>/dev/null | grep -oE 'http[s]?://[^'\''",}]+' | head -1 || echo unknown)"
if [[ "$routing" == *"127.0.0.1:8787"* ]]; then
    ok "hermes routing → headroom proxy ($routing)"
elif [[ "$routing" == *"ollama.com"* ]]; then
    warn "hermes routing → direct Ollama ($routing) — failsafe may have flipped this"
else
    warn "hermes routing → unknown ($routing)"
fi

echo
echo "=== SUMMARY ==="
printf "  ${GRN}passed: %d${NC}\n" "$PASS"
if [[ $FAIL -gt 0 ]]; then
    printf "  ${RED}failed: %d${NC}\n" "$FAIL"
    echo
    echo "Diagnose: tail -50 $HEADROOM_HOME/watchdog.log $HEADROOM_HOME/failsafe.log /tmp/headroom-proxy.log"
    exit 1
else
    printf "  ${RED}failed: 0${NC}\n"
    echo
    echo "All green. Rollup lifetime savings:"
    headroom-rollup.py --since 30d 2>/dev/null || echo "(journal not populated yet)"
fi