#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_card.py (nr_videocard, 2026-10-15) -- the video's picture layer, drawn like the site's card.

SF 2026-10-14: "upgrade the ship picture to the other view anyway" (the site's card view) + ticker B "should not be
blocked by the player bar"; 2026-10-15: "yes build it plz". This draws ONE transparent 1280x720 PNG that ffmpeg lays
over the ship: the station mark + what is on air at the top left (like the site's card), the listener's shout-out
and the next request under it, and the chat column bottom left -- Telegram AND the Nostr live chat now (zaps leave
the chat: they ride the ticker band, drawn by render_ticker.py). The jukebox QR stays its own ffmpeg input.

It is rewritten only when what it shows changes. Any failure leaves the previous picture in place: the feed never
depends on this succeeding. The captain's brakes still work: /tmp/nr-chat-off hides the chat, /tmp/nr-media-off
hides chat pictures (text keeps flowing).

Usage: render_card.py --once|--loop <api_base> <out_png>
"""
import hashlib, io, json, os, re, subprocess, sys, threading, time, unicodedata, urllib.parse, urllib.request
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
FONT_DIR = os.environ.get("NR_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
LOGO = os.environ.get("NR_CARD_LOGO", "/srv/jukebox/site/assets/nr-logo-feed.png")
PRIV = os.environ.get("NR_BRIDGE_PRIV", os.path.expanduser("~/.cache/nr-bridge"))
THUMB_DIR = os.path.join(PRIV, "thumbs")
CHAT_OFF, MEDIA_OFF = "/tmp/nr-chat-off", "/tmp/nr-media-off"     # the captain's brakes (console + API use these)
FF = os.environ.get("NR_FFMPEG", "ffmpeg")
LOOP_S = 3

SIGNAL = (247, 147, 26, 255)      # the site's --signal (orange): names and the eyebrow -- never the only mark
FOAM = (196, 232, 236, 255)
WHITE = (255, 255, 255, 248)
SOFT = (214, 228, 232, 242)
STROKE = (0, 0, 0, 235)

LEFT = 51                          # 4 % of the width, as the site's card
LOGO_W = 88
WORDS_X = LEFT + LOGO_W + 28
WORDS_R = 996                      # the jukebox QR card starts at x = 1020
BAND_H = int(os.environ.get("NR_BAND_H", "40"))   # the ticker band runs along the very top (SF: "move the bar all the way
TOP = BAND_H + 22                                   # to the top"); the top block starts under it
CHAT_BOTTOM = 540                                   # clear of the site's player bar on a phone (it reaches up to ~y 560)
CHAT_W = 560
LINE_H = 25
THUMB_W, THUMB_MAX_H = 160, 120
# nr_labels (2026-10-29): a Great 78 record's own label, round, under the jukebox QR (the QR card: x 1020-1244, y 52-324).
# It ends at y 538, clear of a phone's player bar (~y 560), like the chat.
DISC_DIR = os.path.join(PRIV, "labels")
DISC_D = 200
DISC_X, DISC_Y = 1132 - DISC_D // 2, 338
_DISC_BUSY = set()
# nr_covers: a cover (Wavlake, the ship library) takes the same spot, SQUARE with soft corners; a label stays round
_ART_OK = re.compile(r"^/api/(label/78_[A-Za-z0-9._-]+|art/(w[0-9a-f-]{36}|l[0-9a-f]{24}))$")


def font(style, size):
    name = {"r": "DejaVuSansMono.ttf", "b": "DejaVuSansMono-Bold.ttf", "i": "DejaVuSansMono-Oblique.ttf"}[style]
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


# ---------------------------------------------------------------- text the font can really draw
_GLYPH = {}
_NOTDEF = {}


def has_glyph(f, ch):
    """True when the font has this character (emoji and the like would draw as empty boxes)."""
    key = (f.path, ch)
    if key not in _GLYPH:
        def mask(c):
            im = Image.new("L", (40, 40), 0)
            ImageDraw.Draw(im).text((4, 4), c, font=f, fill=255)
            return im.tobytes()
        if f.path not in _NOTDEF:
            _NOTDEF[f.path] = mask("\U000F0000")
        _GLYPH[key] = mask(ch) != _NOTDEF[f.path]
    return _GLYPH[key]


def clean(s, n, f=None):
    s = unicodedata.normalize("NFC", str(s or ""))
    f = f or font("r", 17)
    out = []
    for ch in s:
        if ch in "\r\n\t":
            out.append(" ")
        elif unicodedata.category(ch)[0] == "C":
            continue
        elif ch == " " or has_glyph(f, ch):
            out.append(ch)
    return " ".join("".join(out).split())[:n]


# ---------------------------------------------------------------- layout helpers
def fit_line(text, style, sizes, maxw):
    """The biggest size at which text fits on one line; at the smallest size, an ellipsis."""
    for s in sizes:
        f = font(style, s)
        if f.getlength(text) <= maxw:
            return f, text
    f = font(style, sizes[-1])
    while text and f.getlength(text + "…") > maxw:
        text = text[:-1]
    return f, text.rstrip() + "…"


def wrap(text, f, maxw, first_offset=0, indent=0):
    """Word wrap by pixel width. first_offset = room already used on line one (a name); indent for later lines."""
    lines, cur, room = [], "", maxw - first_offset
    for word in text.split(" "):
        test = (cur + " " + word) if cur else word
        if f.getlength(test) <= room:
            cur = test
            continue
        if cur:
            lines.append(cur)
            room = maxw - indent
        while f.getlength(word) > room:              # one very long word: hard cut
            cut = len(word)
            while cut > 1 and f.getlength(word[:cut]) > room:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
            room = maxw - indent
        cur = word
    if cur:
        lines.append(cur)
    return lines


def text(d, xy, s, f, fill):
    d.text(xy, s, font=f, fill=fill, stroke_width=2, stroke_fill=STROKE)


def scrim(img, bottom):
    """The site card's dark gradient behind the top block: 0.88 at the top, 0.62 halfway, nothing at the end."""
    g = Image.new("L", (1, bottom))
    for y in range(bottom):
        p = y / max(1, bottom - 1)
        a = 0.88 - (0.88 - 0.62) * (p / 0.52) if p < 0.52 else 0.62 * (1 - (p - 0.52) / 0.48)
        g.putpixel((0, y), int(255 * max(0.0, a)))
    layer = Image.new("RGBA", (W, bottom), (0, 0, 0, 255))
    layer.putalpha(g.resize((W, bottom)))
    img.alpha_composite(layer, (0, 0))


# ---------------------------------------------------------------- chat thumbnails (as render_chat.py did)
FETCH = {"on": True}               # --once (the bridge's start-up check) uses cached thumbnails only: a fast start


def thumb_for(api, fid):
    os.makedirs(THUMB_DIR, exist_ok=True)
    safe = "".join(c for c in fid if c.isalnum())[:48]
    png, bad = os.path.join(THUMB_DIR, safe + ".png"), os.path.join(THUMB_DIR, safe + ".bad")
    if os.path.exists(png):
        return png
    if os.path.exists(bad) or not FETCH["on"]:
        return None
    dl = os.path.join(THUMB_DIR, safe + ".dl")
    try:
        with urllib.request.urlopen(api + "/api/chat/media?file_id=" + urllib.parse.quote(fid), timeout=10) as r, \
                open(dl, "wb") as fh:
            fh.write(r.read())
        # -nostdin + stdin closed: an ffmpeg that sees a terminal tries to use it, and outside the terminal's own
        # process group Linux STOPS it (SIGTTOU) -- that froze the first test run on the box, 2026-10-15
        ok = subprocess.run([FF, "-nostdin", "-y", "-loglevel", "error", "-i", dl, "-frames:v", "1", "-vf",
                             "scale=w=%d:h=%d:force_original_aspect_ratio=decrease" % (THUMB_W, THUMB_MAX_H), png],
                            stdin=subprocess.DEVNULL, timeout=20).returncode == 0
    except Exception:
        ok = False
    try:
        os.remove(dl)
    except OSError:
        pass
    if ok and os.path.exists(png) and os.path.getsize(png) > 0:
        return png
    open(bad, "w").close()
    return None


def _disc_png(url):
    ident = url.rsplit("/", 1)[-1]
    return os.path.join(DISC_DIR, "".join(ch for ch in ident if ch.isalnum() or ch in "-_.")[:200] + ".png")


def _disc_make(api, url, png):
    """fetch the 400-px label from our own API, cut it round, ring it white, keep it (runs in the background)"""
    try:
        with urllib.request.urlopen(api + url + "?s=m", timeout=40) as r:
            data = r.read(4000000)
        big = DISC_D * 4                                 # drawn 4x and shrunk: a smooth edge
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            s = min(im.size)
            l, t = (im.width - s) // 2, (im.height - s) // 2
            im = im.crop((l, t, l + s, t + s)).resize((big, big), Image.LANCZOS).convert("RGBA")
        mask = Image.new("L", (big, big), 0)
        box = (8, 8, big - 9, big - 9)
        if url.startswith("/api/label/"):
            ImageDraw.Draw(mask).ellipse(box, fill=255)
            im.putalpha(mask)
            ImageDraw.Draw(im).ellipse(box, outline=(255, 255, 255, 235), width=10)
        else:                                            # nr_covers: square, soft corners
            ImageDraw.Draw(mask).rounded_rectangle(box, radius=40, fill=255)
            im.putalpha(mask)
            ImageDraw.Draw(im).rounded_rectangle(box, radius=40, outline=(255, 255, 255, 235), width=10)
        disc = im.resize((DISC_D, DISC_D), Image.LANCZOS)
        os.makedirs(DISC_DIR, exist_ok=True)
        tmp = png + ".tmp.png"
        disc.save(tmp)
        os.replace(tmp, png)
    except Exception as e:
        complain("label %s: %s" % (url[-60:], e))
        try:
            os.makedirs(DISC_DIR, exist_ok=True)
            open(png[:-4] + ".bad", "w").close()
        except OSError:
            pass
    finally:
        _DISC_BUSY.discard(png)


def disc_for(api, url):
    """the label picture when it is ready; else None now and a fetch in the background -- the card never waits"""
    if not api or not isinstance(url, str) or not _ART_OK.match(url):
        return None
    png = _disc_png(url)
    if os.path.exists(png):
        return png
    bad = png[:-4] + ".bad"
    if (os.path.exists(bad) and time.time() - os.path.getmtime(bad) < 3600) or not FETCH["on"] or png in _DISC_BUSY:
        return None
    _DISC_BUSY.add(png)
    threading.Thread(target=_disc_make, args=(api, url, png), daemon=True).start()
    return None


MEDIA_TAG = {"animation": "[GIF]", "photo": "[photo]", "sticker": "[sticker]"}


# ---------------------------------------------------------------- the picture
def render(np, queue, msgs, chat_off=False, media_off=False, api=None, logo=LOGO):
    """np/queue as /api/nowplaying, msgs as /api/chat/recent?src=all. Returns a 1280x720 RGBA image."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    maxw = WORDS_R - WORDS_X

    # --- the top block: what is on air, the shout-out, the next request
    title = clean(np.get("title"), 160, font("b", 30)) or "Noderunners Radio"
    artist = clean(np.get("artist"), 160)
    note = clean(np.get("note"), 280) if np.get("requested") else ""
    who = clean(np.get("who"), 24) if note else ""
    nxt = ""
    if queue:
        q0 = queue[0]
        nxt = "REQUESTED NEXT:  %s - %s" % (clean(q0.get("artist"), 40), clean(q0.get("title"), 60))
        if len(queue) > 1:
            nxt += "   (+%d more)" % (len(queue) - 1)
    plan, y = [], TOP
    fe = font("b", 15)
    plan.append(("eyebrow", y)); y += 24
    ft, title_s = fit_line(title, "b", (46, 42, 38, 34, 30, 27), maxw)
    plan.append(("title", y, ft, title_s)); y += ft.size + 12
    if artist:
        fa, artist_s = fit_line(artist, "r", (30, 27, 24, 21), maxw)
        plan.append(("artist", y, fa, artist_s)); y += fa.size + 10
    if note:
        body = "“%s”" % note + ("  — " + who if who else "")
        for s in (24, 21, 19, 17, 16):                   # SF: the font shrinks for long shout-outs (cap 280)
            fn = font("r", s)
            lines = wrap(body, fn, maxw - 26)
            if len(lines) <= 3 or s == 16:
                break
        lines = lines[:4]
        plan.append(("note", y + 6, fn, lines)); y += 6 + len(lines) * int(fn.size * 1.35) + 14 + 8
    if nxt:
        fx, nxt_s = fit_line(nxt, "r", (17, 15), maxw)
        plan.append(("next", y + 2, fx, nxt_s)); y += fx.size + 10
    top_bottom = max(y, TOP + 2 + LOGO_W) + 8
    scrim(img, min(H, top_bottom + 70))
    d = ImageDraw.Draw(img)
    try:
        with Image.open(logo) as lg:
            lg = lg.convert("RGBA").resize((LOGO_W, LOGO_W), Image.LANCZOS)
            img.alpha_composite(lg, (LEFT, TOP + 2))
    except Exception:
        pass
    for item in plan:
        if item[0] == "eyebrow":
            x = WORDS_X
            for ch in "NODERUNNERS FM":                  # letter-spaced, as the site's eyebrow
                d.text((x, item[1]), ch, font=fe, fill=SIGNAL, stroke_width=2, stroke_fill=STROKE)
                x += fe.getlength(ch) + 4
        elif item[0] == "title":
            text(d, (WORDS_X, item[1]), item[3], item[2], WHITE)
        elif item[0] == "artist":
            text(d, (WORDS_X, item[1]), item[3], item[2], FOAM)
        elif item[0] == "note":
            _, ny, fn, lines = item
            lh = int(fn.size * 1.35)
            h = len(lines) * lh + 14
            wmax = max(fn.getlength(l) for l in lines) + 26
            d.rounded_rectangle((WORDS_X, ny, WORDS_X + wmax, ny + h), radius=5, fill=(0, 0, 0, 150))
            d.rectangle((WORDS_X, ny, WORDS_X + 4, ny + h), fill=SIGNAL)           # the thick bar = "a listener's words"
            d.rectangle((WORDS_X + 4, ny, WORDS_X + wmax, ny + h), fill=(247, 147, 26, 34))
            for i, l in enumerate(lines):
                text(d, (WORDS_X + 16, ny + 7 + i * lh), l, fn, WHITE)
        elif item[0] == "next":
            text(d, (WORDS_X, item[1]), item[3], item[2], SOFT)

    # --- nr_labels: the record's own label under the QR
    dp = disc_for(api, np.get("disc") or np.get("cover"))
    if dp:
        try:
            with Image.open(dp) as dimg:
                img.alpha_composite(dimg.convert("RGBA"), (DISC_X, DISC_Y))
        except Exception as e:
            complain("label draw: %s" % e)

    # --- the chat column, bottom left, above the ticker band
    if chat_off or not msgs:
        return img, top_bottom
    fr, fb = font("r", 17), font("b", 17)
    blocks, extracted = [], 0
    for m in msgs[-14:]:
        if m.get("kind") == "zap":
            continue                                     # money rides the ticker band
        name = clean(m.get("username"), 18, fb) or "someone"
        what = clean(m.get("text"), 200)
        media = m.get("media") or {}
        fid, thumb = media.get("file_id"), None
        tag = MEDIA_TAG.get((media.get("kind") or "").lower(), "[media]" if media.get("kind") else "")
        if fid and not media_off and api:
            safe = "".join(c for c in fid if c.isalnum())[:48]
            if extracted < 2 or os.path.exists(os.path.join(THUMB_DIR, safe + ".png")):
                thumb = thumb_for(api, fid)
                extracted += 1
            if thumb is None and not FETCH["on"] and not os.path.exists(os.path.join(THUMB_DIR, safe + ".bad")):
                tag = tag or "[media]"                   # start-up picture: the tag now, the thumbnail on the next draw
        if fid and thumb is None and tag:
            what = (what + " " + tag).strip()
        if not what and not thumb:
            continue
        head = name + ":"
        lines = wrap(what, fr, CHAT_W - 20, first_offset=fb.getlength(head + " "), indent=24) if what else []
        if len(lines) > 3:
            lines = lines[:3]
            lines[2] = lines[2][:max(1, len(lines[2]) - 1)] + "…"
        blocks.append((head, lines, thumb))
    top_limit = max(top_bottom + 24, TOP + 130)
    budget = CHAT_BOTTOM - top_limit
    kept, used = [], 0
    for head, lines, thumb in reversed(blocks):
        h = max(1, len(lines)) * LINE_H + 4
        if thumb:
            with Image.open(thumb) as t:
                h += t.height + 6
        if used + h > budget - 14 and kept:
            break
        if used + h > budget - 14:
            continue
        kept.append((head, lines, thumb, h))
        used += h
    if not kept:
        return img, top_bottom
    kept.reverse()
    y0 = CHAT_BOTTOM - used - 14
    d.rounded_rectangle((LEFT, y0, LEFT + CHAT_W, CHAT_BOTTOM), radius=6, fill=(8, 18, 22, 165),
                        outline=(255, 255, 255, 30))
    y = y0 + 8
    for head, lines, thumb, _h in kept:
        text(d, (LEFT + 10, y), head, fb, SIGNAL)
        x1 = LEFT + 10 + fb.getlength(head + " ")
        for i, l in enumerate(lines):
            text(d, (x1 if i == 0 else LEFT + 10 + 24, y), l, fr, WHITE)
            y += LINE_H
        if not lines:
            y += LINE_H
        if thumb:
            with Image.open(thumb) as t:
                t = t.convert("RGBA")
                img.alpha_composite(t, (LEFT + 34, y + 2))
                y += t.height + 6
        y += 4
    return img, top_bottom


def get(api, path):
    with urllib.request.urlopen(api + path, timeout=4) as r:
        return json.load(r)


def save(img, out):
    tmp = out + ".tmp.png"
    img.save(tmp, compress_level=3)
    try:
        os.replace(tmp, out)                             # atomic: ffmpeg never reads half a file
    except OSError:
        os.remove(tmp)
        raise                                            # the loop tries again in 3 s; the old picture stays


_ERR = {"msg": "", "t": 0.0}


def complain(msg):
    """stderr, but the same complaint at most every 5 minutes (the loop runs for days)."""
    if msg != _ERR["msg"] or time.time() - _ERR["t"] > 300:
        print("render_card: %s" % msg[:200], file=sys.stderr, flush=True)
        _ERR.update(msg=msg, t=time.time())


def once(api, out, last_sig=None):
    """The API down = the station's name and no chat, never no picture."""
    try:
        npd = get(api, "/api/nowplaying")
    except Exception as e:
        complain("nowplaying: %s" % e)
        npd = {}
    chat_off = os.path.exists(CHAT_OFF)
    try:
        msgs = [] if chat_off else (get(api, "/api/chat/recent?src=all").get("messages") or [])
    except Exception as e:
        complain("chat: %s" % e)
        msgs = []
    media_off = os.path.exists(MEDIA_OFF)
    np, queue = npd.get("now_playing") or {}, npd.get("queue") or []
    art = np.get("disc") or np.get("cover")
    disc_ready = bool(isinstance(art, str) and _ART_OK.match(art)
                      and os.path.exists(_disc_png(art)))   # nr_labels / nr_covers: redraw once the picture has arrived
    sig = hashlib.sha256(json.dumps([np, queue[:2], len(queue), msgs[-14:], chat_off, media_off, disc_ready],
                                    sort_keys=True, default=str).encode()).hexdigest()
    if sig == last_sig and os.path.exists(out):
        return sig
    img, _ = render(np, queue, msgs, chat_off, media_off, api)
    save(img, out)
    return sig


def main(argv):
    if len(argv) != 3 or argv[0] not in ("--once", "--loop"):
        print(__doc__)
        return 64
    mode, api, out = argv
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if mode == "--once":
        FETCH["on"] = False
        once(api, out)
        return 0
    sig = None
    while True:
        try:
            sig = once(api, out, sig)
        except Exception as e:
            complain(str(e))
        time.sleep(LOOP_S)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print("render_card: %s" % e, file=sys.stderr)
        sys.exit(1)                                      # the old picture stays; the feed never suffers
