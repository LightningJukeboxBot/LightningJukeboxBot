#!/usr/bin/env python3
"""
try_chain.py -- prove the resolver chain works, WITHOUT touching the live bot.

    python3 try_chain.py "daft punk"
    python3 try_chain.py "some song nobody has"
    python3 try_chain.py --wanted          # show the shopping list

Uses a fake purchasable source so you can see the buy-on-demand flow end to end
before any real Bandcamp/Wavlake code exists. Nothing here talks to Telegram,
Lightning, or Redis. It cannot break anything.
"""

import argparse
import asyncio

from resolvers.base import Resolver, Track, RightsClass, Availability
from resolvers.local import LocalResolver
from resolvers.wanted import WantedQueue
from resolvers.chain import ResolverChain


class FakeShop(Resolver):
    """Stand-in for Bandcamp/Wavlake until the real ones exist."""
    name = "fakeshop"
    rights_class = RightsClass.OWNED

    CATALOG = {
        "brian eno music for airports": 3000,
        "aphex twin selected ambient works": 2500,
    }

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        q = query.strip().lower()
        out = []
        for name, cost in self.CATALOG.items():
            if q in name or name in q:
                artist, _, title = name.partition(" ")
                out.append(Track(
                    title=title.title() or name,
                    artist=artist.title(),
                    source=self.name,
                    source_uri=f"fakeshop://{name}",
                    rights_class=RightsClass.OWNED,
                    availability=Availability.PURCHASABLE,
                    acquire_cost_sats=cost,
                ))
        return out[:limit]

    async def resolve(self, t): return t

    async def acquire(self, t: Track):
        print(f"    [fakeshop] pretending to buy {t.display()} for "
              f"{t.acquire_cost_sats} sats...")
        t.availability = Availability.PLAYABLE
        t.local_path = f"/var/lib/jukebox/music/{t.key()}.flac"
        return t


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--db", default="/var/lib/jukebox/library.db")
    ap.add_argument("--wanted", action="store_true", help="show the shopping list")
    ap.add_argument("--fund", action="store_true", help="simulate the requester paying")
    a = ap.parse_args()

    wanted = WantedQueue()
    if wanted.rds is None:
        print("  [no Redis — wanted list is in-memory and won't survive this process]")
    chain = ResolverChain(
        resolvers=[LocalResolver(a.db), FakeShop()],   # order = priority
        wanted=wanted,
        base_price=21,
    )

    if a.wanted:
        rows = await wanted.top()
        if not rows:
            print("wanted list is empty")
        for r in rows:
            print(f"  {r['votes']:>3} x  {r['query']}")
        return

    if not a.query:
        ap.error("give me something to search for")

    q = await chain.quote(a.query, requester="testuser")
    print(f"\n  {q.message()}\n")

    if q.needs_funding and a.fund:
        t = await chain.fulfil(q)
        print(f"    -> acquired: {t.display()}  ({t.rights_class.value})")
        print(f"    -> now free forever for everyone\n")


if __name__ == "__main__":
    asyncio.run(main())
