#!/usr/bin/env python3
"""Roll up lifetime Headroom savings from proxy_savings.jsonl journal.

Use cases:
  headroom-rollup.py            # show full history + delta between first/last
  headroom-rollup.py --since 30d  # only last 30 days
  headroom-rollup.py --json     # machine-readable
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JOURNAL = Path.home() / ".headroom" / "proxy_savings.jsonl"


def parse_ts(ts):
    # Tolerate missing tz
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def load(since=None):
    if not JOURNAL.exists():
        return []
    cutoff = None
    if since:
        cutoff = datetime.now(timezone.utc) - since
    records = []
    with open(JOURNAL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rec["_dt"] = parse_ts(rec["ts"])
                if cutoff and rec["_dt"] < cutoff:
                    continue
                records.append(rec)
            except Exception:
                continue
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="e.g. 30d, 12h, 7d")
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    args = ap.parse_args()

    since = None
    if args.since:
        n = int(args.since[:-1])
        unit = args.since[-1]
        td = {"h": timedelta(hours=n), "d": timedelta(days=n)}.get(unit)
        if not td:
            print(f"bad --since unit: {args.since}", file=sys.stderr)
            sys.exit(1)
        since = td

    recs = load(since)
    if not recs:
        print("no records", file=sys.stderr)
        sys.exit(1)

    first, last = recs[0], recs[-1]
    if args.json:
        out = {
            "first_snapshot": first["ts"],
            "last_snapshot": last["ts"],
            "snapshots": len(recs),
            "lifetime": {
                "input_total": last["input_total"],
                "output_total": last["output_total"],
                "saved_total": last["saved_total"],
                "cache_bust_total": last["cache_bust_total"],
                "savings_pct": last["savings_pct"],
                "requests_total": last["requests_total"],
                "requests_failed_total": last["requests_failed_total"],
            },
            "delta_since_first_snapshot": {
                "input": last["input_total"] - first["input_total"],
                "output": last["output_total"] - first["output_total"],
                "saved": last["saved_total"] - first["saved_total"],
            },
        }
        print(json.dumps(out, indent=2))
        return

    print(f"Headroom token savings rollup")
    print(f"  journal           : {JOURNAL}")
    print(f"  snapshots         : {len(recs)}")
    print(f"  first snapshot    : {first['ts']}")
    print(f"  last snapshot     : {last['ts']}")
    print()
    print(f"  lifetime input    : {last['input_total']:>14,}")
    print(f"  lifetime output   : {last['output_total']:>14,}")
    print(f"  lifetime saved    : {last['saved_total']:>14,}")
    print(f"  lifetime savings% : {last['savings_pct']:>13}%")
    print(f"  cache bust lost   : {last['cache_bust_total']:>14,}")
    print(f"  requests total    : {last['requests_total']:>14,}")
    print(f"  requests failed   : {last['requests_failed_total']:>14,}")
    if len(recs) >= 2:
        d = last["saved_total"] - first["saved_total"]
        print()
        print(f"  since first snap  : +{d:,} tokens saved")


if __name__ == "__main__":
    main()
