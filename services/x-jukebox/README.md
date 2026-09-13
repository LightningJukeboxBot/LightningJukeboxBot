# nr-xjukebox — the X jukebox

A listener posts `@JukeboxLNDbot play <song> | <message>`. The bot asks the station for a one-time pay code and answers
**once** with a link to the station's own pay page, where they pick the song and pay. When that song goes **on air**, the
bot quote-posts the listener's original request:

> Now playing on NoderunnersRadio.com🎧 Artist — Title. With shoutout: “…”

## The rule it is built around

**X requests pay for X's own costs.** Every billed call to X is written to an append-only ledger *before* the call is
made, the purse is replayed from that ledger at every start, and the price of a song is set once a day from X's prices
and the bitcoin rate. When the purse cannot cover the next action, the lane goes quiet and the operator is told once.
Daily and monthly caps sit on top.

## Files

- `nr-xjukebox` → `/usr/local/bin/`
- `nr-xjukebox.service` → `/etc/systemd/system/`
- Keys: the same env file as the announcer (`../announcer/x.env.example`).

It imports the announcer's text rules, so `nr-announcer` must be installed beside it.

## Switches (in its home folder, no restart needed)

- no `LIVE` file → **practice mode**: reads, runs every check, logs `[practice] would reply`, posts and mints nothing
- `LIVE` → the real thing
- `OFF` → nothing runs, nothing is spent

## Run

```
nr-xjukebox --selftest   # fakes only, nothing leaves the box
nr-xjukebox --check      # state and settings, spends nothing
nr-xjukebox --once       # one cycle
```

The full manual, including the purse files and the Telegram alerts, is the docstring at the top of the program.
