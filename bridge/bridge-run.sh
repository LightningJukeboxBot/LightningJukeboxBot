#!/usr/bin/env bash
# Owncast bridge runner, card layout -- nr_videocard (2026-10-15), band v3 nr_tickertape (same day).
# SF 2026-10-14: "upgrade the ship picture to the other view anyway" (the site's card view); 2026-10-15: "yes build it plz",
# then "it still looks jerky/stuttering while it scrolls... can she do better?", "move the bar all the way to the top of
# the screen" and "get rid of the arrow on the left hand side".
#
# The ship picture gets these layers, all drawn by Python (Pillow) into a private folder and read by ffmpeg as looping
# images -- if a drawing program stops, its layer freezes; the feed never depends on it:
#   card.png       1280x720  station mark, what is on air, the shout-out, the next request, the chat (Telegram + Nostr)
#                            -- render_card.py, redrawn when something changes (checked every 3 s)
#   band-bg.png    1280x40   the band's dark strip, at the very TOP of the picture (no player bar ever reaches it)
#   band-tape.png  13568x40  donations, zaps and live tips on a long ring tape -- render_ticker.py paints it AHEAD of
#                            the screen about once a second; FFMPEG moves it, from its own frame clock:
#                            x = -mod(t*96, 12288) = exactly 4 px a frame at 24 fps. That is what makes it glide.
#   the jukebox QR card, top right, under the band, as before (it sits OVER the card layer).
#   clock.png      1280x34   nr_vclock (2026-10-30, SF: "the countdown timer in the video feed too"): a thin bar that
#                            fills + the time left, right of NODERUNNERS FM -- render_clock.py, redrawn once a second.
#                            Unknown length -> an empty layer. Brake: /tmp/nr-clock-off.
# ffmpeg's t = 0 is written to $PRIV/t0 (wall time) right before it starts: the painter needs it to stay off the screen.
# Every program here runs with stdin closed, ffmpeg also with -nostdin: an ffmpeg that sees a terminal tries to use it,
# and outside the terminal's process group Linux STOPS it (the second test run on the box froze on it, 2026-10-15).
# If any part cannot start, this script hands over to bridge-run-classic.sh (the layout of 2026-09-08), so the feed
# always goes out. The captain's brakes still work: /tmp/nr-chat-off (chat), /tmp/nr-media-off (chat pictures),
# /tmp/nr-ticker-off (the band).
# TEST (no Owncast, no stream key, a throwaway folder; writes test.mp4 there):
#   NR_BRIDGE_TEST=6 NR_BRIDGE_PRIV=/some/dir bash bridge-run.sh
#
# Started by owncast-bridge.service (User=jukebox). STREAM_KEY comes from /etc/owncast-bridge.env (root-only).
set -uo pipefail

HERE=/srv/jukebox/app/bridge
[ -n "${NR_BRIDGE_TEST:-}" ] && HERE="${NR_BRIDGE_HERE:-$HERE}"     # the installer tests the new files before they land
CLASSIC="$HERE/bridge-run-classic.sh"
VENVPY=/srv/jukebox/app/venv/bin/python3
PRIV="${NR_BRIDGE_PRIV:-/srv/jukebox/cache/nr-bridge}"     # private (700): nobody else can put words on the picture
API=http://127.0.0.1:7100
IMG=/srv/jukebox/site/assets/ship-bg.jpg
QR=/srv/jukebox/site/assets/jukebox-qr-card.png
ICECAST=http://127.0.0.1:8000/stream
FPS=24                     # was 2, then 12 ("very annoying to watch", SF 2026-10-15): 24 = Owncast's own output rate
SPEED=96                   # band pixels a second = 4 a frame at 24 fps; must match render_ticker.py's SPEED
TAPE_L=12288               # the tape's lap in pixels; must match render_ticker.py's TAPE_L
BAND_H=40                  # the band runs along the very top (y 0-40)
QR_Y=52                    # the QR card sits under the band
CLOCK_Y=56                 # nr_vclock: the clock layer's top edge; must match render_clock.py CLOCK_Y (TOP - 6)
TEST="${NR_BRIDGE_TEST:-}"
LOG=/tmp/nr-bridge.log
[ -n "$TEST" ] && LOG="$PRIV/test.log"

say() { echo "$(date) card: $*" >> "$LOG" 2>/dev/null || true; }
classic() {
  say "card layout unavailable ($1) -- handing over to the classic layout"
  if [ -n "$TEST" ]; then echo "CLASSIC: $1"; exit 3; fi
  exec /bin/bash "$CLASSIC"
}

[ -n "$TEST" ] || : "${STREAM_KEY:?STREAM_KEY missing - check /etc/owncast-bridge.env}"

# Output is CAPTURED, never piped into grep -q: under pipefail, grep -q quits early, ffmpeg dies of SIGPIPE (141) and a
# good ffmpeg reads as a bad one (the first dry run on the box, 2026-10-15).
FFMPEG=""
for cand in /usr/bin/ffmpeg /usr/local/bin/ffmpeg /srv/jukebox/owncast/owncast/ffmpeg; do
  [ -x "$cand" ] || continue
  enc=$("$cand" -hide_banner -encoders 2>/dev/null || true)
  if [[ "$enc" == *libx264* ]]; then FFMPEG="$cand"; break; fi
done
[ -n "$FFMPEG" ] || classic "no ffmpeg with libx264"
mkdir -p "$PRIV" 2>/dev/null && chmod 700 "$PRIV" || classic "private folder $PRIV"
[ -r "$QR" ] || classic "jukebox QR card not readable"
{ [ -x "$VENVPY" ] && "$VENVPY" -c "import PIL" 2>/dev/null; } || classic "no Pillow in the venv"
export NR_FFMPEG="$FFMPEG" NR_BRIDGE_PRIV="$PRIV" NR_BAND_H="$BAND_H"
rm -f "$PRIV/t0"           # an old start time would make the painter think the band is somewhere it is not

# the ship, scaled ONCE (it is a still): ffmpeg then only repeats it
"$FFMPEG" -nostdin -y -loglevel error -i "$IMG" -vf "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720" \
  -frames:v 1 "$PRIV/bg.png" </dev/null 2>>"$LOG" || classic "background picture (rc=$?)"
timeout 40 "$VENVPY" "$HERE/render_card.py" --once "$API" "$PRIV/card.png" </dev/null >>"$LOG" 2>&1 || classic "card layer (rc=$?)"
timeout 40 "$VENVPY" "$HERE/render_ticker.py" --once "$API" "$PRIV/band-tape.png" </dev/null >>"$LOG" 2>&1 || classic "ticker band (rc=$?)"
timeout 40 "$VENVPY" "$HERE/render_clock.py" --once "$API" "$PRIV/clock.png" </dev/null >>"$LOG" 2>&1 || classic "clock layer (rc=$?)"
[ -s "$PRIV/card.png" ] && [ -s "$PRIV/band-tape.png" ] && [ -s "$PRIV/band-bg.png" ] && [ -s "$PRIV/clock.png" ] || classic "layers missing after the first draw"

( while true; do "$VENVPY" "$HERE/render_card.py" --loop "$API" "$PRIV/card.png" </dev/null 2>>"$PRIV/card.err"; sleep 3; done ) &
( while true; do "$VENVPY" "$HERE/render_ticker.py" --loop "$API" "$PRIV/band-tape.png" </dev/null 2>>"$PRIV/ticker.err"; sleep 3; done ) &
( while true; do "$VENVPY" "$HERE/render_clock.py" --loop "$API" "$PRIV/clock.png" </dev/null 2>>"$PRIV/clock.err"; sleep 3; done ) &

# PREFLIGHT, as in the classic script: wait until Icecast lists the /stream mount (never read the stream itself).
while true; do
  st=$(curl -sf --max-time 5 "http://127.0.0.1:8000/status-json.xsl" 2>/dev/null || true)
  [[ "$st" == *'/stream"'* ]] && break
  say "preflight: waiting for the /stream mount"; sleep 2
done

# inputs: 0 audio | 1 the ship (repeated to FPS) | 2 the card layer | 3 the QR card | 4 the band's strip | 5 the tape
#         | 6 the clock layer (nr_vclock)
GRAPH="[1:v]fps=${FPS},format=yuv420p[bg];[bg][2:v]overlay=0:0[a];[a][3:v]overlay=W-w-36:${QR_Y}[b];[b][4:v]overlay=0:0[c];[c][5:v]overlay=x='-mod(t*${SPEED},${TAPE_L})':y=0[d];[d][6:v]overlay=0:${CLOCK_Y}[e];[e]format=yuv420p[v]"
say "ffmpeg=$FFMPEG fps=$FPS band=top tape=$TAPE_L speed=$SPEED graph=$GRAPH"
IN=( -thread_queue_size 1024 -i "$ICECAST"
     -thread_queue_size 64 -loop 1 -framerate 2 -i "$PRIV/bg.png"
     -f image2 -thread_queue_size 64 -loop 1 -framerate 2 -i "$PRIV/card.png"
     -thread_queue_size 64 -loop 1 -framerate 2 -i "$QR"
     -f image2 -thread_queue_size 64 -loop 1 -framerate 1 -i "$PRIV/band-bg.png"
     -f image2 -thread_queue_size 64 -loop 1 -framerate 1 -i "$PRIV/band-tape.png"
     -f image2 -thread_queue_size 64 -loop 1 -framerate 2 -i "$PRIV/clock.png" )
ENC=( -filter_complex "$GRAPH" -map "[v]" -map 0:a
      -af "aresample=async=1:first_pts=0"
      -c:v libx264 -preset veryfast -b:v 600k -maxrate 700k -bufsize 1200k -r "$FPS" -g $((FPS * 4)) -fps_mode cfr
      -c:a aac -b:a 160k -ar 44100 -ac 2
      -max_interleave_delta 0 -fflags +genpts )

date +%s.%N > "$PRIV/t0"   # ffmpeg's t = 0, give or take its start-up; the painter keeps 12 s of margin for that
if [ -n "$TEST" ]; then
  # a short clip in the throwaway folder: the installer checks the band glides 4 px a frame
  "$FFMPEG" -nostdin -hide_banner -loglevel error -xerror "${IN[@]}" "${ENC[@]}" -t "$TEST" -y "$PRIV/test.mp4" </dev/null 2>>"$LOG"
  rc=$?
  kill $(jobs -p) 2>/dev/null
  pkill -f -- "$PRIV/" 2>/dev/null       # the drawing programs of THIS test only (their arguments name its folder)
  echo "TEST ffmpeg rc=$rc"
  exit $rc
fi
# -xerror on purpose, as in the classic script: DIE on input loss, let Restart=always and the preflight bring us back.
exec "$FFMPEG" -nostdin -hide_banner -loglevel warning -xerror "${IN[@]}" "${ENC[@]}" \
  -f flv "rtmp://127.0.0.1:1935/live/${STREAM_KEY}" 2>>"$LOG"
