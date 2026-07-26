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
    def _search_sync(self, query: str, limit: int) -> list[Track]:
        try:
            resp = requests.get(
                f"{API_BASE}/content/search",
                params={"term": query.strip()},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.warning("wavlake search failed for %r: %s", query, e)
            return []                       # a miss, never a crash

        # SearchResponse is an array; be tolerant if they ever wrap it.
        items = data if isinstance(data, list) else data.get("data", data.get("results", []))
        tracks: list[Track] = []
        for it in items:
            if it.get("type") != "track":
                continue                    # skip artist/album hits; we air tracks
            title = it.get("name") or it.get("title")
            if not title:
                continue
            tracks.append(Track(
                title=title,
                artist=it.get("artist") or "Unknown",
                source="wavlake",
                source_uri=it.get("id", ""),
                rights_class=RightsClass.V4V,
                availability=Availability.PLAYABLE,   # rebroadcast permitted
                duration_s=it.get("duration"),
                album=it.get("albumTitle"),
            ))
            if len(tracks) >= limit:
                break
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
