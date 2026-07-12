"""
resolvers/local.py -- your own music, on your own disk. Free. Always tried first.

Backed by a SQLite index built by scan_library.py, so search is instant and we
never walk 7 TB at request time.

Note the sha256 column: same discipline as the Pictures project. It gives us
dedup across a messy collection AND proves a file is intact before it airs.
"""

import asyncio
import sqlite3
from pathlib import Path
from typing import Optional

from .base import Resolver, Track, RightsClass, Availability

DEFAULT_DB = "/var/lib/jukebox/library.db"


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

    def _search_sync(self, query: str, limit: int) -> list[Track]:
        con = self._connect()
        if con is None:
            return []          # no index yet -> a miss, not a crash
        try:
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM tracks
                WHERE lower(artist) LIKE ?
                   OR lower(title)  LIKE ?
                   OR lower(album)  LIKE ?
                   OR lower(artist || ' ' || title) LIKE ?
                ORDER BY
                   CASE WHEN lower(artist || ' - ' || title) = ? THEN 0 ELSE 1 END,
                   artist, title
                LIMIT ?
                """,
                (like, like, like, like, query.strip().lower(), limit),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]
        finally:
            con.close()

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        # SQLite is sync; keep the event loop free
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def resolve(self, track: Track) -> Optional[Track]:
        """Confirm the file is still actually on disk before we promise to air it."""
        if not track.local_path or not Path(track.local_path).exists():
            return None
        track.availability = Availability.PLAYABLE
        return track
