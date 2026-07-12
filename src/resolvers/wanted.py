"""
resolvers/wanted.py -- the queue that means the bot never says "no".

Every request that nothing else could serve lands here, with a count of how many
people asked. That count IS the acquisition priority: the crowd tells you what to
buy next. Demand-driven library growth, no guessing.

Redis-backed (the bot already runs Redis) so it survives restarts.
"""

import json
import time
from typing import Optional

try:
    import settings                    # the bot's Redis handle
    _rds = settings.rds
except Exception:                      # standalone / test mode
    _rds = None

KEY = "wanted"            # hash: query -> json blob
KEY_VOTES = "wanted:votes"  # sorted set: query -> times requested


class WantedQueue:
    def __init__(self, rds=None):
        self.rds = rds or _rds
        self._mem: dict = {}           # fallback when no Redis (tests)

    async def add(self, query: str, note: str = "", requester: Optional[str] = None) -> int:
        """Record a miss. Returns how many times it's now been asked for."""
        q = query.strip()
        if not q:
            return 0

        if self.rds is None:
            e = self._mem.setdefault(q, {"query": q, "votes": 0, "note": note})
            e["votes"] += 1
            return e["votes"]

        votes = int(self.rds.zincrby(KEY_VOTES, 1, q))
        self.rds.hset(KEY, q, json.dumps({
            "query": q,
            "votes": votes,
            "note": note,
            "last_requester": requester,
            "last_seen": int(time.time()),
        }))
        return votes

    async def top(self, n: int = 20) -> list[dict]:
        """Most-wanted first. This is your shopping list."""
        if self.rds is None:
            return sorted(self._mem.values(), key=lambda e: -e["votes"])[:n]

        out = []
        for q, votes in self.rds.zrevrange(KEY_VOTES, 0, n - 1, withscores=True):
            q = q.decode() if isinstance(q, bytes) else q
            raw = self.rds.hget(KEY, q)
            d = json.loads(raw) if raw else {"query": q}
            d["votes"] = int(votes)
            out.append(d)
        return out

    async def fulfilled(self, query: str) -> None:
        """Call once it's actually in the catalog. Removes it from the list."""
        q = query.strip()
        if self.rds is None:
            self._mem.pop(q, None)
            return
        self.rds.zrem(KEY_VOTES, q)
        self.rds.hdel(KEY, q)
