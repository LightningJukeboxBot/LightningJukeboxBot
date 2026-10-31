#!/usr/bin/env bash
# Reverse bridge: the live DJ's audio (Owncast) pushed INTO Icecast, so
# audio-only listeners hear the session instead of the jukebox rotation.
#
# Direction is the mirror of bridge-run.sh, and it only ever runs DURING a
# live session -- the moment the set ends and this source disconnects,
# liquidsoap's fallback hands /stream straight back to the rotation by
# itself. Listeners never change URL; the STATION changes what it plays.
#
# Started by owncast-reverse.service. ICECAST_SOURCE_PW comes from
# /etc/owncast-reverse.env (root-only; copied out of icecast.xml by the
# install block -- never typed, never shown).
set -uo pipefail

: "${ICECAST_SOURCE_PW:?ICECAST_SOURCE_PW missing - check /etc/owncast-reverse.env}"

LOG=/tmp/nr-reverse.log

# Wait for a REAL stream. The HLS endpoint alone is not proof of a DJ:
# Owncast serves its offline banner over the same playlist, and pulling that
# crash-looped ffmpeg all through the 2026-07-29 lever test (118 KB of log
# for a two-minute session with zero DJs). Ask Owncast itself, patiently --
# a sesh can be open long before the DJ presses go.
while :; do
  if curl -sf --max-time 5 http://127.0.0.1:8090/api/status 2>/dev/null | grep -q '"online": *true'; then
    break
  fi
  sleep 5
done

# A DJ is live: find a reachable HLS endpoint. Localhost first (no tunnel
# round-trip), the public host as fallback; keep trying while online.
SRC=""
while [ -z "$SRC" ]; do
  for cand in "http://127.0.0.1:8090/hls/stream.m3u8" \
              "https://video.noderunnersradio.com/hls/stream.m3u8"; do
    if curl -sf --max-time 5 "$cand" >/dev/null 2>&1; then SRC="$cand"; break; fi
  done
  [ -n "$SRC" ] || sleep 3
done

{ echo "--- $(date -u) starting"; echo "src=$SRC"; } >> "$LOG" 2>/dev/null || true

# Audio only, re-encoded to the station's usual 192k mp3. Log to /tmp like
# bridge-run.sh: the systemd journal is root-only, /tmp is not.
# -xerror and NO reconnect flags, for the reason the main bridge learned the
# hard way (2026-07-29): reconnect keeps ffmpeg alive after the source dies,
# holding a dead session open while systemd sees a happy process. Die instead;
# Restart=always plus the wait-for-online loop above rebuild a clean session.
exec /usr/bin/ffmpeg -hide_banner -loglevel warning -xerror \
  -i "$SRC" -vn \
  -c:a libmp3lame -b:a 192k -ar 44100 -ac 2 \
  -content_type audio/mpeg \
  -f mp3 "icecast://source:${ICECAST_SOURCE_PW}@127.0.0.1:8005/live" 2>>"$LOG"
