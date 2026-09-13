# Nostr — the announcer and the jukebox

Both speak with the same key, have no third-party dependencies (signing, bech32 and the WebSocket client are in the
file), and verify every incoming event's id and BIP-340 signature before looking at it.

## nr-nostr — the Nostr announcer

When a paid request goes on air, it publishes one kind-1 note to the relays in `relays.txt`:

> Now playing on NoderunnersRadio.com🎧 Artist — Title.

A Wavlake artist with a known npub is mentioned and p-tagged. A failed publish is retried with the **same** event id, so
relays dedupe it. Songs requested through the Nostr jukebox are left to `nr-nostr-jukebox`.

## nr-nostr-jukebox — the Nostr jukebox

Mention the bot:

- `play <artist or song>` → a numbered list (library first, the V4V lane when enabled)
- reply to that list with a **number** → a Lightning invoice as a reply (Nostr apps show a Pay button)
- pay it → *"Paid and queued"*
- when the song goes **on air** → a quote note of your own `play` note (NIP-18 `q` tag + `nostr:note1…`), you p-tagged
- `queue`, `now`, `random`, anything else → help, once an hour per person

Chatter replies are budgeted per hour and per person; invoices and receipts always go out.

## Files

- `nr-nostr`, `nr-nostr-jukebox` → `/usr/local/bin/`
- `nr-nostr.service`, `nr-nostr-jukebox.service` → `/etc/systemd/system/`
- `nostr.env.example` → copy to the path the units name. **Never commit the real file.**
- In the home folder (not in the repo): `relays.txt`, one `wss://` relay per line.

Both import the X announcer's text rules (`../announcer/`).

## Switches

- `LIVE` (announcer) / `jukebox.LIVE` (jukebox) → post and mint; without them both only log what they would do
- `OFF` / `jukebox.OFF` → idle

```
nr-nostr --selftest            nr-nostr-jukebox --selftest
nr-nostr --check               nr-nostr-jukebox --check
```

The jukebox selftest checks that a listed word is never echoed; it needs `kanker*` in the announcer's word list.
