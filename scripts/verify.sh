#!/usr/bin/env bash
# Verify headroom-ollama install is healthy.
#
# Tests (all degrade gracefully when systemd or Hermes is unavailable):
#   1. state dir exists
#   2. env file present with mode 0600 and non-placeholder key
#   3. venv has headroom binary + correct shebang
#   4. systemd --user services active (proxy/watchdog/failsafe/timer)
#   5. proxy /readyz reachable
#   6. proxy can complete a real LLM call (auth probe)
#   7. hermes routing (if hermes is installed)
#   8. compression journal + savings summary
#
# Exit codes: 0 = all green, 1 = at least one failure, 2 = no-systemd warning
# Configurable via env vars below.

set -u

HEADROOM_HOME="${HEADROOM_HOME:-$HOME/.headroom}"
HEADROOM_VENV="${HEADROOM_VENV:-$HOME/.local/venvs/headroom}"
PROXY_URL="${PROXY_URL:-http://127.0.0.1:8787}"
PROBE_MODEL="${PROBE_MODEL:-kimi-k2.6}"

PASS=0; FAIL=0; WARN=0
GRN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[1;33m'; NC=$'\033[0m'

ok()   { printf "%s✓%s %s\n" "$GRN" "$NC" "$*"; PASS=$((PASS+1)); }
fail() { printf "%s✗%s %s\n" "$RED" "$NC" "$*"; FAIL=$((FAIL+1)); }
warn() { printf "%s!%s %s\n" "$YEL" "$NC" "$*"; WARN=$((WARN+1)); }

have_systemctl() { command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; }
have_hermes()     { command -v hermes >/dev/null 2>&1; }

echo "=== headroom-ollama verify ==="
echo "(state dir: $HEADROOM_HOME  venv: $HEADROOM_VENV  proxy: $PROXY_URL)"
echo

# 1. State dir
if [[ -d "$HEADROOM_HOME" ]]; then ok "state dir exists"; else fail "missing state dir: $HEADROOM_HOME"; fi

# 2. Env file
ENV_FILE="$HEADROOM_HOME/headroom.env"
if [[ -f "$ENV_FILE" ]]; then
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null)"
    if [[ "$mode" != "600" && "$mode" != "400" ]]; then
        fail "env file mode is $mode, expected 600 or 400"
    else
        ok "env file present, mode $mode"
    fi
    if grep -Eq '__PEND|your_o...here|your-ollama-cloud-api-key|PLACEHOLDER' "$ENV_FILE"; then
        fail "env file contains placeholder key — edit $ENV_FILE and put real OLLAMA_API_KEY"
    else
        ok "env key not a placeholder"
    fi
else
    fail "missing env file: $ENV_FILE"
fi

# 3. Venv + shebang
HEADROOM_BIN="$HEADROOM_VENV/bin/headroom"
if [[ -x "$HEADROOM_BIN" ]]; then
    ok "venv headroom binary present"
    shebang="$(head -1 "$HEADROOM_BIN")"
    expected="$HEADROOM_VENV/bin/python"
    if [[ "$shebang" == "#!$expected"* ]]; then
        ok "shebang points at venv python"
    else
        warn "shebang is '$shebang', expected '#!$expected'"
        warn "fix: sed -i \"1c\\\\#!$expected\" \"$HEADROOM_BIN\""
    fi
    # Also smoke-test: imports work
    if "$HEADROOM_VENV/bin/python" -c "from headroom.backends.litellm import LiteLLMBackend; print('ok')" >/dev/null 2>&1; then
        ok "headroom-ai imports cleanly"
    else
        fail "headroom-ai import failed — venv may be corrupt, reinstall with ./scripts/install.sh"
    fi
else
    fail "missing venv binary: $HEADROOM_BIN"
fi

# 4. systemd --user services (only if systemctl is usable)
if have_systemctl; then
    # Long-running services (proxy + failsafe daemon): check is-active
    for svc in headroom-proxy.service headroom-failsafe.service; do
        if systemctl --user is-active --quiet "$svc" 2>/dev/null; then
            ok "$svc active"
        else
            state="$(systemctl --user is-active "$svc" 2>/dev/null || echo unknown)"
            fail "$svc is $state"
        fi
    done
    # Watchdog service is Type=oneshot (one-shot per timer tick), so is-active
    # flips inactive between ticks. Check is-enabled + recent timer fire instead.
    if systemctl --user is-enabled --quiet headroom-watchdog.service 2>/dev/null; then
        ok "headroom-watchdog.service enabled (oneshot, fired by timer)"
    else
        fail "headroom-watchdog.service not enabled"
    fi
    if systemctl --user is-enabled --quiet headroom-watchdog.timer 2>/dev/null; then
        ok "watchdog timer enabled"
        # Recent fire check via systemd's LastTriggerTimes
        last="$(systemctl --user show headroom-watchdog.timer --property=LastTriggerUSecMonotonic --value 2>/dev/null || echo 0)"
        if [[ -n "$last" && "$last" != "0" ]]; then
            ok "watchdog timer has fired at least once (last USec: $last)"
        else
            warn "watchdog timer has not fired yet — may need up to 5 min after enable"
        fi
    else
        fail "watchdog timer not enabled"
    fi
else
    warn "systemctl --user is unavailable — skipping service checks"
    warn "(if you intended systemd integration, run inside a graphical session with"
    warn " 'loginctl enable-linger <username>' enabled)"
fi

# 5. /readyz
if curl -sf --max-time 5 "$PROXY_URL/readyz" >/dev/null 2>&1; then
    ok "/readyz reachable"
else
    fail "/readyz unreachable — proxy may be down"
fi

# 6. Auth probe
probe_resp="$(curl -sS --max-time 20 -X POST "$PROXY_URL/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$PROBE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\".\"}],\"max_tokens\":1}" 2>&1 || echo "")"
if echo "$probe_resp" | grep -q '"choices"'; then
    ok "auth probe succeeded (real LLM call worked)"
elif echo "$probe_resp" | grep -q '"error"'; then
    err_msg="$(echo "$probe_resp" | sed -n 's/.*"message":"\([^"]*\)".*/\1/p' | head -1)"
    fail "auth probe returned error: ${err_msg:-unknown}"
else
    fail "auth probe produced no recognizable response: $(echo "$probe_resp" | head -c 120)"
fi

# 7. hermes routing (only if hermes present)
if have_hermes; then
    routing="$(hermes config show 2>/dev/null | grep -oE "'base_url': '[^']*'" | head -1 | sed "s/'base_url': '//; s/'$//")"
    if [[ "$routing" == *"127.0.0.1:8787"* ]]; then
        ok "hermes routing → headroom proxy ($routing)"
    elif [[ "$routing" == *"ollama.com"* ]]; then
        warn "hermes routing → direct Ollama ($routing) — failsafe may have flipped this"
    elif [[ -n "$routing" ]]; then
        warn "hermes routing → unknown ($routing)"
    else
        warn "could not parse hermes config base_url"
    fi
else
    echo "  (skipping hermes routing check — hermes not installed)"
fi

# 8. journal / savings
SAVINGS="$HEADROOM_HOME/proxy_savings.jsonl"
JOURNAL="$HEADROOM_HOME/proxy_journal.jsonl"
if [[ -f "$SAVINGS" ]]; then
    line_count="$(wc -l < "$SAVINGS")"
    if [[ "$line_count" -gt 0 ]]; then
        ok "savings journal exists ($line_count records)"
        # Show last record's totals
        if command -v python3 >/dev/null 2>&1; then
            python3 -c "
import json
with open('$SAVINGS') as f:
    lines = [l for l in f if l.strip()]
print(f'  - records: {len(lines)}')
if lines:
    r = json.loads(lines[-1])
    print(f'  - tokens before compress: {r.get(\"input_total\",0)+r.get(\"output_total\",0):,}')
    print(f'  - tokens saved:           {r.get(\"saved_total\",0):,}')
    print(f'  - savings pct:            {r.get(\"savings_pct\",0)}%')
    print(f'  - requests (cumulative):  {r.get(\"requests_total\",0):,}')
" 2>/dev/null | sed 's/^/    /'
        fi
    else
        warn "savings journal exists but empty (no traffic yet?)"
    fi
else
    warn "no savings journal at $SAVINGS — proxy never wrote to it"
fi

echo
echo "=== SUMMARY ==="
printf "  %spassed:%s %d   %sfailed:%s %d   %swarned:%s %d\n" "$GRN" "$NC" "$PASS" "$RED" "$NC" "$FAIL" "$YEL" "$NC" "$WARN"
if [[ "$FAIL" -gt 0 ]]; then
    echo
    echo "Diagnose logs:"
    echo "  tail -50 $HEADROOM_HOME/watchdog.log"
    echo "  tail -50 $HEADROOM_HOME/failsafe.log"
    echo "  journalctl --user -u headroom-proxy.service -n 50"
    exit 1
fi
if [[ "$WARN" -gt 0 ]] && ! have_systemctl; then
    exit 2  # warn-but-no-systemd
fi
exit 0
