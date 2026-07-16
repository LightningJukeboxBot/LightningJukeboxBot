"""
resolvers/musicbrainz.py -- "is this even a real song?" existence check.

Free, keyless, no account. Sits between "not in local library" and "add to
wanted list" so garbage search input (typos, half-typed words) stops
polluting a list that's meant to be a genuine demand signal.

It never plays anything -- Availability.METADATA_ONLY, same as Spotify's
role in the chain. It only confirms MusicBrainz has heard of the thing.

MusicBrainz's anonymous-use policy requires a descriptive User-Agent and
asks for roughly 1 request/second -- the _throttle() call below enforces
that across threads (library_api.py runs a ThreadingHTTPServer).
"""

import threading
import time
from typing import Optional

import requests

from .base import Resolver, Track, RightsClass, Availability

API_URL = "https://musicbrainz.org/ws/2/recording/"
USER_AGENT = "NoderunnersRadioJukebox/0.1 (noderunners-radio@proton.me)"
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 5


class MusicBrainzResolver(Resolver):
    name = "musicbrainz"
    rights_class = RightsClass.UNKNOWN

    def __init__(self):
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _throttle(self):
        with self._lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _search_sync(self, query: str, limit: int) -> list[Track]:
        self._throttle()
        try:
            resp = requests.get(
                API_URL,
                params={"query": query.strip(), "fmt": "json", "limit": limit},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []            # network hiccup or MusicBrainz down -> treat as a miss, never crash

        tracks = []
        for rec in data.get("recordings", []):
            title = rec.get("title")
            if not title:
                continue
            artist = "Unknown"
            credit = rec.get("artist-credit")
            if credit:
                artist = credit[0].get("name", artist)
            tracks.append(Track(
                title=title,
                artist=artist,
                source="musicbrainz",
                source_uri=rec.get("id", ""),
                rights_class=RightsClass.UNKNOWN,
                availability=Availability.METADATA_ONLY,
                duration_s=(rec["length"] // 1000) if rec.get("length") else None,
            ))
        return tracks

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        import asyncio
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def resolve(self, track: Track) -> Optional[Track]:
        return track  # metadata-only; nothing further to confirm before showing it
