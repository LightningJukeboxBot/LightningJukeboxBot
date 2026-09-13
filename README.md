# Lightning Jukebox Bot — Noderunners Radio

**A community radio station where the listeners pick the music, paid in sats.**

Listen, request a song, or just hang out: **[noderunnersradio.com](https://noderunnersradio.com)** ·
[Telegram](https://t.me/noderunnersradio) · [X @JukeboxLNDbot](https://x.com/JukeboxLNDbot)

![Lightning Jukebox Bot](assets/20230307-Bot-logo-new.jpg)

> Still being built, in the open. Things break sometimes. Use it at your own risk.

---

## What it does

- **Anyone can request a song.** Search the station's own music library, pay a small Lightning invoice, and your song
  joins the queue and plays on air.
- **21 sats is anti-spam, not a price for music.** It keeps the queue honest. The music itself is covered by the
  station's licence with the Dutch collecting society (BUMA/STEMRA), and every track played is logged for it.
- **Independent artists get paid directly.** Songs from [Wavlake](https://wavlake.com) cost 210 sats, and 206 of those go
  straight to the artist. Value for value.
- **Request from wherever you are:** the website, Telegram, X or Nostr. Every door leads to the same queue.
- **Live nights with guest DJs.** When a DJ is on, donations are split between the station and the crew on the decks.
- **When nobody requests anything,** the station plays its own library like a shuffled deck of cards: every track once
  before any repeats.

Listeners pay one invoice per song. There are no listener accounts or balances to top up.

---

## Four doors, one queue

| Where | How |
|---|---|
| **Website** | [noderunnersradio.com](https://noderunnersradio.com) → Jukebox tab → search → pay → it plays |
| **Telegram** | In the [Noderunners Radio group](https://t.me/noderunnersradio): `/play <song>`, `/play random`, `/now`, `/queue`, `/history` |
| **X** | Post `@JukeboxLNDbot play <song>`, optionally `\| a shoutout`. The bot replies once with a pay link. When your song airs, it quote-posts your request. |
| **Nostr** | Mention the Jukebox Lightning Bot with `play <song>`, reply to its list with a number, pay the invoice. When your song airs, it quotes your note. |

When a paid request goes on air, the station says so on X and Nostr:

> Now playing on NoderunnersRadio.com🎧 Artist — Title. With shoutout: “…”

---

## What is in this repository

| Folder | What it holds |
|---|---|
| [`src/`](src/) | The station API (`library_api.py`): search, requests, invoices, the queue, the play log, the admin console. The music library index and search. The Telegram chat relay and DJ registration. |
| [`services/`](services/) | The programs that run beside the station: the X jukebox, the X and Nostr announcers, the Nostr jukebox, and the rotation deck. Each folder has its own README. |
| [`bridge/`](bridge/) | Sends the broadcast picture and sound to the video stream. |
| [`web_assets/`](web_assets/), [`assets/`](assets/) | Images and web files. |

It runs on one self-hosted Linux box with [LNbits](https://github.com/lnbits/lnbits) in front of a Lightning node,
[Liquidsoap](https://www.liquidsoap.info/) for the audio, Icecast and [Owncast](https://owncast.online/) for the streams,
and Redis for the queue.

**Never in this repository:** keys, env files, databases, logs, play logs, or anything that says how to get into the box.
Keys live only in env files on the server. See [`services/README.md`](services/README.md).

### A note on the older code

The repository started life as a Telegram bot that remote-controlled a Spotify Premium account, with per-user wallets
inside the bot. That design is **retired**: the station now plays its own licensed library, and listeners pay per song
instead of keeping a balance in the bot. Some of that code (`src/jukeboxbot.py`, `spotifyhelper.py`, the `/stack` and `/fund` commands) is still in `src/` for
history, and is not what runs today.

---

## History and thanks

The Lightning Jukebox Bot was created by [@pieterjm](https://github.com/pieterjm), who wrote the original bot, its LNbits
integration and most of its early code. [@Herovk](https://github.com/Herovk) carried the first Nostr work. Thanks to everyone who tested it, broke it, and played their favourite tunes on it over the years.

Noderunners Radio is run by **SF** ([@artdesignbySF](https://github.com/artdesignbySF)) and the
[Noderunners](https://t.me/noderunnersfm) crew. Questions, ideas, bugs: tag us in the
[Noderunners Radio Telegram](https://t.me/noderunnersradio).

This would not exist without [LNbits](https://github.com/lnbits), [Wavlake](https://wavlake.com), and the open-source
audio stack it stands on.

---

## Licence and disclaimer

GNU General Public License v3 — see [`LICENSE`](LICENSE).

You are responsible for using this software, and any third-party software or music it interacts with, in line with the
licensing laws where you run it.
