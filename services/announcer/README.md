# nr-announcer — the X announcer

Every 30 seconds it reads the station's own `/api/nowplaying`. When a **paid request** goes on air, it posts once to X:

> Now playing on NoderunnersRadio.com🎧 Artist — Title.
> *(plus `@Handle on Wavlake.` for a Wavlake track, and `With shoutout: “…”` when the listener left a message)*

Songs requested through the X jukebox are left alone here — `nr-xjukebox` quote-posts those instead.

## Also the shared text rules

The other bots import this program for its `clean()` (no links, no @mentions, no hashtags, no forged quotes, track
numbers stripped), its word list and X's weighted character count. One set of rules for every network.

## Files

- `nr-announcer` → `/usr/local/bin/`
- `nr-announcer.service` → `/etc/systemd/system/`
- `x.env.example` → copy to the path the unit names, fill in the four X developer keys. **Never commit the real file.**
- In its home folder (not in the repo): `blocklist.txt` (one word or phrase per line; `word*` catches compounds) and
  `handles.tsv` (artist name → verified X handle, tab-separated).

## Switches

- no `LIVE` file → **dry**: logs `[dry] would post`, posts nothing
- `LIVE` → posts
- `OFF` → idles

A withheld message and every posted message are reported to the operator's Telegram through the station's admin-notes
channel. An hourly cap, back-off on 402/403/429, and a dead-stop on 401 keep a bad day from becoming an expensive one.

```
nr-announcer --selftest
nr-announcer --check
nr-announcer --once
```
