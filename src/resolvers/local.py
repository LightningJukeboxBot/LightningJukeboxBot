"""
resolvers/local.py -- your own music, on your own disk. Free. Always tried first.

Backed by a SQLite index built by scan_library.py, so search is instant and we
never walk 7 TB at request time.

Note the sha256 column: same discipline as the Pictures project. It gives us
dedup across a messy collection AND proves a file is intact before it airs.

SEARCH is deliberately two-tier (loosened 2026-08-11 after "direstraits" found
nothing):
  1. FAST path -- every word in the query must appear somewhere in
     artist+title+album, in ANY order, matched at C speed. Sub-second, and it
     answers the overwhelming majority of searches.
  2. LOOSE fallback -- runs ONLY when the fast path finds nothing. It strips
     spaces and punctuation from BOTH sides, so "direstraits" finds "Dire
     Straits" and "acdc" finds "AC/DC". That normalisation is a full scan
     (~1-2s), which is why it is a fallback, not the default.
"""

import asyncio
import sqlite3
from pathlib import Path
from typing import Optional

from .base import Resolver, Track, RightsClass, Availability

DEFAULT_DB = "/var/lib/jukebox/library.db"

# Characters folded away for the loose fallback so spacing/punctuation stop
# mattering. Keep this in sync between _loose() (the query side) and _loose_sql()
# (the row side) or the two normalisations diverge and matches are missed.
_STRIP = " -'./,&!?:;\"_()[]"


def _loose(s: str) -> str:
    """Query-side normaliser: lowercase, then drop every _STRIP character."""
    s = (s or "").lower()
    for ch in _STRIP:
        s = s.replace(ch, "")
    return s


def _loose_sql(expr: str) -> str:
    """Row-side normaliser as a nested REPLACE() chain (runs in C, in SQLite).
    Mirrors _loose() exactly. No user input goes in here -- only fixed chars."""
    for ch in _STRIP:
        lit = "''''" if ch == "'" else "'" + ch + "'"
        expr = "replace(%s, %s, '')" % (expr, lit)
    return expr


def _trigrams(s: str) -> list:
    """The 3-character shingles of a normalised string. A one-letter typo still
    shares most of them, which is what makes the fuzzy tier tolerant."""
    s = _loose(s)
    if len(s) < 3:
        return [s] if s else []
    return sorted(set(s[i:i + 3] for i in range(len(s) - 2)))


DUR_TOL_S = 3          # two rows within 3 s of each other are one recording


def _dur(r):
    """Length in whole seconds, or None when the index does not know."""
    try:
        d = int(r["duration_s"] if "duration_s" in r.keys() else 0)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def _dedupe(rows, limit: int):
    """Collapse duplicate PRESSINGS, keep distinct VERSIONS.

    History worth keeping: 2026-08-13 SF asked for variety -- '/play dandy'
    showed Southern Dandy thrice -- so this keyed on loose artist+title alone.
    That went too far. On 2026-08-19 SF searched Dire Straits 'Lions', got ONE
    row, and heard a take he did not know; Ceephax 'Marshmellow' proved it --
    three files on the disks, one row in the search. Hiding the other rows also
    CHOSE for him, because the play path queues whichever row survived.

    So: same artist+title rows are clustered by LENGTH. A row joins an existing
    cluster (and is dropped) only when its duration is within DUR_TOL_S of one
    already kept -- three rips of one master differ by a second or two and still
    collapse, while a live take, a remix, a 12" or an edit stands on its own row
    with its album and length beside it. Rows with no duration collapse on name
    alone, as before. First hit wins -- each tier already orders by relevance."""
    kept, out = {}, []
    for r in rows:
        name = (_loose(r["artist"] or ""), _loose(r["title"] or ""))
        d = _dur(r)
        lens = kept.setdefault(name, [])
        if d is None:
            if any(x is None for x in lens):
                continue
        elif any(x is not None and abs(x - d) <= DUR_TOL_S for x in lens):
            continue
        lens.append(d)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _overfetch(limit: int) -> int:
    """Ask SQL for more rows than we show, so deduping still fills the page.
    Wider since 2026-08-19: with versions kept apart, one popular song can now
    hold several rows of the page by itself.

    nr_offset (2026-09-10, SF: "I want to be able to next and previous scroll through them all"):
    the ceiling was 120, which is what actually made the deep rows unreachable. It costs almost
    nothing to lift: measured on the live 683,530-row catalogue, the same query takes 0.45 s at 120
    rows, 0.48 s at 400 and 0.37 s for all 482 matches. The leading-wildcard LIKE scans the whole
    table whatever the limit, so the scan is the entire bill and it is already being paid. The floor
    and the x8 shape are untouched, so an ordinary page takes exactly the path it always did."""
    return min(max(limit * 8, 40), 1200)


def _fts_ready(con: sqlite3.Connection) -> bool:
    """Is the optional trigram index present? Built by nr-build-fts.py. When it
    is missing the fuzzy tier is simply skipped -- search still works."""
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks_fts'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


class LocalResolver(Resolver):
    name = "local"
    # NOTE: owning a file is not the same as having the right to broadcast it.
    # A ripped-from-CD track is still society repertoire. scan_library.py lets
    # you tag folders, so the truth lives in the index, not in an assumption.
    rights_class = RightsClass.UNKNOWN

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path

    def _connect(self) -> Optional[sqlite3.Connection]:
        if not Path(self.db_path).exists():
            return None
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _row_to_track(self, r: sqlite3.Row) -> Track:
        return Track(
            title=r["title"],
            artist=r["artist"],
            source="local",
            source_uri=r["path"],
            local_path=r["path"],
            rights_class=RightsClass(r["rights_class"]),
            availability=Availability.PLAYABLE,
            duration_s=r["duration_s"],
            isrc=r["isrc"],
            album=r["album"],
            sha256=r["sha256"],
        )

    def _search_sync(self, query: str, limit: int, offset: int = 0) -> list[Track]:
        # nr_offset: the skip is applied AFTER the dedupe, on the deduped list -- so a page boundary
        # can never land inside a collapsed cluster, and page 5 is the same page 5 every time. "want"
        # is what we must produce BEFORE skipping; offset 0 leaves every number exactly as it was.
        # The SQL LIMIT saturates at _overfetch's 1200, so a very deep offset simply runs dry and
        # the caller is told "no more" -- short, never wrong.
        offset = max(0, int(offset or 0))
        want = limit + offset
        con = self._connect()
        if con is None:
            return []          # no index yet -> a miss, not a crash
        try:
            raw = query.strip().lower()
            toks = [t for t in raw.split() if t]
            if not toks:
                return []
            # COALESCE every field: in SQLite `x || NULL` is NULL, so without
            # this a track with a blank album (or artist/title) would produce a
            # NULL blob and match nothing at all -- it would vanish from search
            # AND from the play re-lookup. Empty string keeps the rest of the row.
            blob = ("lower(coalesce(artist,'') || ' ' || coalesce(title,'')"
                    " || ' ' || coalesce(album,''))")

            # 1. FAST: every word must appear (any order), on the raw blob.
            where = " AND ".join([blob + " LIKE ?"] * len(toks))
            # nr_rank (2026-09-10, SF: "jukebox won't serve it"): the blob above matches the
            # ALBUM as well as the artist and the title, and the only relevance rule used to be
            # an exact "artist - title" equality against the whole typed string -- which never
            # fires for anything a person actually types. So every real search came back
            # ALPHABETICALLY, and a twelve-track compilation whose ALBUM carried the words buried
            # the song itself below all twelve. Tiers now, strongest signal first:
            #   0  the whole query IS "artist - title"   (the old rule, kept)
            #   1  the query is BOTH the artist and the title (an eponymous song)
            #   2  the query is the ARTIST -- so "queen" gives you Queen, not three cover bands
            #      who happen to have a song called Queen. Artist BEFORE title: an artist name is
            #      the commonest thing anyone types, and ranking a title above it was a
            #      regression this fix introduced and a review caught (2026-09-10).
            #   3  the query is the TITLE
            #   4  every word is in artist+title -- the NAME, not merely the album
            # then alphabetical as before, so ties break exactly as they always did.
            nameblob = "lower(coalesce(artist,'') || ' ' || coalesce(title,''))"
            in_name = " AND ".join([nameblob + " LIKE ?"] * len(toks))
            sql = (
                "SELECT * FROM tracks WHERE " + where +
                " ORDER BY"
                " CASE WHEN lower(coalesce(artist,'') || ' - ' ||"
                " coalesce(title,'')) = ? THEN 0 ELSE 1 END,"
                " CASE WHEN lower(coalesce(artist,'')) = ? AND"
                " lower(coalesce(title,'')) = ? THEN 0"
                " WHEN lower(coalesce(artist,'')) = ? THEN 1"
                " WHEN lower(coalesce(title,'')) = ? THEN 2 ELSE 3 END,"
                " CASE WHEN " + in_name + " THEN 0 ELSE 1 END,"
                " artist, title"
                " LIMIT ?"
            )
            like = ["%" + t + "%" for t in toks]
            rows = con.execute(
                sql, like + [raw] * 5 + like + [_overfetch(want)]
            ).fetchall()
            if rows:
                return [self._row_to_track(r) for r in _dedupe(rows, want)[offset:]]

            # 2. LOOSE fallback -- only on a total miss. Space/punct-insensitive.
            ntoks = [_loose(t) for t in toks if _loose(t)]
            if not ntoks:
                return []
            nblob = _loose_sql(blob)
            where = " AND ".join([nblob + " LIKE ?"] * len(ntoks))
            sql = (
                "SELECT * FROM tracks WHERE " + where +
                " ORDER BY artist, title LIMIT ?"
            )
            rows = con.execute(
                sql, ["%" + t + "%" for t in ntoks] + [_overfetch(want)]
            ).fetchall()
            if rows:
                return [self._row_to_track(r) for r in _dedupe(rows, want)[offset:]]

            # 3. FUZZY fallback -- typo tolerance via the trigram FTS index, if
            # present. Reached only on a total miss, so a near-match beats an
            # empty result. bm25 ranks the best trigram overlap first. The play
            # path still demands an EXACT title match, so a fuzzy near-miss can
            # never cause a wrong charge.
            if _fts_ready(con):
                tg = _trigrams(raw)
                if tg:
                    match = " OR ".join('"' + t + '"' for t in tg)
                    try:
                        rows = con.execute(
                            "SELECT t.* FROM tracks_fts f"
                            " JOIN tracks t ON t.rowid = f.rowid"
                            " WHERE f.tracks_fts MATCH ?"
                            " ORDER BY bm25(tracks_fts) LIMIT ?",
                            (match, _overfetch(want)),
                        ).fetchall()
                        return [self._row_to_track(r) for r in _dedupe(rows, want)[offset:]]
                    except sqlite3.OperationalError:
                        pass
            return []
        finally:
            con.close()

    async def search(self, query: str, limit: int = 5, offset: int = 0) -> list[Track]:
        # SQLite is sync; keep the event loop free
        return await asyncio.to_thread(self._search_sync, query, limit, offset)

    async def resolve(self, track: Track) -> Optional[Track]:
        """Confirm the file is still actually on disk before we promise to air it."""
        if not track.local_path or not Path(track.local_path).exists():
            return None
        track.availability = Availability.PLAYABLE
        return track
