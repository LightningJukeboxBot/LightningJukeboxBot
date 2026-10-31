# Noderunners Radio jukebox

The code behind the Lightning jukebox of [Noderunners Radio](https://noderunnersradio.com): listeners pay a few sats over Lightning to put a song on the air. It also runs the station's library search, the catalog ("Browse the ship"), listener sets, the "pepper" songs between the steady rotation, the play log for the music rights societies, the Telegram jukebox bot and the video feed's overlays.

This grew out of [LightningJukeboxBot](https://github.com/LightningJukeboxBot/LightningJukeboxBot), the Telegram + Spotify jukebox Pieter Meulenhoff started in 2023. See `NOTICE.md`.

## Support the station ⚡

Noderunners Radio is not a business. Listeners, DJs and friends keep the ship afloat. If you enjoy the station, or this code is useful to you, please send a tip to the station's tip jar:

- **Lightning Address:** `radio@lnbits.plebprojects.com` (works from any Lightning wallet)
- **Zap on Nostr:** the station is [`npub1ua6fxn9ktc4jncanf79jzvklgjftcdrt5etverwzzg0lgpmg3hsq2gh6v6`](https://njump.me/npub1ua6fxn9ktc4jncanf79jzvklgjftcdrt5etverwzzg0lgpmg3hsq2gh6v6); zaps land in the same tip jar
- **Where the sats go:** https://noderunnersradio.com/funding

A tip is a gift to the station, never a payment for music. Every sat helps keep the radio on the air.

## What is in here

| Folder | What it is |
| --- | --- |
| `src/library_api.py` | The station API: search, catalog, jukebox payments (LNbits), the request queue, listener sets, pepper, now playing, chat relay, the captain's console calls |
| `src/resolvers/` | Where a song comes from: the local library index, Wavlake (value-for-value), MusicBrainz |
| `src/liquidsoap_control.py` | Talks to Liquidsoap over its socket |
| `src/telegram_chat_relay.py` | Mirrors the public Telegram group onto the site (media through the API, the bot token stays on the server) |
| `src/dj_registration.py` | Self-service DJ onboarding (an LNbits wallet + a Lightning Address) |
| `src/buma_reports.py`, `src/playlog_*.py` | Play-log reports for the music rights societies |
| `src/jukeboxbot.py` and friends | The original 2023 Telegram + Spotify bot, kept for its history |
| `src/spotipy/` | A vendored copy of the spotipy library (MIT, see `NOTICE.md`) |
| `bot/nr-jukebox-bot.py` | Today's Telegram jukebox bot (library and Wavlake lanes, no custody) |
| `bridge/` | The video feed: ffmpeg + the overlay painters (card, chat, clock, ticker) |
| `liquidsoap/noderunners.liq` | The radio's Liquidsoap script: rotation, requests, the captain's queue, jingles, live DJs |
| `tg-stream/` | The Telegram live-stream relay |
| `tools/` | The station's helper programs: the X and Nostr announcers and jukebox bots (`nr-announcer`, `nr-nostr`, `nr-xjukebox`, `nr-nostr-jukebox`, `nr-radio`), the zap.stream listing and chat (`nr-nostr-live`, `nr-nostrchat`), the rotation deal (`nr-shuffle`, `nr-deck-resync`), pepper (`nr-pepper-deck`), station IDs (`nr-jingle`), loudness and file quality (`nr-gain-measure`, `nr-gain-annotate`, `nr-bitrate-measure`, `nr-lengthscan`), plus the full-text index build and a socket push |
| `web_assets/` | Images and scripts of the original bot's web pages |

## Before you run it

- **Paths are examples.** Every folder in this code (`/srv/jukebox/...`, `/srv/music_library...`, `/srv/music/free_music`) is a placeholder. Your library and your setup will have their own folders. The genre and label hints (`_GENRE_*`, `_LABEL_*` in `library_api.py`) hold made-up example folder names: put your own there.
- **Secrets never live in the code.** Tokens, wallet keys and passwords are read from environment files on the server (`/etc/*.env`, readable by root only, loaded by systemd `EnvironmentFile=`). None of them are in this repository.
- **No custody.** Payments go through LNbits; a paid Wavlake song is forwarded to the artist at once.

## Not in this repository

The station's environment files and keys, its databases and play logs, the music itself, the helpers that handle keys, backups, the home network or security updates, the chat's scam filter, the website (see [noderunnersradio-site](https://github.com/artdesignbySF/noderunnersradio-site)) and anything personal.

## Links

**The station**
- Website and player: https://noderunnersradio.com · request a song: https://noderunnersradio.com/play
- Telegram group and jukebox bot: https://t.me/noderunnersradio
- Live on Nostr: [zap.stream](https://zap.stream/naddr1qqgkummyv4e82mnwv4e8xttjv9jxjmczyq6cyfd8ctcrcxs0p9h3tawy3nggp68r9zc58zwmq9gk6gng8q6j7qcyqqq8vecwz2klw) · the station on Nostr: [`npub1ua6f…6v6`](https://njump.me/npub1ua6fxn9ktc4jncanf79jzvklgjftcdrt5etverwzzg0lgpmg3hsq2gh6v6)
- The jukebox on X: https://x.com/JukeboxLNDbot
- The website's code: https://github.com/artdesignbySF/noderunnersradio-site

**Friends and sister projects**
- [Noderunners](https://noderunners.network): the Dutch bitcoin-only community the station grew from
- [Pleb Projects](https://plebprojects.com): the home of our other projects and the tools we like
- [Bitcoin Fortune Cookie](https://bitcoinfortunecookie.com): crack a cookie for a few sats
- [Pleb Royale](https://plebroyale.com): art and design

## Licence

GNU General Public License v3.0, the licence of the original LightningJukeboxBot. See `LICENSE` and `NOTICE.md`.
