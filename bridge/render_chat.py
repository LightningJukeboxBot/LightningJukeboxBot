#!/usr/bin/env python3
"""
render_chat.py -- draw the feed's chat column as ONE transparent PNG, with
media thumbnails INLINE in the message flow, Telegram-style (SF, 2026-08-14:
"just have them scroll by"). ffmpeg overlays the PNG bottom-anchored; the
-loop image input picks up every atomic swap, so the column scrolls as chat
moves. Any failure leaves the previous image in place -- the feed never
depends on this succeeding.

Usage: render_chat.py <api_base> <out_png>
"""

import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

API = sys.argv[1]
OUT = sys.argv[2]

W = 470                    # column width on the 1280x720 canvas
MAX_H = 450                # never taller than this (bottom-anchored budget; incl. the backing panel)
LINE_H = 25
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
THUMB_DIR = "/tmp/nr-chat-thumbs"
THUMB_W, THUMB_MAX_H = 160, 120
FF = os.environ.get("NR_FFMPEG", "ffmpeg")


def clean(s, n):
    s = "".join(c for c in (s or "") if 32 <= ord(c) < 127)
    return s.replace("%", "").strip()[:n]


def get(path):
    with urllib.request.urlopen(API + path, timeout=4) as r:
        return json.load(r)


def thumb_for(fid):
    """Cached inline thumbnail for one media file_id, or None if unrenderable."""
    os.makedirs(THUMB_DIR, exist_ok=True)
    safe = "".join(c for c in fid if c.isalnum())[:48]
    png = os.path.join(THUMB_DIR, safe + ".png")
    bad = os.path.join(THUMB_DIR, safe + ".bad")
    if os.path.exists(png):
        return png
    if os.path.exists(bad):
        return None
    dl = os.path.join(THUMB_DIR, safe + ".dl")
    try:
        with urllib.request.urlopen(
                API + "/api/chat/media?file_id=" + urllib.parse.quote(fid),
                timeout=10) as r, open(dl, "wb") as f:
            f.write(r.read())
        ok = subprocess.run(
            [FF, "-y", "-loglevel", "error", "-i", dl, "-frames:v", "1",
             "-vf", "scale=w=%d:h=%d:force_original_aspect_ratio=decrease"
             % (THUMB_W, THUMB_MAX_H), png],
            timeout=20).returncode == 0
    except Exception:
        ok = False
    try:
        os.remove(dl)
    except OSError:
        pass
    if ok and os.path.exists(png) and os.path.getsize(png) > 0:
        return png
    open(bad, "w").close()          # a sticker ffmpeg cannot open: remember
    return None


def main():
    # CONSTANT canvas size always: ffmpeg's looping input must never see the
    # frame dimensions change mid-stream. Content bottom-anchors on alpha.
    if os.path.exists("/tmp/nr-chat-off"):
        img = Image.new("RGBA", (W, MAX_H), (0, 0, 0, 0))   # kill switch: empty
        tmp = OUT + ".tmp.png"
        img.save(tmp)
        os.replace(tmp, OUT)
        return

    msgs = (get("/api/chat/recent").get("messages") or [])[-10:]
    font = ImageFont.truetype(FONT, 17)

    # build blocks newest-last: each = (text_lines, thumb_path_or_None)
    blocks = []
    extracted = 0
    for m in msgs:
        who = clean(m.get("username"), 13)
        what = clean(m.get("text"), 200)
        media = (m.get("media") or {})
        fid = media.get("file_id")
        thumb = None
        # /tmp/nr-media-off: captain's brake for IMAGES only -- text keeps
        # flowing, pictures wait outside (raid lesson, 2026-08-14)
        if fid and os.path.exists("/tmp/nr-media-off"):
            fid = None
            kind = (media.get("kind") or "").lower()
            tag = {"animation": "[GIF]", "photo": "[photo]",
                   "sticker": "[sticker]"}.get(kind, "[media]" if kind else "")
            what = (what + " " + tag).strip() if tag else what
        if fid:
            if extracted < 2 or os.path.exists(
                    os.path.join(THUMB_DIR,
                                 "".join(c for c in fid if c.isalnum())[:48] + ".png")):
                thumb = thumb_for(fid)
                extracted += 1
            if thumb is None:
                kind = (media.get("kind") or "").lower()
                tag = {"animation": "[GIF]", "photo": "[photo]",
                       "sticker": "[sticker]"}.get(kind, "[media]" if kind else "")
                what = (what + " " + tag).strip() if tag else what
        if not what and not thumb:
            continue
        lines = textwrap.wrap("%s: %s" % (who, what), width=44,
                              subsequent_indent="   ")[:3] if what else \
                ["%s:" % who]
        blocks.append((lines, thumb))

    # bottom-anchored: keep the newest blocks that fit the height budget
    kept, used = [], 0
    for lines, thumb in reversed(blocks):
        h = len(lines) * LINE_H + 4
        if thumb:
            with Image.open(thumb) as t:
                h += t.height + 6
        if used + h > MAX_H and kept:
            break
        kept.append((lines, thumb, h))
        used += h
    kept.reverse()

    img = Image.new("RGBA", (W, MAX_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = MAX_H - used                    # bottom-anchored on the fixed canvas
    # dark backing panel behind the whole column: the text was hard to read over the
    # bright sky and sea (SF 2026-09-02). Contrast carries it, no colour needed.
    if kept:
        top = max(0, y - 10)
        try:
            draw.rounded_rectangle((0, top, W - 1, MAX_H - 1), radius=10, fill=(0, 0, 0, 140))
        except Exception:
            draw.rectangle((0, top, W - 1, MAX_H - 1), fill=(0, 0, 0, 140))
        y += 4
    for lines, thumb, _h in kept:
        for ln in lines:
            draw.text((10, y), ln, font=font, fill=(255, 255, 255, 237),
                      stroke_width=2, stroke_fill=(0, 0, 0, 235))
            y += LINE_H
        if thumb:
            with Image.open(thumb) as t:
                t = t.convert("RGBA")
                img.paste(t, (24, y + 3), t)
                y += t.height + 6
        y += 4

    tmp = OUT + ".tmp.png"
    img.save(tmp)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)                 # old image stays; the feed never suffers
