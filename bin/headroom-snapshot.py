#!/usr/bin/env python3
"""Daily Headroom token-savings snapshot.

Appends a single JSON line to ~/.headroom/proxy_savings.jsonl with:
  ts                - ISO timestamp
  uptime_s          - seconds since headroom proxy started
  input_total       - cumulative input tokens
  output_total      - cumulative output tokens
  saved_total       - cumulative tokens saved
  cache_bust_total  - cumulative tokens lost to cache busts
  savings_pct       - saved_total / input_total * 100 (current lifetime rate)

Why jsonl: append-only, crash-safe, easy to roll up later, no schema migration drama.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

PROXY_URL = os.environ.get("HEADROOM_URL", "http://127.0.0.1:8787")
STATE_DIR = Path.home() / ".headroom"
JOURNAL = STATE_DIR / "proxy_savings.jsonl"
ERROR_LOG = STATE_DIR / "snapshot_errors.log"


def log_err(msg):
    """Append-only error log, no exceptions raised from logging."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def fetch_metrics():
    """Pull metrics from headroom proxy, parse out the relevant counters."""
    url = f"{PROXY_URL}/metrics"
    req = urllib.request.Request(url, headers={"User-Agent": "headroom-snapshot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        text = r.read().decode()

    counters = {
        "input_total": 0,
        "output_total": 0,
        "saved_total": 0,
        "cache_bust_total": 0,
        "requests_total": 0,
        "requests_failed_total": 0,
    }
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, value = parts[0], parts[1]
        try:
            v = float(value)
        except ValueError:
            continue
        if name == "headroom_tokens_input_total":
            counters["input_total"] = int(v)
        elif name == "headroom_tokens_output_total":
            counters["output_total"] = int(v)
        elif name == "headroom_tokens_saved_total":
            counters["saved_total"] = int(v)
        elif name == "headroom_cache_bust_tokens_lost_total":
            counters["cache_bust_total"] = int(v)
        elif name == "headroom_requests_total":
            counters["requests_total"] = int(v)
        elif name == "headroom_requests_failed_total":
            counters["requests_failed_total"] = int(v)
    return counters


def get_uptime_seconds():
    """Read PID 1 of the headroom proxy and compute its uptime via /proc/<pid>/stat field 22 (starttime)."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "headroom proxy"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip().splitlines()
        if not out:
            return 0
        pid = int(out[0])
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        # field 22 (index 21) is starttime in clock ticks
        starttime_ticks = int(stat[21])
        clk_tck = os.sysconf("SC_CLK_TCK")
        # /proc/uptime gives wall seconds since boot
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        boot_to_now = uptime_s
        proc_age = (boot_to_now) - (starttime_ticks / clk_tck)
        return max(0, int(proc_age))
    except Exception as e:
        log_err(f"uptime lookup failed: {e}")
        return 0


def main():
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    try:
        counters = fetch_metrics()
    except Exception as e:
        log_err(f"metrics fetch failed: {e}")
        sys.exit(1)

    uptime = get_uptime_seconds()
    savings_pct = 0.0
    if counters["input_total"] > 0:
        savings_pct = round(counters["saved_total"] / counters["input_total"] * 100, 2)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uptime_s": uptime,
        **counters,
        "savings_pct": savings_pct,
    }

    line = json.dumps(record) + "\n"
    with open(JOURNAL, "a") as f:
        f.write(line)

    # Human-readable echo (visible if run from CLI)
    print(f"[{record['ts']}] up={uptime}s in={counters['input_total']:,} "
          f"out={counters['output_total']:,} saved={counters['saved_total']:,} "
          f"({savings_pct}%) -> {JOURNAL}")


if __name__ == "__main__":
    main()
