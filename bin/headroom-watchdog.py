#!/usr/bin/env python3
"""Headroom stability + self-heal watchdog.

Runs every 5 minutes via systemd timer. Two-phase behavior:

  Phase 1 (PRE-PROMOTION, first 3 days):
    Checks proxy stats and tracks consecutive stable days.
    After REQUIRED_DAYS clean days, marks the deployment promoted.

  Phase 2 (POST-PROMOTION, permanent):
    Keeps monitoring forever but shifts focus from stability-tracking
    to self-heal. Detects: venv broken, env file missing, service
    down, restart-loop storm, AND upstream auth failures (broken API
    key — /readyz doesn't catch this since headroom doesn't validate
    the key until a real request goes through).

    Attempts safe automatic recovery:
      - resets systemd failed counter
      - if venv broken, `uv pip install --force-reinstall headroom-ai[proxy]`
      - restart service
    Skips auto-heal if env file has placeholder/missing key (owner must
    intervene — we don't fabricate credentials).

    On every failed heal, increments consecutive_self_heal_failures in
    the shared state file. The headroom-failsafe.py daemon reads this
    counter and flips Hermes config to direct Ollama when it reaches
    threshold (default 3). This is the "3-strike rule" that the old
    manual rule of thumb never implemented.

Why this exists: the previous watchdog marked the deployment
"permanent" after 3 stable days and then refused to monitor at all,
which left a 2-day silent outage (proxy down, journal stalled)
undetected. This version keeps monitoring even after promotion.

State file: ~/.headroom/watchdog-state.json (shared with failsafe)
Log file:   ~/.headroom/watchdog.log
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

PROXY_URL = "http://127.0.0.1:8787"
STATE_FILE = Path.home() / ".headroom" / "watchdog-state.json"
LOG_FILE = Path.home() / ".headroom" / "watchdog.log"
REQUIRED_DAYS = 3

ERROR_RATE_MAX = 0.05
AVG_COMPRESSION_MIN = 30.0
MIN_REQUESTS = 10

ENV_FILE = Path.home() / ".headroom" / "headroom.env"
VENV_HEADROOM_BIN = Path.home() / ".local" / "venvs" / "headroom" / "bin" / "headroom"
SERVICE_NAME = "headroom-proxy.service"


def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line.rstrip())


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default_state()
    return default_state()


def default_state():
    return {
        "consecutive_stable_days": 0,
        "last_check": None,
        "history": [],
        "promoted": False,
        "promoted_at": None,
        "started_at": datetime.now().isoformat(),
        "consecutive_self_heal_failures": 0,
        "last_self_heal_attempt_at": None,
        "last_self_heal_result": None,
    }


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def fetch_stats():
    try:
        with urllib.request.urlopen(f"{PROXY_URL}/stats", timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def check_health():
    try:
        with urllib.request.urlopen(f"{PROXY_URL}/readyz", timeout=5) as resp:
            body = resp.read().decode().lower()
            return "healthy" in body or "ready" in body
    except Exception:
        return False


def check_auth():
    """Real round-trip auth probe. Headroom's /readyz doesn't validate the
    upstream API key — it reports healthy even when OLLAMA_API_KEY is broken.
    Only a real request can detect a bad key.
    """
    try:
        req = urllib.request.Request(
            f"{PROXY_URL}/v1/chat/completions",
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
            try:
                parsed = json.loads(body)
                if "error" in parsed:
                    return False, f"upstream error: {parsed['error'].get('code', 'unknown')}"
                return True, "auth ok"
            except json.JSONDecodeError:
                return False, f"non-json response: {body[:100]}"
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


def evaluate(stats, healthy):
    if not healthy:
        return False, "proxy unhealthy (readyz failed)"
    if stats is None:
        return False, "stats endpoint unreachable"
    summary = stats.get("summary", {})
    api_requests = summary.get("api_requests", 0)
    if api_requests < MIN_REQUESTS:
        return False, f"insufficient traffic ({api_requests} requests)"
    compression = summary.get("compression", {})
    avg = compression.get("avg_compression_pct", 0)
    if avg < AVG_COMPRESSION_MIN:
        return False, f"avg compression regressed to {avg}%"
    errors = summary.get("errors", 0)
    error_rate = errors / api_requests if api_requests else 0
    if error_rate > ERROR_RATE_MAX:
        return False, f"error rate {error_rate:.1%} exceeds threshold"
    if stats.get("upstream_unauthorized_count", 0) > 0:
        return False, "upstream 401 detected"
    return True, "all checks passed"


def already_checked_today(state):
    last = state.get("last_check")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return False
    return last_dt.date() == datetime.now().date()


# ---------- Self-heal helpers ----------

def systemctl(*args, timeout=15):
    try:
        r = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def check_venv_health():
    if not VENV_HEADROOM_BIN.is_file():
        return False, "venv binary missing"
    try:
        with open(VENV_HEADROOM_BIN) as f:
            first_line = f.readline(200)
    except OSError as e:
        return False, f"venv binary unreadable: {e}"
    if not first_line.startswith("#!"):
        return False, "venv binary missing shebang"
    interp = first_line[2:].strip().split()[0]
    if not Path(interp).is_file():
        return False, f"venv shebang interpreter missing: {interp}"
    try:
        r = subprocess.run(
            [str(VENV_HEADROOM_BIN), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"venv --version failed (rc={r.returncode})"
    except Exception as e:
        return False, f"venv --version raised: {e}"
    return True, "venv healthy"


def check_env_file():
    if not ENV_FILE.is_file():
        return False, "env file missing"
    try:
        content = ENV_FILE.read_text()
    except OSError as e:
        return False, f"env file unreadable: {e}"
    if "OLLAMA_API_KEY" not in content:
        return False, "env file missing OLLAMA_API_KEY"
    for line in content.splitlines():
        if line.startswith("OLLAMA_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if not value or value.startswith("__PEND") or value == "your_o...here":
                return False, "env file OLLAMA_API_KEY is placeholder"
    return True, "env file has real key"


def get_service_state():
    rc, out = systemctl("is-active", SERVICE_NAME)
    active = (rc == 0 and out.strip() == "active")
    return active, out.strip()


def get_restart_loop_count():
    rc, out = systemctl("show", SERVICE_NAME, "-p", "NRestarts", "--value")
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def attempt_heal(reason):
    log(f"HEAL: attempting recovery — reason: {reason}")
    actions = []
    rc, out = systemctl("reset-failed", SERVICE_NAME)
    if rc == 0:
        actions.append("reset-failed")

    venv_ok, venv_msg = check_venv_health()
    env_ok, env_msg = check_env_file()

    if not venv_ok:
        log(f"HEAL: venv unhealthy ({venv_msg}) — attempting uv pip reinstall")
        rc, out = run(
            f"uv pip install --python {Path.home() / '.local' / 'venvs' / 'headroom' / 'bin' / 'python'} 'headroom-ai[proxy]' --force-reinstall"
        )
        if rc == 0:
            actions.append("venv-reinstalled")
        else:
            log(f"HEAL: venv reinstall failed: {out[-500:]}")
            return False

    if not env_ok:
        log(f"HEAL: env file unhealthy ({env_msg}) — cannot auto-recover")
        return False

    rc, out = systemctl("start", SERVICE_NAME)
    actions.append(f"start (rc={rc})")
    log(f"HEAL: actions taken: {', '.join(actions)}")

    time.sleep(3)
    healthy = check_health()
    if not healthy:
        log("HEAL: FAILED — readyz still red after restart")
        return False
    # Re-verify auth too — /readyz green doesn't mean key works
    auth_ok, auth_detail = check_auth()
    if not auth_ok:
        log(f"HEAL: FAILED — auth still broken after restart: {auth_detail}")
        return False
    log("HEAL: SUCCESS — proxy healthy AND auth verified")
    return True


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1-only", action="store_true",
                    help="Skip Phase 2. Used by legacy daily cron path; not used by 5-min timer.")
    args = ap.parse_args()

    state = load_state()

    # ---- Phase 2: promoted deployment — monitor + self-heal ----
    if state.get("promoted") and not args.phase1_only:
        log(f"PROMOTED-PERMANENT check at {datetime.now().isoformat(timespec='seconds')}")

        issues = []

        venv_ok, venv_msg = check_venv_health()
        if not venv_ok:
            issues.append(f"venv: {venv_msg}")

        env_ok, env_msg = check_env_file()
        if not env_ok:
            issues.append(f"env: {env_msg}")

        active, sub = get_service_state()
        if not active:
            issues.append(f"service: {sub}")

        healthy = check_health() if active else False
        if not healthy and active:
            issues.append("proxy: readyz failed")

        # Real auth probe — only when readyz is green (saves LLM calls when
        # the proxy itself is broken)
        if healthy:
            auth_ok, auth_detail = check_auth()
            if not auth_ok:
                issues.append(f"upstream auth: {auth_detail}")

        restarts = get_restart_loop_count()
        if restarts > 50:
            issues.append(f"restart-storm: {restarts} restarts")

        if not issues:
            log("PROMOTED-PERMANENT: all green")
            if state.get("consecutive_self_heal_failures", 0) > 0:
                log(f"PROMOTED-PERMANENT: resetting consecutive_self_heal_failures from {state['consecutive_self_heal_failures']} to 0")
            state["consecutive_self_heal_failures"] = 0
            save_state(state)
            return 0

        log(f"PROMOTED-PERMANENT: issues detected — {', '.join(issues)}")

        if not env_ok:
            log("PROMOTED-PERMANENT: env file issue requires owner — leaving service stopped, no auto-restart")
            state["consecutive_self_heal_failures"] = state.get("consecutive_self_heal_failures", 0) + 1
            state["last_self_heal_attempt_at"] = datetime.now().isoformat()
            state["last_self_heal_result"] = "skipped (env issue)"
            save_state(state)
            return 1

        recovered = attempt_heal(reason="; ".join(issues))
        state["last_self_heal_attempt_at"] = datetime.now().isoformat()

        if recovered:
            state["consecutive_self_heal_failures"] = 0
            state["last_self_heal_result"] = "success"
            state["history"].append({
                "date": datetime.now().date().isoformat(),
                "ok": True,
                "reason": f"regression recovered: {'; '.join(issues)}",
                "healthy": True,
                "recovered": True,
                "summary": {},
            })
            state["history"] = state["history"][-30:]
            state["last_check"] = datetime.now().isoformat()
            save_state(state)
            return 0
        else:
            state["consecutive_self_heal_failures"] = state.get("consecutive_self_heal_failures", 0) + 1
            state["last_self_heal_result"] = "failure"
            state["history"].append({
                "date": datetime.now().date().isoformat(),
                "ok": False,
                "reason": f"regression unrecovered (strike {state['consecutive_self_heal_failures']}): {'; '.join(issues)}",
                "healthy": False,
                "recovered": False,
                "summary": {},
            })
            state["history"] = state["history"][-30:]
            state["last_check"] = datetime.now().isoformat()
            save_state(state)
            log(f"PROMOTED-PERMANENT: self-heal strike {state['consecutive_self_heal_failures']} — failsafe may now flip kill-switch")
            return 1

    # ---- Phase 1: pre-promotion stability tracking ----
    if already_checked_today(state):
        log("Already checked today, skipping.")
        return 0

    healthy = check_health()
    stats = fetch_stats()
    ok, reason = evaluate(stats, healthy)

    snapshot = {
        "date": datetime.now().date().isoformat(),
        "ok": ok,
        "reason": reason,
        "healthy": healthy,
        "summary": (stats or {}).get("summary", {}),
    }

    if ok:
        state["consecutive_stable_days"] += 1
        log(f"STABLE day {state['consecutive_stable_days']}/{REQUIRED_DAYS}: {reason}")
    else:
        if state["consecutive_stable_days"] > 0:
            log(f"REGRESSION: {reason}. Resetting counter from {state['consecutive_stable_days']} to 0.")
        else:
            log(f"NOT STABLE: {reason}")
        state["consecutive_stable_days"] = 0

    state["history"].append(snapshot)
    state["history"] = state["history"][-30:]
    state["last_check"] = datetime.now().isoformat()

    if state["consecutive_stable_days"] >= REQUIRED_DAYS:
        state["promoted"] = True
        state["promoted_at"] = datetime.now().isoformat()
        log(f"PROMOTED: {REQUIRED_DAYS} consecutive stable days achieved.")
        log(f"Watchdog continues in monitor+self-heal mode.")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())