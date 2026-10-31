#!/usr/bin/env bash
# nr-tg-stream.sh -- push the station's EXISTING Owncast program into a
# Telegram group live stream. No second encode: -c copy remuxes the very same
# bytes the website viewers get, so Telegram is a second door on ONE stream.
#
# Credentials: /etc/nr-tg-stream.env (root, 0600), two lines:
#   TG_RTMP_URL=rtmps://dcX-Y.rtmp.t.me/s/
#   TG_RTMP_KEY=xxxxxxxxxxxxxxxx
# SF pastes both from Telegram: group -> Live Stream -> "Stream with...".
# The key is a credential. Only SF handles it.
set -e
[ -r /etc/nr-tg-stream.env ] || { echo "no /etc/nr-tg-stream.env"; exit 1; }
. /etc/nr-tg-stream.env
[ -n "$TG_RTMP_URL" ] && [ -n "$TG_RTMP_KEY" ] || { echo "env incomplete"; exit 1; }

# normalise the join so one slash sits between url and key
TARGET="${TG_RTMP_URL%/}/${TG_RTMP_KEY}"

exec ffmpeg -hide_banner -loglevel warning \
  -i http://127.0.0.1:8090/hls/stream.m3u8 \
  -c copy -f flv "$TARGET"
