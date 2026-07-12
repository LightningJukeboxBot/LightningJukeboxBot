"""
resolvers/chain.py -- ordered sources. First playable hit wins. Misses never die.

    Local?       -> play (free)
    Wavlake?     -> play + zap the artist
    Purchasable? -> quote a price, the REQUESTER decides whether to buy it
    miss         -> wanted queue

A dead source must never break the chain. One try/except per resolver.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from .base import Resolver, Track, Availability


@dataclass
class Quote:
    """What the bot shows the requester."""
    track: Optional[Track]
    price_sats: int
    needs_funding: bool = False
    is_miss: bool = False
    votes: int = 0            # how many people have now asked for this

    def message(self) -> str:
        if self.is_miss:
            n = f" ({self.votes} people have asked for this)" if self.votes > 1 else ""
            return f"Not in the catalog and I can't buy it — added to the wanted list{n}."
        if self.needs_funding:
            t = self.track
            return (f"'{t.display()}' isn't in the catalog yet.\n"
                    f"{self.price_sats} sats buys it from {t.source} — it plays now "
                    f"and stays in the catalog for everyone, forever. Fund it?")
        return f"{self.track.display()} — {self.price_sats} sats"


class ResolverChain:
    def __init__(self, resolvers: list[Resolver], wanted, base_price: int = 21):
        self.resolvers = resolvers
        self.wanted = wanted
        self.base_price = base_price
        self.by_name = {r.name: r for r in resolvers}

    async def quote(self, query: str, requester: Optional[str] = None) -> Quote:
        cheapest_buy: Optional[Track] = None

        for r in self.resolvers:
            try:
                hits = await r.search(query, limit=3)
            except Exception as e:
                logging.warning("resolver %s failed on %r: %s", r.name, query, e)
                continue                      # one bad source must not kill the request

            for cand in hits:
                if cand.availability == Availability.PLAYABLE:
                    track = await r.resolve(cand)
                    if track:
                        return Quote(track=track, price_sats=self.base_price)

                if cand.availability == Availability.PURCHASABLE:
                    cost = cand.acquire_cost_sats or 0
                    if cheapest_buy is None or cost < (cheapest_buy.acquire_cost_sats or 0):
                        cheapest_buy = cand
                # METADATA_ONLY (Spotify) just enriches; it can never be aired.

        if cheapest_buy:
            return Quote(
                track=cheapest_buy,
                price_sats=self.base_price + (cheapest_buy.acquire_cost_sats or 0),
                needs_funding=True,
            )

        votes = await self.wanted.add(query, requester=requester)
        return Quote(track=None, price_sats=0, is_miss=True, votes=votes)

    async def fulfil(self, quote: Quote) -> Optional[Track]:
        """Called ONLY after the requester has actually paid."""
        if quote.is_miss or not quote.track:
            return None
        if not quote.needs_funding:
            return quote.track

        r = self.by_name.get(quote.track.source)
        if not r:
            return None

        acquired = await r.acquire(quote.track)
        if acquired:
            await self.wanted.fulfilled(quote.track.display())
        else:
            await self.wanted.add(quote.track.display(), note="purchase failed")
        return acquired
