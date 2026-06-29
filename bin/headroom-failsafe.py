#!/usr/bin/env python3
"""Headroom failsafe — reactive circuit breaker.

Watches headroom proxy health. When headroom stays unhealthy across multiple
self-heal attempts (tracked by the watchdog's consecutive_self_heal_failures
counter), this daemon flips Hermes config to route directly to Ollama Cloud,
restoring user-facing service.

Runs as a systemd service, ticks every 30s.

State flow:
  - Read watchdog-state.json → consecutive_self_heal_failures
  - If routed-through-headroom AND headroom unhealthy AND counter >= 3:
      FLIP TO DIRECT (kill-switch)
  - If routed-direct AND headroom healthy:
      FLIP BACK TO HEADROOM (recovery)
  - If direct AND headroom still unhealthy: stay direct, keep checking

State file: ~/.headroom/failsafe-state.json (separate from watchdog state
            so the two services can run independently)
Log file:   ~/.headroom/failsafe.log
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ---- Config (HEADROOM_URL/DIRECT_URL defined above) ----
HEALTHCHECK_TIMEOUT = 3
SELF_HEAL_FAILURES_THRESHOLD = 3
STATE_FILE = Path.home() / ".headroom" / "failsafe-state.json"
WATCHDOG_STATE_FILE = Path.home() / ".headroom" / "watchdog-state.json"
LOG_FILE = Path.home() / ".headroom" / "failsafe.log"
# Operator-controlled mode file. Values: "auto" (default), "learn", "crawl".
# In "learn" or "crawl" mode the failsafe pins routing to direct Ollama
# regardless of headroom health. Useful for: scraping/crawling where
# compression overhead exceeds savings, or training data ingestion where
# every prompt is unique.
MODE_FILE = Path.home() / ".headroom" / "failsafe-mode"
DIRECT_URL = "https://ollama.com/v1"
HEADROOM_URL = "http://127.0.0.1:8787"
LEARN_MODE_URLS = {
    "learn": "https://ollama.com/v1",
    "crawl": "https://ollama.com/v1",
}


def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line.rstrip())


def load_state():
    """Load failsafe state, backfilling any missing keys for forward compat."""
    state = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    defaults = {
        "routing": "headroom",  # "headroom" | "direct" | "unknown"
        "last_action": None,
        "last_action_at": None,
        "last_killswitch_at": None,
        "last_recovery_at": None,
        "flips_total": 0,
        "mode": "auto",  # last-observed operator mode
        "mode_pinned_flips": 0,  # flips caused by mode change rather than health
    }
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def check_headroom():
    """Returns (healthy, auth_ok, detail). Combines readyz + auth probe."""
    readyz_ok, readyz_detail = _check_readyz()
    if not readyz_ok:
        return False, None, readyz_detail
    auth_ok, auth_detail = _check_auth()
    if not auth_ok:
        return True, False, f"readyz green but auth broken: {auth_detail}"
    return True, True, "readyz green + auth ok"


def _check_readyz():
    """Check /readyz endpoint. Returns (healthy, detail)."""
    try:
        with urllib.request.urlopen(
            f"{HEADROOM_URL}/readyz", timeout=HEALTHCHECK_TIMEOUT
        ) as r:
            body = r.read().decode().lower()
            if "healthy" in body or "ready" in body:
                return True, "readyz green"
            return False, f"readyz returned 200 but body lacks healthy/ready: {body[:100]}"
    except urllib.error.URLError as e:
        return False, f"readyz unreachable: {e}"
    except Exception as e:
        return False, f"readyz error: {type(e).__name__}: {e}"


def _check_auth():
    """Real round-trip auth probe. See headroom-watchdog.check_auth() for rationale."""
    try:
        req = urllib.request.Request(
            f"{HEADROOM_URL}/v1/chat/completions",
            data=json.dumps({
                "model": "kimi-k2.6",
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            parsed = json.loads(body)
            if "error" in parsed:
                return False, parsed["error"].get("code", "unknown")
            return True, "auth ok"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
            parsed = json.loads(err_body)
            if "error" in parsed:
                return False, f"http {e.code}: {parsed['error'].get('code', 'unknown')}"
        except Exception:
            pass
        return False, f"http {e.code}"
    except Exception as e:
        return False, f"probe failed: {type(e).__name__}: {e}"


def get_current_base_url():
    """Read Hermes config to determine current routing target.

    `hermes config show` accepts no args and dumps the full config. The base_url
    may appear on its own line (yaml-style) or inline as part of a Python dict
    literal ('Model: {...}'). We grep for 'base_url' and extract whatever
    quoted/unquoted value follows.
    """
    try:
        out = subprocess.check_output(
            ["hermes", "config", "show"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        for line in out.splitlines():
            if "base_url" not in line:
                continue
            # Try yaml-style first: "base_url: <value>"
            if "base_url:" in line:
                after = line.split("base_url:", 1)[1].strip().strip('"').strip("'")
                if after and not after.startswith("{"):
                    return after
            # Try dict-literal style: "'base_url': '<value>'"
            if "'base_url'" in line or '"base_url"' in line:
                # Find the value: look for the quote after base_url key
                idx = line.find("base_url")
                if idx == -1:
                    continue
                rest = line[idx:]
                # skip past 'base_url' + optional colon + quote
                for q in ("'", '"'):
                    sep = f"base_url{q}':" if False else None
                    pat_single = f"'base_url':"
                    pat_double = f'"base_url":'
                    for pat in (pat_single, pat_double):
                        if pat in rest:
                            after = rest.split(pat, 1)[1].strip()
                            # value is quoted
                            if after.startswith(q):
                                # find closing quote
                                end = after.find(q, 1)
                                if end > 0:
                                    return after[1:end]
                # fallback: grep for http(s)://... in the line
                import re
                m = re.search(r"['\"](https?://[^'\"]+)['\"]", line)
                if m:
                    return m.group(1)
        return None
    except Exception as e:
        log(f"FAILSAFE: get_current_base_url error: {e}")
        return None


def get_watchdog_failures():
    """Read consecutive_self_heal_failures from watchdog state. Returns int."""
    if not WATCHDOG_STATE_FILE.exists():
        return 0
    try:
        with open(WATCHDOG_STATE_FILE) as f:
            wd = json.load(f)
        return int(wd.get("consecutive_self_heal_failures", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def flip_to_direct(state, reason):
    """Flip Hermes config to direct Ollama. Updates state."""
    log(f"KILL-SWITCH ACTIVATING: {reason}")
    try:
        subprocess.run(
            ["hermes", "config", "set", "model.base_url", DIRECT_URL],
            check=True, timeout=10,
        )
        state["routing"] = "direct"
        state["last_killswitch_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_action"] = f"killswitch: {reason}"
        state["last_action_at"] = state["last_killswitch_at"]
        state["flips_total"] += 1
        log(f"KILL-SWITCH FLIPPED to {DIRECT_URL} (flips_total={state['flips_total']})")
        return True
    except subprocess.CalledProcessError as e:
        log(f"KILL-SWITCH FAILED: hermes config set rc={e.returncode}: {e.stderr if hasattr(e, 'stderr') else ''}")
        return False
    except Exception as e:
        log(f"KILL-SWITCH FAILED: {type(e).__name__}: {e}")
        return False


def flip_to_headroom(state, reason):
    """Flip Hermes config back to headroom proxy. Updates state."""
    log(f"RECOVERY: {reason}")
    try:
        subprocess.run(
            ["hermes", "config", "set", "model.base_url", f"{HEADROOM_URL}/v1"],
            check=True, timeout=10,
        )
        state["routing"] = "headroom"
        state["last_recovery_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_action"] = f"recovery: {reason}"
        state["last_action_at"] = state["last_recovery_at"]
        log(f"RECOVERY: flipped back to {HEADROOM_URL}/v1")
        return True
    except subprocess.CalledProcessError as e:
        log(f"RECOVERY FAILED: rc={e.returncode}")
        return False
    except Exception as e:
        log(f"RECOVERY FAILED: {type(e).__name__}: {e}")
        return False


def get_mode():
    """Read operator-set mode. Returns "auto" | "learn" | "crawl".

    Read from MODE_FILE. Strips whitespace, lowercases, defaults to "auto".
    Unknown values are coerced to "auto" + logged.
    """
    if not MODE_FILE.exists():
        return "auto"
    try:
        with open(MODE_FILE) as f:
            val = f.read().strip().lower()
    except OSError as e:
        log(f"FAILSAFE: get_mode read error: {e}")
        return "auto"
    if val not in ("auto", "learn", "crawl"):
        log(f"FAILSAFE: unknown mode '{val}' in {MODE_FILE}, treating as 'auto'")
        return "auto"
    return val


def set_mode(new_mode):
    """Write mode to MODE_FILE. Returns True on success."""
    if new_mode not in ("auto", "learn", "crawl"):
        log(f"FAILSAFE: set_mode rejected invalid mode '{new_mode}'")
        return False
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(MODE_FILE, "w") as f:
            f.write(new_mode + "\n")
        os.chmod(MODE_FILE, 0o644)
        log(f"FAILSAFE: mode set to '{new_mode}'")
        return True
    except OSError as e:
        log(f"FAILSAFE: set_mode write error: {e}")
        return False


def tick():
    """One iteration of the failsafe loop."""
    state = load_state()
    mode = get_mode()
    state["mode"] = mode
    current_url = get_current_base_url()

    # Where does Hermes think it's routing right now?
    if current_url and "127.0.0.1:8787" in current_url:
        effective_routing = "headroom"
    elif current_url and "ollama.com" in current_url:
        effective_routing = "direct"
    else:
        effective_routing = "unknown"

    # ---- Mode override (learn / crawl) ----
    # If operator has set learn or crawl mode, force routing to direct regardless
    # of headroom health. This is an immediate-effect operator toggle.
    if mode in ("learn", "crawl"):
        if effective_routing != "direct":
            target_url = LEARN_MODE_URLS[mode]
            log(f"MODE OVERRIDE: mode={mode} forcing direct routing to {target_url}")
            try:
                subprocess.run(
                    ["hermes", "config", "set", "model.base_url", target_url],
                    check=True, timeout=10,
                )
                state["routing"] = "direct"
                state["last_action"] = f"mode-pinned: {mode}"
                state["last_action_at"] = datetime.now().isoformat(timespec="seconds")
                state["flips_total"] += 1
                state["mode_pinned_flips"] += 1
                save_state(state)
                log(f"MODE OVERRIDE: flipped to direct ({mode}) — flips_total={state['flips_total']}")
            except subprocess.CalledProcessError as e:
                log(f"MODE OVERRIDE: hermes config set rc={e.returncode}")
            except Exception as e:
                log(f"MODE OVERRIDE: {type(e).__name__}: {e}")
        # In mode-pinned state, do not run normal flip/recover logic below.
        # Stay direct until mode changes back to "auto".
        return

    # If mode is "auto" and we're routed direct because of a previous learn/crawl
    # pin, but routing should now reflect actual headroom health, fall through to
    # the normal decision matrix below. (Recovery back to headroom happens here.)

    # Probe headroom (readyz + auth)
    readyz_ok, auth_ok, detail = check_headroom()
    # Treat headroom as "broken" if readyz is down OR auth is broken
    headroom_healthy = readyz_ok and (auth_ok is True)
    watchdog_failures = get_watchdog_failures()
    log(f"tick: routing={effective_routing}, readyz={readyz_ok}, auth={auth_ok}, counter={watchdog_failures}")
    readyz_ok, auth_ok, detail = check_headroom()
    # Treat headroom as "broken" if readyz is down OR auth is broken
    headroom_healthy = readyz_ok and (auth_ok is True)
    watchdog_failures = get_watchdog_failures()
    log(f"tick: routing={effective_routing}, readyz={readyz_ok}, auth={auth_ok}, counter={watchdog_failures}")

    # ---- Decision matrix ----

    # Case 1: routed through headroom, headroom healthy → all good
    if effective_routing == "headroom" and headroom_healthy:
        # nothing to do
        return

    # Case 2: routed through headroom, headroom unhealthy, watchdog gave up → flip to direct
    if effective_routing == "headroom" and not headroom_healthy and watchdog_failures >= SELF_HEAL_FAILURES_THRESHOLD:
        flip_to_direct(
            state,
            f"headroom unhealthy ({detail}), watchdog self-heal failed {watchdog_failures}x",
        )

    # Case 3: routed through headroom, headroom unhealthy, watchdog still trying → wait
    if effective_routing == "headroom" and not headroom_healthy and watchdog_failures < SELF_HEAL_FAILURES_THRESHOLD:
        # Don't flip yet — let watchdog try
        return

    # Case 4: routed direct, headroom healthy → recover
    if effective_routing == "direct" and headroom_healthy:
        flip_to_headroom(state, f"headroom healthy again (watchdog_failures={watchdog_failures})")

    # Case 5: routed direct, headroom still unhealthy → stay direct, wait
    if effective_routing == "direct" and not headroom_healthy:
        # nothing to do
        return

    save_state(state)


def show_status():
    """Print current mode + state for operator inspection."""
    mode = get_mode()
    state = load_state()
    print(f"mode:           {mode}")
    print(f"routing:        {state.get('routing', 'unknown')}")
    print(f"flips_total:    {state.get('flips_total', 0)}")
    print(f"mode_pinned:    {state.get('mode_pinned_flips', 0)}")
    print(f"last_action:    {state.get('last_action', 'none')}")
    print(f"last_action_at: {state.get('last_action_at', 'never')}")
    print(f"mode_file:      {MODE_FILE}")
    return 0


def apply_mode_immediate(new_mode):
    """Set mode and immediately force the routing flip in this process.

    Used when the operator toggles via CLI but the daemon is not running —
    we still want the immediate-effect semantics.
    """
    if set_mode(new_mode):
        if new_mode == "auto":
            # Force a normal tick to recover if possible
            tick()
        else:
            # Force-pin to direct right now
            try:
                subprocess.run(
                    ["hermes", "config", "set", "model.base_url", DIRECT_URL],
                    check=True, timeout=10,
                )
                state = load_state()
                state["routing"] = "direct"
                state["last_action"] = f"mode-pinned: {new_mode} (CLI)"
                state["last_action_at"] = datetime.now().isoformat(timespec="seconds")
                state["flips_total"] += 1
                state["mode_pinned_flips"] += 1
                save_state(state)
                print(f"Mode set to '{new_mode}'. Routing forced to {DIRECT_URL}.")
                print(f"  (the running failsafe daemon will see this on its next tick too)")
            except Exception as e:
                print(f"Mode set to '{new_mode}' but routing flip failed: {e}")
                return 1
    else:
        print(f"Failed to set mode to '{new_mode}'")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Headroom failsafe — reactive circuit breaker + mode override",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single tick and exit (systemd timer-style)")
    parser.add_argument("--mode", choices=["auto", "learn", "crawl"],
                        help="Set operator mode (auto=normal failsafe, learn/crawl=bypass headroom)")
    parser.add_argument("--status", action="store_true",
                        help="Show current mode + state")
    args = parser.parse_args()

    # CLI mode toggle (immediate effect)
    if args.mode:
        return apply_mode_immediate(args.mode)

    # Status display
    if args.status:
        return show_status()

    # Single-tick mode
    if args.once:
        tick()
        return 0

    # Daemon mode — loop forever
    log("FAILSAFE: daemon starting")
    while True:
        try:
            tick()
        except Exception as e:
            log(f"FAILSAFE: tick raised: {type(e).__name__}: {e}")
            import traceback
            log(f"FAILSAFE: traceback: {traceback.format_exc()}")
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())