#!/usr/bin/env python3
"""
playlog_spotcheck.py -- random-sample the play log for manual verification.

"Steekproefgewijs" integrity check: before vouching to any party that the log
(and the monthly reports built from it) are correct, eyeball a random sample.
Prints N random plays from a month, shows the full-month vs sample pool
distribution (so you can see the sample is representative), and flags anything
that needs a look (unknown pool, empty artist/title).

Usage:
  playlog_spotcheck.py 2026-07              # 20 random plays
  playlog_spotcheck.py 2026-07 --n 50
  playlog_spotcheck.py 2026-07 --pool v4v   # sample only one pool
  playlog_spotcheck.py 2026-07 --seed 1     # reproducible sample
"""

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

DEFAULT_LOG = "/var/log/liquidsoap/playlog.tsv"


def read_plays(log_path, month, pool=None):
    p = Path(log_path)
    if not p.exists():
        sys.exit(f"play log not found: {log_path}")
    rows = []
    for line in p.open(encoding="utf-8", errors="replace"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 6:
            continue
        ts, event, source, rights, artist, title = parts
        if event != "play" or not ts.startswith(month):
            continue
        rights = rights or "unknown"
        if pool and rights != pool:
            continue
        rows.append({"ts": ts, "source": source, "rights": rights, "artist": artist, "title": title})
    return rows


def pct(counter):
    tot = sum(counter.values()) or 1
    return "  ".join(f"{k}={v} ({100*v/tot:.0f}%)" for k, v in counter.most_common())


def main():
    ap = argparse.ArgumentParser(description="Random-sample the play log for manual verification.")
    ap.add_argument("month", help="YYYY-MM")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--n", type=int, default=20, help="sample size (default 20)")
    ap.add_argument("--pool", help="restrict sample to one rights_class")
    ap.add_argument("--seed", type=int, help="fix RNG for a reproducible sample")
    args = ap.parse_args()

    plays = read_plays(args.log, args.month, args.pool)
    if not plays:
        sys.exit(f"no plays for {args.month}" + (f" pool={args.pool}" if args.pool else ""))
    if args.seed is not None:
        random.seed(args.seed)
    n = min(args.n, len(plays))
    sample = random.sample(plays, n)

    print(f"\n=== play-log spot check — {args.month} ===")
    print(f"total plays this month: {len(plays)}   sampling {n}\n")
    print("pool mix (full month):", pct(Counter(r['rights'] for r in plays)))
    print("pool mix (sample)    :", pct(Counter(r['rights'] for r in sample)))
    print()

    flagged = 0
    for r in sorted(sample, key=lambda x: x["ts"]):
        flag = ""
        if r["rights"] == "unknown":
            flag = "  <-- UNKNOWN pool"
        elif not r["artist"] and not r["title"]:
            flag = "  <-- EMPTY entry"
        if flag:
            flagged += 1
        at = f"{r['artist']} - {r['title']}".strip(" -") or "(empty)"
        print(f"  {r['ts']}  [{r['rights']:<18}] {r['source']:<12} {at}{flag}")

    print()
    print(f"{flagged} of {n} sampled rows need a look (unknown/empty).")
    print("Eyeball the rest against what you know aired. If the sample holds up,")
    print("the month's reporting can be trusted proportionally.")


if __name__ == "__main__":
    main()
