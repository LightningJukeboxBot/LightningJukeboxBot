#!/usr/bin/env python3
"""
Build the trigram fuzzy-search index (tracks_fts) inside library.db.

    sudo python3 nr-build-fts.py

WHY: the search falls back to this index only when the exact and loose passes
find nothing, so a typo like "diretraits" still finds Dire Straits. Without the
index, search still works -- it just loses typo tolerance.

RE-RUN this after any rebuild of library.db (the filter/reindex step drops the
table with the rest of the database). No service restart is needed: the search
opens a fresh connection per query and picks the index up on the next search.
"""
import os
import sqlite3
import sys
import time

DB = "/var/lib/jukebox/library.db"

# Must match the blob used in resolvers/local.py exactly, COALESCE included, or
# the fuzzy tier ranks against a different string than the other tiers search.
BLOB = ("lower(coalesce(artist,'') || ' ' || coalesce(title,'')"
        " || ' ' || coalesce(album,''))")


def main() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sys.exit("run me as root:  sudo python3 " + sys.argv[0])
    if not os.path.exists(DB):
        sys.exit("no database at " + DB)

    con = sqlite3.connect(DB)

    # Prove this SQLite build actually has the FTS5 trigram tokenizer before we
    # touch the real table, so a missing feature fails loud and changes nothing.
    try:
        con.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x, tokenize='trigram')")
        con.execute("DROP TABLE _fts_probe")
    except sqlite3.OperationalError as e:
        con.close()
        sys.exit("this SQLite has no FTS5 trigram tokenizer: %s" % e)

    n_src = con.execute("SELECT count(*) FROM tracks").fetchone()[0]
    print("indexing %d tracks ..." % n_src)

    t0 = time.time()
    con.execute("DROP TABLE IF EXISTS tracks_fts")
    con.execute(
        "CREATE VIRTUAL TABLE tracks_fts USING fts5(blob, tokenize='trigram', content='')"
    )
    con.execute(
        "INSERT INTO tracks_fts(rowid, blob) SELECT rowid, %s FROM tracks" % BLOB
    )
    con.commit()
    n = con.execute("SELECT count(*) FROM tracks_fts").fetchone()[0]
    con.close()

    print("built tracks_fts: %d rows in %.1fs" % (n, time.time() - t0))
    print("fuzzy search is now live -- no restart needed.")


if __name__ == "__main__":
    main()
