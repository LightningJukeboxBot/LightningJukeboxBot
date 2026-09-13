# services — the programs that run beside the station

Noderunners Radio is more than the Telegram bot in `src/`. These programs run on the same box as the station API
(`src/library_api.py`) and talk to it over localhost. Each one is a single self-contained file with its manual at the top
and a built-in `--selftest` that never touches the network or the live station.

| Folder | Program | What it does |
|---|---|---|
| [`x-jukebox/`](x-jukebox/) | `nr-xjukebox` | `@JukeboxLNDbot play <song>` on X → one reply with a one-time pay link. When the paid song goes on air, it quote-posts the listener's request. Runs on a purse: X requests pay for X's own costs. |
| [`announcer/`](announcer/) | `nr-announcer` | Posts every paid request to X as it goes on air: *"Now playing on NoderunnersRadio.com🎧 Artist — Title."* Also holds the shared text rules (cleaning, word list) the other bots import. |
| [`nostr/`](nostr/) | `nr-nostr`, `nr-nostr-jukebox` | The same two jobs on Nostr: an on-air announcer, and a jukebox you mention to search, pick a number, pay an invoice, and get quoted when your song airs. |
| [`deck/`](deck/) | `nr-shuffle`, `nr-deck-resync` | The background rotation. Deals the library like a deck of cards so every track plays once before any repeats, and a guard that keeps the deck in step after a restart. |

## Installing one

1. Copy the program to `/usr/local/bin/` and make it executable.
2. Copy its `.service` (and `.timer`, if any) to `/etc/systemd/system/`, then `systemctl daemon-reload`.
3. Put the keys in the env file the unit names (see the `*.env.example` beside it). **Never commit a real env file.**
4. Run `<program> --selftest`, then `<program> --check`.
5. `systemctl enable --now <unit>`. The bots start in **dry/practice mode**: they read and log what they *would* do and
   post nothing until a `LIVE` file exists in their home folder. An `OFF` file stops them without a restart.

## What is never in this repository

Keys, env files, databases, logs, play logs, recovery notes, and anything that says how to get into the box. The unit
files name *where* an env file lives, never what is in it.

Licensed with the rest of the repository under the GNU GPL v3.
