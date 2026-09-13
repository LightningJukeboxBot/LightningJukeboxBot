# The deck — the background rotation

When nobody has requested anything, the station plays its own library. It does that like a **deck of cards**:

- `rotation-full.m3u` — the pool: every track allowed in the rotation.
- `deck.m3u` — the pool, shuffled once. It does not change until every card has been played.
- `deck.cursor` — how far through the deck the station has actually got (counted from the play log, not guessed).
- `rotation.m3u` — a small window of the next cards, written where Liquidsoap reads.

So every track plays **exactly once** before any track repeats. Only then is a new deck shuffled.

## nr-shuffle

Run by its timer. Counts what actually aired since the last run, moves the cursor, and deals the next window. Takes a
file lock (`.deck.lock`) so it never collides with an edit from the admin console, refuses to reshuffle without that
lock, and never skips a chunk of the deck on doubtful evidence.

## nr-deck-resync

A guard, run by its timer. After a restart of the audio engine it checks that the window on air is still in step with
the deck and re-deals if it is not, so a restart never replays or skips music.

## Files

- `nr-shuffle`, `nr-deck-resync` → `/usr/local/bin/`
- `nr-shuffle.service` + `.timer`, `nr-deck-resync.service` + `.timer` → `/etc/systemd/system/`

All deck files live in `/var/lib/jukebox/` and **must be owned by the user the station runs as**. A script run with
sudo that rewrites one of them must hand it back, or the admin console silently loses the ability to add or remove
tracks.
