#!/usr/bin/env python3
"""
scan_library.py -- index your music into SQLite so the jukebox can find it.

    python3 scan_library.py /path/to/music --rights collecting_society
    python3 scan_library.py /path/to/wavlake-downloads --rights v4v
    python3 scan_library.py /path/to/bandcamp --rights owned

WHY --rights IS MANDATORY:
Owning a file is NOT the same as having the right to broadcast it. A CD you ripped
is still society repertoire. You must tell the index which bucket a folder is, and
that truth then flows all the way through to the BUMA/STEMRA + SENA report.
Guessing here is how stations get in trouble.

Re-running is safe and cheap: unchanged files are skipped (size+mtime), so a
second pass over 7 TB takes minutes, not hours.

    pip install mutagen
"""

import argparse
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("Need mutagen:  pip install mutagen")

AUDIO = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma", ".aiff"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    path         TEXT PRIMARY KEY,
    sha256       TEXT,
    artist       TEXT,
    title        TEXT,
    album        TEXT,
    isrc         TEXT,
    duration_s   INTEGER,
    rights_class TEXT NOT NULL,
    size         INTEGER,
    mtime        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_title  ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_sha    ON tracks(sha256);
"""


def sha256(p: Path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while (b := f.read(chunk)):
            h.update(b)
    return h.hexdigest()


def tags(p: Path) -> dict:
    """Best-effort tag read. ISRC is the field SENA actually needs — chase it."""
    try:
        m = MutagenFile(p, easy=True)
        if m is None:
            return {}
        # strip(): real tags carry trailing whitespace ("The Flashbulb    ")
        # and that dirt flowed into the playlog verbatim (seen 2026-07-29)
        def g(k):
            v = (m.get(k) or [None])[0]
            return v.strip() if isinstance(v, str) else v
        isrc = g("isrc")
        if not isrc:                      # easy mode misses ISRC on some formats
            raw = MutagenFile(p)
            raw_tags = getattr(raw, "tags", None) or {}
            for k in ("TSRC", "ISRC", "----:com.apple.iTunes:ISRC"):
                if k in raw_tags:
                    try:
                        isrc = str(raw_tags[k][0])
                        break
                    except Exception:
                        pass
        return {
            "artist": g("artist"),
            "title": g("title"),
            "album": g("album"),
            "isrc": isrc,
            "duration_s": int(m.info.length) if getattr(m, "info", None) else None,
        }
    except Exception:
        return {}


# Track numbers come in many dialects: "02. ", "02 - ", "02_", "02 ",
# and vinyl-style "A2. ". One strip, never more.
_TRACKNO = re.compile(r"^(?:\d{1,3}|[A-Da-d]\d{1,2})[.)]?[ _-]+")


def from_filename(p: Path) -> tuple:
    """
    Untagged files are the norm in a big messy collection.
    'Daft Punk - Around The World.flac' -> ('Daft Punk', 'Around The World')

    Same rules as the liquidsoap playlog fallback (proven in /tmp/liqtest2.liq,
    2026-07-29). The scanner and the player MUST agree on what a filename
    means -- they disagreeing is how artist="02. S.E.B" reached site search:
      1. underscores read as spaces
      2. strip ONE leading track number ('02. ', '04 - ', '03_', vinyl 'A2. ')
      3. split on the FIRST spaced ' - ' only
      4. fill the artist ONLY on that spaced separator -- a wrong artist is a
         wrong royalty attribution, so ambiguous names stay whole in the title
    """
    stem = _TRACKNO.sub("", p.stem.replace("_", " "), count=1).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return None, stem


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="folder to scan")
    ap.add_argument("--db", default="/var/lib/jukebox/library.db")
    ap.add_argument("--rights", required=True,
                    choices=["collecting_society", "v4v", "owned", "public_domain"],
                    help="What IS this folder? Be honest — it flows into the reports.")
    ap.add_argument("--no-hash", action="store_true",
                    help="skip sha256 (much faster; lose dedup + integrity)")
    a = ap.parse_args()

    root = Path(a.root)
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")

    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA)

    seen = {r[0]: (r[1], r[2]) for r in
            con.execute("SELECT path,size,mtime FROM tracks")}

    added = skipped = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in AUDIO:
            continue
        st = p.stat()
        key = str(p)
        if key in seen and seen[key] == (st.st_size, int(st.st_mtime)):
            skipped += 1
            continue

        t = tags(p)
        fn_artist, fn_title = from_filename(p)
        con.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key,
             None if a.no_hash else sha256(p),
             t.get("artist") or fn_artist or "Unknown Artist",
             t.get("title") or fn_title,        # untagged -> filename, still findable
             t.get("album"),
             t.get("isrc"),
             t.get("duration_s"),
             a.rights,
             st.st_size,
             int(st.st_mtime)),
        )
        added += 1
        if added % 200 == 0:
            con.commit()
            print(f"  indexed {added}...", flush=True)

    con.commit()

    total = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    no_isrc = con.execute(
        "SELECT COUNT(*) FROM tracks WHERE rights_class='collecting_society' "
        "AND (isrc IS NULL OR isrc='')").fetchone()[0]
    dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT sha256 FROM tracks WHERE sha256 IS NOT NULL "
        "GROUP BY sha256 HAVING COUNT(*)>1)").fetchone()[0]
    con.close()

    print(f"\nadded {added}, unchanged {skipped}, library now {total} tracks")
    if dupes:
        print(f"  {dupes} duplicate recordings (same sha256, different paths)")
    if no_isrc:
        print(f"  {no_isrc} society-repertoire tracks have NO ISRC "
              f"-> SENA reports will be incomplete for these")


if __name__ == "__main__":
    main()
