# Noderunners Radio — Ops notes: secrets & royalty logging

*Living doc. If you're a future dev (or future SF) digging into how the stream
authenticates or how plays get attributed to royalty pools, start here.*

---

## 1. Icecast source password (machine-to-machine secret)

**What it is:** the shared secret liquidsoap uses to publish the audio stream to
Icecast. It is NOT a login password and **cannot be salted/hashed** — Icecast
must compare the plaintext the source client presents, so both ends hold the
real value. "Best practice" here = rotate + lock down + keep out of readable
config, NOT hashing.

**Where it lives (rotated & migrated 2026-07-22):**
- `/etc/liquidsoap/noderunners.env`  → `ICECAST_SOURCE_PW=...`  (perms `600`, `radio:radio`)
- `/etc/icecast2/icecast.xml`         → `<source-password>...</source-password>`  (perms `640`)
- Liquidsoap reads it via `environment.get("ICECAST_SOURCE_PW")` in
  `/etc/liquidsoap/noderunners.liq`; the env file is loaded by a systemd drop-in
  `/etc/systemd/system/liquidsoap.service.d/env.conf` (`EnvironmentFile=`).

**You don't need to memorize it.** Retrieve if ever needed:
`sudo cat /etc/liquidsoap/noderunners.env`

**To rotate:** generate a new value, write it to BOTH the env file and
`icecast.xml`'s `<source-password>`, then `systemctl restart icecast2` and
`systemctl restart liquidsoap` (brief audio gap). Never put it back into the
`.liq` in plaintext. (Prior value was found hardcoded in the `.liq` and rotated
out — don't regress to that.)

---

## 2. Play logging & royalty pools

**Model (settled):** ONE audio stream, ONE append-only log, filtered at report
time. No splitting into multiple streams.

- Log: `/var/log/liquidsoap/playlog.tsv`, written by the `log_play` on_track hook
  in `noderunners.liq`. 6 tab-separated columns:
  `timestamp | event | source | rights_class | artist | title`
  - `event`: `play` | `set_start` | `set_end`
  - `rights_class`: `collecting_society` | `v4v` | `owned` | `public_domain` | `cc0` | `unknown`
- Report: `playlog_report.py YYYY-MM` → one summary per pool; flags any
  `unknown` (unattributed) plays for triage.

**Core principle — the SOURCE determines the lane, deterministically.**
`rights_class` is set once, at resolve time, by whichever resolver supplied the
track (local library → `collecting_society`; Wavlake → `v4v`; etc.). It is NOT
gamed per play. A deterministic, source-driven lane is exactly what makes the
log defensible under audit — the opposite (re-routing plays to dodge a pool)
turns the log into a liability.

---

## 3. The two-lane story (for when a collecting society asks)

Noderunners runs two legitimate, separate licensing lanes on one stream:

1. **`collecting_society` lane** — licensed repertoire, aired under the paid
   BUMA/STEMRA + SENA webcast licence. Fully logged with timestamps; the monthly
   report names every track aired in this lane. Above board, nothing hidden.
2. **`v4v` lane** — Wavlake content, aired under Wavlake's explicit rebroadcast
   permission ("as long as listeners can directly pay the artists in a V4V way";
   confirmed by Wavlake 2026-07-19). These artists opted into direct-sats
   payment; each play pays them per Wavlake's LNURL split. No collecting society
   sits in this lane.

**Backbone:** we stand behind exactly what we aired — the play log is the single
source of truth and we can produce the society report on demand. We do not
under-report tracks.

**Open legal question (needs a professional, do NOT hand-wave):** an artist may
be registered with a collecting society AND take V4V on Wavlake. If they've
assigned public-performance rights to the society, a V4V-sourced play of their
Wavlake upload *might* still carry a society obligation. The log preserves
`source` + `artist` + `rights_class` on every line precisely so this can be
reconciled honestly either way once a pro advises. Design captures the truth;
it does not pre-decide the edge case.

**"Profits the artist most" lever = resolver-chain ORDER, and it's a business
decision for SF (not code's to assume).** For a track available from BOTH a
licensed local copy and Wavlake, whichever resolver is tried first wins the
play and thus the lane. Preferring the Wavlake lane for artists who offer V4V
routes direct sats to them (honouring their chosen channel) — legitimate and
transparent, distinct from evasion. Currently local is tried first; revisit
when Wavlake goes live (it's gated until the appId/payout works).

---

## 4. Privacy — we are not ship-rats

- **Tracks aired:** fully documented (that's how artists get paid). Backbone.
- **People:** never doxxed. DJ set entries log a **handle/pseudonym only** —
  never real names or personal info. Requester identities (Telegram usernames,
  etc.) NEVER enter the play log or any report. Reports carry tracks + counts +
  timestamps, no PII.
- Keep any external-facing artifact (society reports) free of personal data.

---

## 5. Live DJ sets

Ad hoc; DJs are NOT expected to provide tracklists. Minimum viable logging: a
`set_start` / `set_end` pair carrying the DJ handle + a set label, so the log
shows who was on and when, without per-track detail. Per-track ID inside a live
set (audio fingerprinting) is feasibility-unclear and NOT promised. Practice
material (recordings) exists at youtube.com/@NoderunnersFM.
