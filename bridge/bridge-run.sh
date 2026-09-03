#!/usr/bin/env bash
# Owncast bridge runner — the station's own audio + the ship image + a
# "Artist - Title" strip, pushed into Owncast as the live video feed.
#
# Two jobs in one service:
#   1. a small loop that keeps nowplaying.txt in step with the play log
#   2. ffmpeg, which redraws that text on every frame (reload=1)
# Keeping both here means systemd never has to parse an ffmpeg filter string.
#
# Started by owncast-bridge.service. STREAM_KEY comes from
# /etc/owncast-bridge.env (root-only).
set -uo pipefail

PLAYLOG=/var/log/liquidsoap/playlog.tsv
# /tmp, not the bridge folder: that folder is owned by another account, so the
# service (running as sf) could not write there -- which killed the whole feed
# on 2026-07-28. The video must never depend on the text overlay succeeding.
NP=/tmp/nr-nowplaying.txt
NEXT=/tmp/nr-nextup.txt      # a paid request waiting in the queue (blank = nothing)
NOTE=/tmp/nr-note.txt        # the listener's fortune-cookie note for the track ON AIR (blank = none)
CHAT=/tmp/nr-chat.txt        # the last few lines of the Telegram chat
API=http://127.0.0.1:7100    # the station's own API, localhost only
IMG=/home/sf/noderunners-site/assets/ship-bg.jpg
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
ICECAST=http://127.0.0.1:8000/stream
# Donation card burned into the picture, top right. It can only ever show in
# RADIO mode: this bridge is stopped for a live DJ session, and during a session
# the OBS overlay carries that session's own QR instead.
# The card holds an LNURL-pay for the station jar -- public by design, pay-only.
QR=/home/sf/noderunners-site/assets/donate-qr-card.png
# The station's own mark, bottom right -- the broadcaster's "bug". Radio mode
# only, same as everything else this script draws.
LOGO=/home/sf/noderunners-site/assets/nr-logo-feed.png

# Owncast ships a stripped static ffmpeg with NO drawtext filter (found the
# hard way 2026-07-28: the feed crash-looped 30 times). Prefer a system ffmpeg
# that can draw text; fall back to Owncast's, and simply skip the overlay.
FFMPEG=/home/sf/owncast/owncast/ffmpeg
HAVE_DRAWTEXT=0
for cand in /usr/bin/ffmpeg /usr/local/bin/ffmpeg /home/sf/owncast/owncast/ffmpeg; do
  [ -x "$cand" ] || continue
  if "$cand" -hide_banner -filters 2>/dev/null | grep -qE '[[:space:]]drawtext[[:space:]]'; then
    FFMPEG="$cand"; HAVE_DRAWTEXT=1; break
  fi
done

: "${STREAM_KEY:?STREAM_KEY missing - check /etc/owncast-bridge.env}"

# --- job 1: keep the on-screen text fresh ---------------------------------
# Three files, all redrawn by ffmpeg every frame (reload=1): what is playing,
# what somebody has paid to hear next, and the last few lines of chat.
# Every one of them is written atomically and can safely be empty.
now_playing_loop() {
  local line cur
  while true; do
    line=$(tail -n 400 "$PLAYLOG" 2>/dev/null | awk -F'\t' \
      '$2=="play" && ($5!="" || $6!="") {a=$5; t=$6} END{ if (a!="" || t!="") printf "%s - %s", a, t }')
    # '%' is a format specifier to drawtext; strip it and any control bytes
    line=$(printf '%s' "${line:-}" | tr -d '%\000-\037' | cut -c1-70)
    [ -z "$line" ] && line="Noderunners Radio"
    cur=$(cat "$NP" 2>/dev/null || true)
    [ "$line" != "$cur" ] && printf '%s' "$line" > "$NP"

    # requests + chat come from the station's own API. If it is unreachable the
    # files simply keep their last contents -- the picture never depends on it.
    python3 - "$NEXT" "$CHAT" "$API" "$NOTE" <<'PY' 2>/dev/null || true
import json, os, sys, textwrap, urllib.request

nextf, chatf, api, notef = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

def get(path):
    with urllib.request.urlopen(api + path, timeout=4) as r:
        return json.load(r)

def clean(s, n):
    # DejaVu has no emoji, and drawtext chokes on control bytes -- keep it plain
    s = "".join(c for c in (s or "") if 32 <= ord(c) < 127)
    return s.replace("%", "").strip()[:n]

def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # EMPTY when blank. A lone space made drawtext paint its box around nothing: two idle
        # black boxes above the now-playing strip (SF 2026-09-02). Tested on this ffmpeg 6.1:
        # an empty text file draws nothing and reloads fine; the start-up seed stays a space.
        f.write(text)
    os.replace(tmp, path)                  # atomic: ffmpeg never sees a half-written file

try:
    npd = get("/api/nowplaying")
    q = (npd.get("queue") or [])
    line = ""
    if q:
        t = q[0]
        line = "REQUESTED NEXT:  " + clean(t.get("artist"), 26) + " - " + clean(t.get("title"), 32)
        if len(q) > 1:
            line += "   (+%d more)" % (len(q) - 1)
    write(nextf, line)
    # NOTES (2026-09-02): the note a listener attached to the track ON AIR, in quotes.
    # Comes from the API only (never from liquidsoap, never from the play-log); the API
    # already applies the kill switch and the hide list, so an emptied note vanishes here
    # within 5 s too. ASCII-only like everything else drawtext gets.
    np = npd.get("now_playing") or {}
    note = clean(np.get("note"), 80) if np.get("requested") else ""
    who = clean(np.get("who"), 24) if note else ""
    write(notef, ('"' + note + '"' + ("   - " + who if who else "")) if note else "")
except Exception:
    pass

# KILL SWITCH: `touch /tmp/nr-chat-off` on the server and the chat strip leaves
# the broadcast within 5 seconds; `rm` it to bring the chat back. It exists
# because whatever anyone types in that group goes out on the picture.
try:
    if os.path.exists("/tmp/nr-chat-off"):
        write(chatf, "")
    else:
        msgs = get("/api/chat/recent").get("messages") or []
        lines = []
        for m in msgs[-10:]:
            who, what = clean(m.get("username"), 13), clean(m.get("text"), 200)
            # GIFs/photos posted without a caption used to VANISH from the
            # strip (SF, 2026-08-14). drawtext cannot show the image itself,
            # but it can honestly say one happened. DejaVu has no emoji, so
            # plain brackets it is.
            kind = ((m.get("media") or {}).get("kind") or "").lower()
            tag = {"animation": "[GIF]", "photo": "[photo]",
                   "sticker": "[sticker]"}.get(kind, "[media]" if kind else "")
            if tag:
                what = (what + " " + tag).strip() if what else tag
            if not what:
                continue
            # long messages WRAP onto continuation lines instead of being cut
            # off, but one person never gets more than three lines of picture
            wrapped = textwrap.wrap(f"{who}: {what}", width=46, subsequent_indent="   ") or []
            lines.extend(wrapped[:3])
        write(chatf, "\n".join(lines[-9:]))   # taller strip, SF 2026-08-14
except Exception:
    pass

# MEDIA PANEL (SF 2026-08-14: "[GIF] instead of showing the image, fix it").
# This pipeline cannot ANIMATE chat media, but it can SHOW it: pull the newest
# GIF/photo, take one frame, swap it atomically under the -loop image input.
# Newest wins and stays until the next one. Failures cost only the panel.
try:
    if not os.path.exists("/tmp/nr-chat-off"):
        media = [m for m in msgs if (m.get("media") or {}).get("file_id")]
        idf = "/tmp/nr-chat-media.id"      # the fid currently ON the panel
        badf = "/tmp/nr-chat-media.bad"    # fids ffmpeg could not open (tgs etc.)
        shown = open(idf).read().strip() if os.path.exists(idf) else ""
        bad = set(open(badf).read().split()) if os.path.exists(badf) else set()
        # newest first; show the first RENDERABLE one. A sticker ffmpeg cannot
        # open must never freeze the panel or hide an older good GIF
        # (SF, 2026-08-14: "some work some don't, seems a bit arbitrary").
        for m in reversed(media[-6:]):
            fid = m["media"]["file_id"]
            if fid == shown:
                break                       # panel already shows the newest good one
            if fid in bad:
                continue
            import subprocess, urllib.parse
            dl = "/tmp/nr-chat-media.dl"
            try:
                with urllib.request.urlopen(
                        api + "/api/chat/media?file_id=" + urllib.parse.quote(fid),
                        timeout=10) as r, open(dl, "wb") as f:
                    f.write(r.read())
                ff = os.environ.get("NR_FFMPEG", "ffmpeg")
                new = "/tmp/nr-chat-media.new.png"
                ok = subprocess.run(
                    [ff, "-y", "-loglevel", "error", "-i", dl,
                     "-frames:v", "1", "-vf",
                     "scale=w=200:h=170:force_original_aspect_ratio=decrease", new],
                    timeout=20).returncode == 0 and os.path.getsize(new) > 0
            except Exception:
                ok = False
            if ok:
                os.replace(new, "/tmp/nr-chat-media.png")
                with open(idf, "w") as f:
                    f.write(fid)
                break
            bad.add(fid)
            with open(badf, "w") as f:      # keep the dud-list small
                f.write("\n".join(list(bad)[-20:]))
except Exception:
    pass
PY
    [ "$CHAT_IMG" = "1" ] && { "$VENVPY" "$RENDER" "$API" /tmp/nr-chat.png 2>/dev/null || true; }
    sleep 5
  done
}

# --- decide whether the text overlay is safe to use -----------------------
# If anything about it is not writable/readable, the feed still goes out --
# just without the strip. Silence is better than a black screen.
# --- inline chat mode (Telegram-style, SF 2026-08-14) ---------------------
# When Pillow + the renderer are available, the chat column is drawn as ONE
# PNG with media thumbnails inline, and ffmpeg overlays that instead of the
# drawtext strip. If anything is missing, the old text strip carries on.
CHAT_IMG=0
VENVPY=/home/sf/LightningJukeboxBot/venv/bin/python3
RENDER=/home/sf/LightningJukeboxBot/bridge/render_chat.py
if [ -x "$VENVPY" ] && [ -r "$RENDER" ] && "$VENVPY" -c "import PIL" 2>/dev/null; then
  export NR_FFMPEG="$FFMPEG"
  "$VENVPY" "$RENDER" "$API" /tmp/nr-chat.png 2>/dev/null || true
  [ -s /tmp/nr-chat.png ] && CHAT_IMG=1
fi

VF="scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"
if [ "$HAVE_DRAWTEXT" = "1" ] && printf 'Noderunners Radio' > "$NP" 2>/dev/null && [ -r "$FONT" ]; then
  printf ' ' > "$NEXT" 2>/dev/null || true
  printf ' ' > "$NOTE" 2>/dev/null || true
  printf ' ' > "$CHAT" 2>/dev/null || true
  # media panel: the loop's python extracts frames with the same ffmpeg, and
  # a transparent seed keeps the input harmless until the first GIF arrives
  export NR_FFMPEG="$FFMPEG"
  [ -s /tmp/nr-chat-media.png ] || "$FFMPEG" -y -loglevel error -f lavfi \
      -i "color=black@0.0:s=200x140,format=rgba" -frames:v 1 \
      /tmp/nr-chat-media.png 2>/dev/null || true
  now_playing_loop &
  # Heights are deliberate. The site draws its own player controls over the
  # bottom of the picture, and they were covering the track name (SF, 2026-07-28)
  # -- worse on a phone, where the controls are proportionally taller. Everything
  # now sits well clear of that strip:
  #   chat        bottom left,   y = h-360
  #   requested   centred above, y = h-262
  #   note        centred,       y = h-225   (the listener's note for the track on air, quoted)
  #   now playing centred,       y = h-170
  # expansion=none: chat is public text, it must never be read as a filter expression.
  # Type matches the website: the site is set in a monospace stack, so the feed
  # uses DejaVu Sans Mono. Chat and the request line use the REGULAR weight --
  # SF found the bold too heavy (2026-07-28) -- and a 2px outline instead of a
  # box, so they stay readable over sky or hull and draw NOTHING when empty.
  MONO=/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf
  MONOB=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf
  MONOI=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf
  [ -r "$MONO" ]  || MONO="$FONT"
  [ -r "$MONOB" ] || MONOB="$FONT"
  [ -r "$MONOI" ] || MONOI="$MONO"
  D="drawtext=fontfile=${MONO}:reload=1:expansion=none:borderw=2:bordercolor=black@0.92"
  if [ "$CHAT_IMG" != "1" ]; then
    VF="${VF},${D}:textfile=${CHAT}:fontsize=17:line_spacing=8:fontcolor=white@0.93:x=40:y=h-500"
  fi
  # requested-next and the note sit in soft boxes like the now-playing strip: readable over
  # sky, sea or hull (SF 2026-09-02: "hard to read"). A box draws nothing when the file is blank.
  VF="${VF},drawtext=fontfile=${MONO}:reload=1:expansion=none:textfile=${NEXT}:fontsize=20:fontcolor=white@0.95:box=1:boxcolor=black@0.55:boxborderw=8:x=(w-text_w)/2:y=h-262"
  VF="${VF},drawtext=fontfile=${MONOI}:reload=1:expansion=none:textfile=${NOTE}:fontsize=22:fontcolor=white@0.98:box=1:boxcolor=black@0.6:boxborderw=10:x=(w-text_w)/2:y=h-225"
  VF="${VF},drawtext=fontfile=${MONOB}:reload=1:expansion=none:textfile=${NP}:fontsize=28:fontcolor=white@0.96:box=1:boxcolor=black@0.45:boxborderw=16:x=(w-text_w)/2:y=h-170"
else
  echo "bridge: now-playing overlay unavailable, streaming without it" >&2
fi

# --- decide whether the donation card is safe to use ----------------------
# Same rule as the text strip: if the file is not there, the feed still goes
# out, just without the card. Never let decoration take the picture down.
# Built one image at a time so a missing file only costs its own overlay: the
# input indexes are computed, never hard-coded, and if every image is missing
# the script falls back to the plain -vf path and still streams.
QR_IN=()
CHAIN=""
IDX=2                      # 0 = icecast audio, 1 = the background picture
LAST="[bgv]"
if [ -r "$QR" ]; then
  # -loop: a single-frame input ENDS at once and leans on overlay's repeat
  # grace, which came up empty on an unlucky start (QR+logo vanished,
  # 2026-08-14). A looping input never ends and never needs grace.
  QR_IN+=( -loop 1 -framerate 2 -thread_queue_size 64 -i "$QR" )
  CHAIN="${CHAIN};${LAST}[${IDX}:v]overlay=W-w-36:36[ov${IDX}]"
  LAST="[ov${IDX}]"; IDX=$((IDX + 1))
else
  echo "bridge: donation card not readable at $QR, streaming without it" >&2
fi
if [ -r "$LOGO" ]; then
  QR_IN+=( -loop 1 -framerate 2 -thread_queue_size 64 -i "$LOGO" )
  CHAIN="${CHAIN};${LAST}[${IDX}:v]overlay=W-w-36:H-h-92[ov${IDX}]"
  LAST="[ov${IDX}]"; IDX=$((IDX + 1))
else
  echo "bridge: station logo not readable at $LOGO, streaming without it" >&2
fi
if [ "$CHAT_IMG" = "1" ]; then
  # the whole chat column as one image, media inline, bottom-anchored left
  QR_IN+=( -f image2 -loop 1 -framerate 2 -thread_queue_size 64 -i /tmp/nr-chat.png )
  # 40 px higher than before: the requested-next line moved up to h-262 for the note and
  # collided with the column's last line (SF 2026-09-02).
  CHAIN="${CHAIN};${LAST}[${IDX}:v]overlay=40:H-h-280[ov${IDX}]"
  LAST="[ov${IDX}]"; IDX=$((IDX + 1))
elif [ -s /tmp/nr-chat-media.png ]; then
  # fallback mode: latest GIF/photo as a fixed top-left panel
  QR_IN+=( -f image2 -loop 1 -framerate 2 -thread_queue_size 64 -i /tmp/nr-chat-media.png )
  CHAIN="${CHAIN};${LAST}[${IDX}:v]overlay=40:30[ov${IDX}]"
  LAST="[ov${IDX}]"; IDX=$((IDX + 1))
else
  echo "bridge: no chat media panel yet, streaming without it" >&2
fi
if [ -n "$CHAIN" ]; then
  MAPS=( -filter_complex "[1:v]${VF}[bgv]${CHAIN};${LAST}format=yuv420p[v]" -map "[v]" -map 0:a )
else
  MAPS=( -vf "${VF},format=yuv420p" -map 1:v -map 0:a )
fi

# --- job 2: the video feed ------------------------------------------------
# Text sits bottom-centre in a soft box: readable over any image, carrying
# meaning by position and contrast rather than colour.
# own error log: the service journal is root-only, and a feed that dies
# silently is a feed nobody can fix
LOG=/tmp/nr-bridge.log
{ echo "--- $(date) starting"; echo "ffmpeg=$FFMPEG drawtext=$HAVE_DRAWTEXT qr=$([ -r "$QR" ] && echo 1 || echo 0)"; echo "vf=$VF"; echo "maps=${MAPS[*]}"; } >> "$LOG" 2>/dev/null || true

# PREFLIGHT: wait for the audio source to be genuinely healthy before starting.
# The whole crash class (Owncast dies every time liquidsoap restarts, 2026-07-28
# and -29) came from starting ffmpeg against Icecast /stream while liquidsoap
# was mid-restart: the mount 404s, and with -reconnect_streamed ffmpeg used to
# hang alive-but-useless, so Owncast saw a dead RTMP session forever. Now we
# wait for real audio bytes first, and if the input ever dies we EXIT so systemd
# gives Owncast a clean, fresh session instead of a wedged one.
# Check the MOUNT is present in Icecast's status -- never read the stream
# itself: /stream is continuous, so a byte/range read just hangs until timeout
# (that exact mistake wedged this preflight once). When liquidsoap is mid-
# restart the mount vanishes from status-json and reappears when it returns.
STATUS_URL="http://127.0.0.1:8000/status-json.xsl"
until curl -sf --max-time 5 "$STATUS_URL" 2>/dev/null | grep -q '/stream"'; do
  echo "$(date) preflight: waiting for the /stream mount" >> "$LOG" 2>/dev/null || true
  sleep 2
done
echo "$(date) preflight OK, source is live" >> "$LOG" 2>/dev/null || true

# Live audio + a looped still image start on different clocks, so newer ffmpeg
# refuses the mux ("negative timestamp", 2026-07-28): force audio to zero, hold
# the video at a constant rate, stop ffmpeg buffering for interleave it never gets.
# NO reconnect flags, and -xerror ON PURPOSE. Reconnect keeps ffmpeg ALIVE
# after the local Icecast mount drops (a liquidsoap restart), holding a dead
# RTMP session open -- Owncast then reads offline forever while systemd sees
# a perfectly happy process. Seen twice, once as a 57-minute-old wedged
# ffmpeg with the feed down. We want the opposite: DIE on input loss, let
# Restart=always bring us back, let the preflight wait for a real source.
exec "$FFMPEG" -hide_banner -loglevel warning -xerror \
  -thread_queue_size 1024 \
  -i "$ICECAST" \
  -thread_queue_size 512 -loop 1 -framerate 2 -i "$IMG" \
  "${QR_IN[@]}" \
  "${MAPS[@]}" \
  -af "aresample=async=1:first_pts=0" \
  -c:v libx264 -preset veryfast -b:v 600k -maxrate 700k -bufsize 1200k -r 2 -g 8 -fps_mode cfr \
  -c:a aac -b:a 160k -ar 44100 -ac 2 \
  -max_interleave_delta 0 -fflags +genpts \
  -f flv "rtmp://127.0.0.1:1935/live/${STREAM_KEY}" 2>>"$LOG"
