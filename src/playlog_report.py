#!/usr/bin/env python3
"""
playlog_report.py -- monthly royalty-pool rollup from the play log.

Reads the append-only TSV the liquidsoap on_track hook writes and produces one
summary per rights pool for a given month. This is the reporting half of the
"one stream, one log, filter at report time" design.

Log format (tab-separated, 6 columns):
    timestamp_iso  event  source  rights_class  artist  title

  event:        play | set_start | set_end
  rights_class: collecting_society | v4v | owned | public_domain | cc0 | unknown
  For set_start/set_end rows: artist = DJ name, title = set label.

Pools:
  collecting_society -> BUMA/STEMRA + SENA reportable
  v4v                -> Wavlake / value-for-value artist payouts
  cc0 / public_domain-> free lane, tracked for completeness (no society report)
  unknown            -> UNATTRIBUTED — must be triaged, never silently dropped

Usage:
  playlog_report.py 2026-07
  playlog_report.py 2026-07 --pool collecting_society
  playlog_report.py 2026-07 --csv out.csv          # per-track counts for the pool
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_LOG = "/var/log/liquidsoap/playlog.tsv"

SOCIETY = "collecting_society"
POOL_ORDER = [SOCIETY, "v4v", "owned", "public_domain", "cc0", "unknown"]
POOL_LABEL = {
    SOCIETY: "BUMA/STEMRA + SENA (collecting society)",
    "v4v": "Wavlake / value-for-value",
    "owned": "Owned (bought outright)",
    "public_domain": "Public domain",
    "cc0": "CC0",
    "unknown": "UNATTRIBUTED — needs triage",
}


def read_rows(log_path: str, month: str):
    """Yield parsed rows whose timestamp starts with `month` (YYYY-MM)."""
    p = Path(log_path)
    if not p.exists():
        sys.exit(f"play log not found: {log_path}")
    with p.open(encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 6:
                print(f"  ! skipping malformed line {lineno} ({len(parts)} cols)", file=sys.stderr)
                continue
            ts, event, source, rights, artist, title = parts
            if not ts.startswith(month):
                continue
            yield {
                "ts": ts, "event": event, "source": source,
                "rights": rights or "unknown", "artist": artist, "title": title,
            }


def summarize(rows):
    plays_by_pool = defaultdict(list)          # pool -> [ (artist,title), ... ]
    dj_sets = []                               # (start_ts, dj, label, end_ts|None)
    open_sets = {}                             # dj -> (start_ts, label)
    for r in rows:
        if r["event"] == "play":
            plays_by_pool[r["rights"]].append((r["artist"], r["title"]))
        elif r["event"] == "set_start":
            open_sets[r["artist"]] = (r["ts"], r["title"])
        elif r["event"] == "set_end":
            start = open_sets.pop(r["artist"], (None, r["title"]))
            dj_sets.append((start[0], r["artist"], start[1], r["ts"]))
    for dj, (start_ts, label) in open_sets.items():   # sets with no end logged
        dj_sets.append((start_ts, dj, label, None))
    return plays_by_pool, dj_sets


def print_report(month, plays_by_pool, dj_sets, only_pool=None):
    total = sum(len(v) for v in plays_by_pool.values())
    print(f"\n=== Noderunners Radio play report — {month} ===")
    print(f"Total logged track-plays: {total}\n")

    for pool in POOL_ORDER:
        if only_pool and pool != only_pool:
            continue
        plays = plays_by_pool.get(pool, [])
        if not plays and only_pool is None and pool not in (SOCIETY, "v4v", "unknown"):
            continue
        counts = Counter(plays)
        print(f"--- {POOL_LABEL[pool]} ---")
        print(f"    plays: {len(plays)}   unique tracks: {len(counts)}")
        for (artist, title), n in counts.most_common():
            print(f"      {n:>4}×  {artist} - {title}")
        if pool == "unknown" and plays:
            print("    ^ these aired without a rights tag — fix the source's annotation.")
        print()

    if dj_sets and not only_pool:
        print("--- Live DJ sets (ad hoc; tracklists not expected) ---")
        for start_ts, dj, label, end_ts in sorted(dj_sets, key=lambda x: x[0] or ""):
            span = f"{start_ts or '?'} → {end_ts or '(no end logged)'}"
            print(f"      {dj}: {label}   [{span}]")
        print()


def write_csv(path, month, plays_by_pool, pool):
    counts = Counter(plays_by_pool.get(pool, []))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "rights_class", "artist", "title", "plays"])
        for (artist, title), n in counts.most_common():
            w.writerow([month, pool, artist, title, n])
    print(f"wrote {path} ({len(counts)} rows for pool '{pool}')")


def main():
    ap = argparse.ArgumentParser(description="Monthly royalty-pool rollup from the play log.")
    ap.add_argument("month", help="YYYY-MM, e.g. 2026-07")
    ap.add_argument("--log", default=DEFAULT_LOG, help=f"play log path (default {DEFAULT_LOG})")
    ap.add_argument("--pool", choices=POOL_ORDER, help="restrict output to one pool")
    ap.add_argument("--csv", help="write per-track counts for --pool (or collecting_society) to this CSV")
    args = ap.parse_args()

    rows = list(read_rows(args.log, args.month))
    plays_by_pool, dj_sets = summarize(rows)
    print_report(args.month, plays_by_pool, dj_sets, only_pool=args.pool)
    if args.csv:
        write_csv(args.csv, args.month, plays_by_pool, args.pool or SOCIETY)


if __name__ == "__main__":
    main()
