#!/usr/bin/env python3
"""
queue_track.py -- admin/test tool: queue local tracks onto the live jukebox
WITHOUT payment. Testing + manual ops only (the public path is pay-to-play).

Must run as a user in the `radio` group (e.g. sf) so it can reach the
liquidsoap control socket. For each query it searches the local library index,
queues the best match, and annotates it with the track's rights_class/source —
so a test play still lands in the correct royalty pool in the play-log, exactly
like a paid request.

    cd /home/sf/LightningJukeboxBot/src
    ../venv/bin/python3 queue_track.py "air playground love" "simple things zero 7"
"""

import asyncio
import sys

from resolvers.local import LocalResolver
from liquidsoap_control import push_track

DB = "/var/lib/jukebox/library.db"


async def main(queries):
    local = LocalResolver(DB)
    for q in queries:
        hits = await local.search(q, limit=1)
        if not hits:
            print(f"  MISS   '{q}' -- not in library")
            continue
        track = await local.resolve(hits[0])   # confirm the file is still on disk
        if not track:
            print(f"  GONE   '{q}' -> {hits[0].display()} (file missing on disk)")
            continue
        rid = push_track(
            track.local_path,
            rights_class=track.rights_class.value,
            source=track.source,
        )
        print(f"  QUEUED '{q}' -> {track.display()}  [rights={track.rights_class.value}]  rid={(rid or '').strip()}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: queue_track.py "search one" "search two" ...')
    asyncio.run(main(sys.argv[1:]))
