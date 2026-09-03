"""
resolvers/wavlake.py -- Wavlake as a V4V catalog source.

Wavlake hosts independent artists paid value-for-value in sats. Their catalog
API is fully public and keyless for reads (search + track metadata + the CDN
mediaUrl), so we can find AND air Wavlake tracks today. Rebroadcast is
explicitly permitted by Wavlake "as long as listeners can directly pay the
artists in a V4V way" (confirmed by email from Josh @ Wavlake, 2026-07-19).

Two-speed design, on purpose:
  * search() / resolve()  -- keyless, work right now.
  * payout()              -- needs an `appId` from Wavlake's developer-splits
                             program (manual onboarding: contact@wavlake.com).
                             Until one is set (WAVLAKE_APP_ID env or constructor
                             arg) tracks still play; the artist zap is logged-
                             and-skipped rather than sent. The moment an appId
                             lands, payouts light up with no other code change.
                             The appId also credits Noderunners a 5% referring-
                             app split (artist keeps 90%, Wavlake 5%).

API: https://wavlake.com/api/v1  (docs: dev.wavlake.com; spec in
wavlake/developer-docs swagger.yaml)
  GET /content/search?term=...   -> [{id,name,type,artist,albumTitle,duration,...}]
  GET /content/track/{trackId}   -> {mediaUrl,artistNpub,msatTotal,duration,...}
  GET /lnurl?contentId=&appId=   -> {lnurl}          (appId REQUIRED)
"""

import asyncio
import logging
import os
from typing import Optional

import requests

from .base import Resolver, Track, RightsClass, Availability

API_BASE = "https://wavlake.com/api/v1"
USER_AGENT = "NoderunnersRadioJukebox/0.1 (noderunners-radio@proton.me)"
TIMEOUT_S = 8


class WavlakeResolver(Resolver):
    name = "wavlake"
    rights_class = RightsClass.V4V

    def __init__(self, app_id: str = "", pay_lnurl=None):
        # app_id: Wavlake developer-splits application id. Empty => payouts
        # dormant (tracks still play). Falls back to the WAVLAKE_APP_ID env var
        # so ops can switch it on without touching code.
        self.app_id = app_id or os.environ.get("WAVLAKE_APP_ID", "")
        # pay_lnurl: optional async callable(lnurl_str, sats) -> bool, injected
        # by whoever owns the LNbits wallet. Keeps LN payment OUT of the
        # resolver so this file stays a pure catalog adapter.
        self._pay_lnurl = pay_lnurl

    # ---- search (keyless) -------------------------------------------------
    # 2026-09-02: an ARTIST-name search (e.g. "Mystical Denck") makes Wavlake return
    # only the artist card -- no track hits at all -- and the old code dropped every
    # non-track hit, so the site said "Not in the library yet" (Josh @ Wavlake saw
    # the same one-artist answer with curl; there was no rate limiting anywhere).
    # Now artist and album hits are FOLLOWED into their track lists:
    #   artist hit -> GET /content/artist/{id} -> albums -> GET /content/album/{id} -> tracks
    #   album hit  -> GET /content/album/{id} -> tracks
    # Direct track hits keep first place; expanded tracks fill up to `limit`.
    # Bounded on purpose: a few artists/albums, a few album pages each, a short
    # per-call timeout and a HARD wall-clock budget: no call may start that cannot
    # finish inside the budget, so one uncached search stays under ~14 s worst case
    # (search 8 s + budget 6 s). Any failure inside the expansion is a miss, never a crash.
    # COST (honest): in the Wavlake lane (limit 21) a broad word like "bitcoin" now
    # costs up to ~1 + 2 + 9 calls the first time; the caches absorb every repeat.
    # POLITENESS (Josh, 2026-09-02: server-side volume may trip their rate limits):
    # a small in-process TTL cache -- search answers 5 min, artist/album pages
    # 30 min -- so the same question is never asked of Wavlake twice in a row.
    # v2 (after review): an answer produced while any page failed or the budget ran
    # out is NOT cached for 5 min (only 20 s), so a Wavlake hiccup can never pin
    # "nothing found" on an artist; the cache has a lock; one bad album item can
    # never discard the good ones; failures log at WARNING so they reach the journal.
    EXPAND_ARTISTS = 2          # artist hits followed
    EXPAND_ALBUMS_PER_ARTIST = 6
    EXPAND_ALBUM_HITS = 3       # album hits followed directly
    EXPAND_TIMEOUT_S = 4
    EXPAND_BUDGET_S = 6.0       # whole expansion, wall clock, hard
    CACHE_SEARCH_S = 300
    CACHE_SEARCH_INCOMPLETE_S = 20
    CACHE_PAGE_S = 1800
    CACHE_MAX = 400
    _cache: dict = {}           # key -> (expires_at, value)   (class-wide, all instances)
    _session = None             # one keep-alive HTTPS connection instead of a TLS handshake per call
    _lock = None                # guards _cache (created lazily below)

    @classmethod
    def _http(cls):
        if cls._session is None:
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
            cls._session = s
        return cls._session

    @classmethod
    def _guard(cls):
        if cls._lock is None:
            import threading as _th
            cls._lock = _th.Lock()
        return cls._lock

    def _cache_get(self, key: str):
        import time as _t
        with self._guard():
            hit = self._cache.get(key)
            if hit and hit[0] > _t.monotonic():
                return hit[1]
            if hit:
                self._cache.pop(key, None)
            return None

    def _cache_put(self, key: str, value, ttl: float):
        import time as _t
        with self._guard():
            if len(self._cache) >= self.CACHE_MAX:
                now = _t.monotonic()
                for k in [k for k, v in list(self._cache.items()) if v[0] <= now]:
                    self._cache.pop(k, None)
                if len(self._cache) >= self.CACHE_MAX:
                    # still full: drop the entries that expire soonest, keep the hot half
                    for k, _v in sorted(self._cache.items(), key=lambda kv: kv[1][0])[: self.CACHE_MAX // 2]:
                        self._cache.pop(k, None)
            self._cache[key] = (_t.monotonic() + ttl, value)

    def _get_json(self, path: str, timeout: float, cache_ttl: float = 0):
        key = "GET " + path
        if cache_ttl:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        resp = self._http().get(f"{API_BASE}{path}", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if cache_ttl:
            self._cache_put(key, data, cache_ttl)
        return data

    @staticmethod
    def _track_from(it, artist_hint: str = "", album_hint: str = "") -> Optional[Track]:
        if not isinstance(it, dict):
            return None
        title = it.get("name") or it.get("title")
        if not title:
            return None
        return Track(
            title=title,
            artist=it.get("artist") or artist_hint or "Unknown",
            source="wavlake",
            source_uri=it.get("id", ""),
            rights_class=RightsClass.V4V,
            availability=Availability.PLAYABLE,   # rebroadcast permitted
            duration_s=it.get("duration"),
            album=it.get("albumTitle") or album_hint or None,
        )

    def _album_tracks(self, album_id: str, artist_hint: str, deadline: float):
        """-> (tracks, ok). ok=False when the page could not be fetched or parsed."""
        import time as _t
        remaining = deadline - _t.monotonic()
        if not album_id or remaining < 1.0:
            return [], False
        try:
            d = self._get_json("/content/album/" + album_id, min(self.EXPAND_TIMEOUT_S, remaining), self.CACHE_PAGE_S)
            if isinstance(d, list):
                d = d[0] if d else {}
            if not isinstance(d, dict):
                return [], False
            album_title = d.get("title") or ""
            artist = d.get("artist") or artist_hint
            out = []
            for it in (d.get("tracks") or []):
                t = self._track_from(it, artist, album_title)
                if t:
                    out.append(t)
            return out, True
        except Exception as e:
            logging.warning("wavlake album expand failed for %s: %s", album_id, e)
            return [], False

    def _artist_albums(self, ar, deadline: float):
        """-> (album_jobs, ok) for one artist hit: its newest albums as (album_id, artist_name)."""
        import time as _t
        aid = ar.get("id", "") if isinstance(ar, dict) else ""
        remaining = deadline - _t.monotonic()
        if not aid or remaining < 1.0:
            return [], False
        try:
            d = self._get_json("/content/artist/" + aid, min(self.EXPAND_TIMEOUT_S, remaining), self.CACHE_PAGE_S)
            if isinstance(d, list):
                d = d[0] if d else {}
            if not isinstance(d, dict):
                return [], False
            name = d.get("name") or ar.get("name") or ""
            albums = [x for x in (d.get("albums") or []) if isinstance(x, dict)]
            # newest releases first, so a fresh drop is what a fan finds
            albums.sort(key=lambda x: str(x.get("releaseDate") or ""), reverse=True)
            return [(al.get("id", ""), name) for al in albums[: self.EXPAND_ALBUMS_PER_ARTIST]], True
        except Exception as e:
            logging.warning("wavlake artist expand failed for %s: %s", aid, e)
            return [], False

    def _expand(self, artist_hits: list, album_hits: list, need: int):
        """Tracks behind the artist/album hits -> (tracks, complete). Bounded by count and a
        hard wall clock; `complete` is False when any page failed or the budget ran out."""
        import time as _t
        from concurrent.futures import ThreadPoolExecutor
        deadline = _t.monotonic() + self.EXPAND_BUDGET_S
        found: list = []
        complete = True
        album_jobs: list = []                              # (album_id, artist_hint)
        for a in album_hits[: self.EXPAND_ALBUM_HITS]:
            if isinstance(a, dict) and a.get("id"):
                album_jobs.append((a.get("id", ""), a.get("artist") or ""))
        artists = [a for a in artist_hits[: self.EXPAND_ARTISTS] if isinstance(a, dict) and a.get("id")]
        with ThreadPoolExecutor(max_workers=3) as pool:
            if artists:
                # the (at most two) artist pages together, not one after the other
                for jobs, ok in pool.map(lambda ar: self._artist_albums(ar, deadline), artists):
                    album_jobs.extend(jobs)
                    complete = complete and ok
            seen = set()
            jobs = []
            for aid, hint in album_jobs:
                if aid and aid not in seen:
                    seen.add(aid)
                    jobs.append((aid, hint))
            # album pages in small batches of three (Wavlake's limiter punishes bursts), never more
            # than still needed, and never a batch that cannot finish inside the budget
            i = 0
            while i < len(jobs) and len(found) < need:
                if deadline - _t.monotonic() < 1.0:
                    complete = False
                    break
                batch = jobs[i:i + min(3, max(1, need - len(found)))]
                i += len(batch)
                for tracks, ok in pool.map(lambda j: self._album_tracks(j[0], j[1], deadline), batch):
                    found.extend(tracks)
                    complete = complete and ok
            if i < len(jobs) and len(found) < need:
                complete = False
        return found, complete

    def _search_sync(self, query: str, limit: int) -> list[Track]:
        q = query.strip()
        ckey = "SEARCH %d %s" % (limit, q.lower())
        import copy as _copy
        cached = self._cache_get(ckey)
        if cached is not None:
            # fresh objects every time: the queue's resolve() mutates Tracks in place
            return [_copy.deepcopy(t) for t in cached]
        try:
            resp = self._http().get(f"{API_BASE}/content/search", params={"term": q}, timeout=TIMEOUT_S)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.warning("wavlake search failed for %r: %s", query, e)
            return []                       # a miss, never a crash (and never cached)

        # SearchResponse is an array; be tolerant if they ever wrap it.
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("data", data.get("results", []))
        else:
            items = []
        items = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        tracks: list[Track] = []
        artist_hits, album_hits = [], []
        for it in items:
            kind = it.get("type")
            if kind == "artist":
                artist_hits.append(it)
                continue
            if kind == "album":
                album_hits.append(it)
                continue
            if kind != "track":
                continue
            t = self._track_from(it)
            if t:
                tracks.append(t)
            if len(tracks) >= limit:
                break

        complete = True
        if len(tracks) < limit and (artist_hits or album_hits):
            try:
                have = {t.source_uri for t in tracks}
                more, complete = self._expand(artist_hits, album_hits, limit - len(tracks))
                for t in more:
                    if t.source_uri and t.source_uri in have:
                        continue
                    have.add(t.source_uri)
                    tracks.append(t)
                    if len(tracks) >= limit:
                        break
            except Exception as e:
                logging.warning("wavlake expand failed for %r: %s", query, e)
                complete = False
        try:
            self._cache_put(ckey, [_copy.deepcopy(t) for t in tracks],
                            self.CACHE_SEARCH_S if complete else self.CACHE_SEARCH_INCOMPLETE_S)
        except Exception:
            pass
        return tracks

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        return await asyncio.to_thread(self._search_sync, query, limit)

    # ---- resolve: fetch the airable mediaUrl + payout metadata -----------
    def _resolve_sync(self, track: Track) -> Optional[Track]:
        if not track.source_uri:
            return None
        try:
            resp = requests.get(
                f"{API_BASE}/content/track/{track.source_uri}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as e:
            logging.warning("wavlake resolve failed for %s: %s", track.source_uri, e)
            return None

        # /content/track/{id} returns a single-element LIST in practice (the
        # swagger spec says object, but the live API disagrees). Unwrap it.
        if isinstance(d, list):
            d = d[0] if d else {}
        if not isinstance(d, dict):
            return None

        media = d.get("mediaUrl")
        if not media:
            return None                     # nothing airable -> treat as slipped away

        track.title = d.get("title", track.title)
        track.artist = d.get("artist", track.artist)
        track.album = d.get("albumTitle", track.album)
        track.duration_s = d.get("duration", track.duration_s)
        track.availability = Availability.PLAYABLE
        # Remote CDN URL. The queueing layer decides stream-through vs download;
        # Josh asked for on-demand fetch, NOT bulk-mirroring the catalog.
        track.extra["media_url"] = media
        track.extra["artist_npub"] = d.get("artistNpub")
        track.extra["msat_total"] = d.get("msatTotal")
        return track

    async def resolve(self, track: Track) -> Optional[Track]:
        return await asyncio.to_thread(self._resolve_sync, track)

    # ---- V4V payout (needs appId) ----------------------------------------
    def _lnurl_sync(self, content_id: str) -> Optional[str]:
        if not self.app_id:
            return None
        try:
            resp = requests.get(
                f"{API_BASE}/lnurl",
                params={"contentId": content_id, "appId": self.app_id},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            return resp.json().get("lnurl")
        except Exception as e:
            logging.warning("wavlake lnurl fetch failed for %s: %s", content_id, e)
            return None

    async def payout(self, track: Track, sats: int) -> bool:
        """Zap the artist via Wavlake's LNURL-pay. Dormant (logged) until appId set."""
        if not self.app_id:
            logging.info(
                "wavlake payout skipped (no appId yet): would zap %d sats to %s",
                sats, track.display(),
            )
            return False
        lnurl = await asyncio.to_thread(self._lnurl_sync, track.source_uri)
        if not lnurl:
            return False
        if self._pay_lnurl is None:
            logging.warning("wavlake: appId set but no LN payer injected; lnurl=%s", lnurl)
            return False
        return bool(await self._pay_lnurl(lnurl, sats))
