#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_ticker.py (nr_videocard, 2026-10-15) -- the ticker band on the video: every donation, zap and live tip, with
its message, runs right to left along a band at the top of the picture.

SF 2026-10-14 chose sketch B ("a ticker band, lines run right to left, one after another") -- "B is good but it should
not be blocked by the player bar"; 2026-10-15: "every shoutout gets screen time everywhere, especially when folks make
a donation" + "let's try and funnel folks to donate as much as possible".

WHAT IT SHOWS
  New money (from the station API's /api/donate/recent: site donations, Lightning-address payments, Nostr zaps, live
  tips) enters first, marked NEW, and passes by 1x (under 100 sats), 2x (under 1,000) or 3x (1,000 and more).
  Between new ones, the band loops the last five, then the call to donate. It is never empty.
SAFETY
  Names and messages go through the announcer's word list (a listed word: the message goes, the name becomes
  "someone"). If the word list cannot be loaded, NO names and NO messages are shown, only amounts.
  /tmp/nr-ticker-off exists -> the band leaves the picture within a second (the captain's brake, like nr-chat-off).
HOW (nr_tickertape, 2026-10-15)
  ffmpeg moves the band itself, from its own frame clock (overlay x = -mod(t*96, 12288)): exactly 4 px a frame at
  24 fps, so it glides. This program only paints a long ring 'tape' (band-tape.png) AHEAD of the visible window,
  about once a second, plus the still strip (band-bg.png). If it stops, the band keeps gliding over
  what was painted and then runs empty -- the feed never depends on it.
  The band sits at the very TOP of the picture (SF: "move the bar all the way to the top"): no player bar reaches it.

Usage: render_ticker.py --once|--loop <api_base> <tape_png>
"""
import importlib.machinery, importlib.util, json, os, sys, threading, time, unicodedata, urllib.request
from PIL import Image, ImageDraw, ImageFont

W, BAND_H = 1280, 40
FONT_DIR = os.environ.get("NR_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
PRIV = os.environ.get("NR_BRIDGE_PRIV", os.path.expanduser("~/.cache/nr-bridge"))
STATE = os.path.join(PRIV, "ticker-state.json")
ANN_PATH = os.environ.get("NR_ANNOUNCER_PATH", "/usr/local/bin/nr-announcer")
OFF = os.environ.get("NR_TICKER_OFF", "/tmp/nr-ticker-off")
SPEED = 96.0                      # pixels a second = exactly 4 a frame at 24 fps (even steps); 280 characters in ~40 s
POLL_S = 5
IDLE_N = 5
GAP = 70
CTA = "Keep the ship afloat: donate or play a song at noderunnersradio.com"

SIGNAL = (247, 147, 26, 255)
WHITE = (255, 255, 255, 250)
GREY = (170, 178, 182, 255)
STROKE = (0, 0, 0, 230)


def font(style, size):
    name = {"r": "DejaVuSansMono.ttf", "b": "DejaVuSansMono-Bold.ttf"}[style]
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


FR, FB, FS = font("r", 21), font("b", 21), font("b", 13)
_GLYPH, _NOTDEF = {}, {}


def has_glyph(f, ch):
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


def drawable(s, n):
    s = unicodedata.normalize("NFC", str(s or ""))
    out = [" " if ch in "\r\n\t" else ch for ch in s
           if ch in "\r\n\t" or (unicodedata.category(ch)[0] != "C" and (ch == " " or has_glyph(FR, ch)))]
    return " ".join("".join(out).split())[:n]


# ---------------------------------------------------------------- the word list (one set of rules everywhere)
def load_ann():
    try:
        sys.dont_write_bytecode = True
        loader = importlib.machinery.SourceFileLoader("nr_announcer_rules", ANN_PATH)
        spec = importlib.util.spec_from_loader("nr_announcer_rules", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.load_blocklist()
        return mod
    except Exception as e:
        print("render_ticker: word list unavailable (%s) -- amounts only" % str(e)[:120], file=sys.stderr, flush=True)
        return None


def safe_item(it, ann):
    """(name, message) cleared for the broadcast, or ('', '') in amounts-only mode."""
    if ann is None:
        return "", ""
    try:
        bl = ann.load_blocklist()
        name = ann.clean(it.get("name"), 40)
        msg = ann.clean(it.get("message"), 280)
        if name and ann.blocked(name, bl):
            name = ""
        if msg and ann.blocked(msg, bl):
            msg = ""
        return drawable(name, 40), drawable(msg, 280)
    except Exception:
        return "", ""


# ---------------------------------------------------------------- segments
def bolt(d, x, y, h, fill):
    w = h * 0.62
    pts = [(x + w * 0.62, y), (x, y + h * 0.56), (x + w * 0.45, y + h * 0.56), (x + w * 0.30, y + h),
           (x + w, y + h * 0.40), (x + w * 0.55, y + h * 0.40), (x + w * 0.80, y)]
    d.polygon(pts, fill=fill, outline=(0, 0, 0, 255))
    return w


def segment(item, ann, new=False):
    """One money line as an image strip: [NEW] bolt 2,100 sats  name: message   -- then the gap."""
    ty = (BAND_H - 25) // 2
    if item is None:
        parts = [("bolt",), ("t", CTA, FB, WHITE)]
    else:
        name, msg = safe_item(item, ann)
        parts = []
        if new:
            parts.append(("new",))
        parts += [("bolt",), ("t", "{:,} sats".format(int(item.get("sats") or 0)), FB, WHITE),
                  ("t", "   " + (name or "someone"), FB, SIGNAL)]
        parts.append(("t", (": " + msg) if msg else "  — thank you!", FR, WHITE))
    width = 0
    for p in parts:
        width += {"new": 56, "bolt": 22}.get(p[0], 0) or int(p[2].getlength(p[1]))
    width += GAP
    im = Image.new("RGBA", (width, BAND_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 0
    for p in parts:
        if p[0] == "new":
            d.rounded_rectangle((x, 9, x + 46, BAND_H - 9), radius=4, fill=WHITE)      # a solid white box = NEW
            d.text((x + 8, 12), "NEW", font=FS, fill=(0, 0, 0, 255))
            x += 56
        elif p[0] == "bolt":
            bolt(d, x + 2, 9, BAND_H - 18, WHITE)
            x += 22
        else:
            d.text((x, ty), p[1], font=p[2], fill=p[3], stroke_width=2, stroke_fill=STROKE)
            x += int(p[2].getlength(p[1]))
    sx = width - GAP // 2 - 5
    d.rectangle((sx, BAND_H // 2 - 4, sx + 8, BAND_H // 2 + 4), fill=GREY)               # the separator square
    return im


# ---------------------------------------------------------------- the feed
def key_of(it):
    return "%s|%s|%s" % (it.get("ts"), it.get("sats"), it.get("name"))


def is_spam(it):
    """Satograms: 1-2 sat keysend adverts ('\U0001F4E8 Satogram: ...'). SF 2026-10-15: "we need to filter those out"."""
    m = str(it.get("message") or "")
    return "satogram" in m.lower() or "\U0001F4E8" in m


def passes_for(sats):
    return 1 if sats < 100 else (2 if sats < 1000 else 3)


class Feed:
    def __init__(self, api):
        self.api, self.items, self.lock = api, [], threading.Lock()
        try:
            with open(STATE, encoding="utf-8") as fh:
                self.state = json.load(fh)
        except Exception:
            self.state = None                         # first run: what is there now is history, not news
        self.idle_i = 0

    def fetch(self):
        with urllib.request.urlopen(self.api + "/api/donate/recent", timeout=4) as r:
            items = [x for x in (json.load(r).get("donations") or []) if int(x.get("sats") or 0) > 0 and not is_spam(x)]
        with self.lock:
            self.items = items
            if self.state is None:
                self.state = {"left": {key_of(x): 0 for x in items}}
            left = self.state.setdefault("left", {})
            for x in items:
                if key_of(x) not in left:
                    left[key_of(x)] = passes_for(int(x.get("sats") or 0))
            keep = {key_of(x) for x in items}
            self.state["left"] = {k: v for k, v in left.items() if k in keep or v > 0}
            self.save()

    def save(self):
        try:
            os.makedirs(PRIV, exist_ok=True)
            tmp = STATE + ".new"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, STATE)
        except Exception:
            pass

    def poller(self):
        last, when = "", 0.0
        while True:
            try:
                self.fetch()
            except Exception as e:
                msg = str(e)[:160]
                if msg != last or time.time() - when > 300:      # the same complaint at most every 5 minutes
                    print("render_ticker: %s" % msg, file=sys.stderr, flush=True)
                    last, when = msg, time.time()
            time.sleep(POLL_S)

    def next(self):
        """(item, new): new money first (oldest new first), else the idle loop: the last five, then the call to donate."""
        with self.lock:
            left = (self.state or {}).get("left", {})
            fresh = [x for x in reversed(self.items) if left.get(key_of(x), 0) > 0]
            if fresh:
                x = fresh[0]
                left[key_of(x)] -= 1
                self.save()
                return x, True
            idle = self.items[:IDLE_N][::-1] + [None]
            it = idle[self.idle_i % len(idle)]
            self.idle_i += 1
            return it, False


def band_bg():
    """The band's still part, drawn once: the dark strip with a thin line along its bottom edge (it sits at the top of
    the picture now). No arrow block (SF: "get rid of the arrow on the left hand side")."""
    bg = Image.new("RGBA", (W, BAND_H), (0, 0, 0, 188))
    ImageDraw.Draw(bg).line((0, BAND_H - 1, W, BAND_H - 1), fill=(255, 255, 255, 70))
    return bg


def save(im, out):
    """Atomic swap. A swap that fails (a reader holding the file, a full disk) skips this round, never the program."""
    tmp = out + ".tmp.png"
    try:
        im.save(tmp, compress_level=1)
        os.replace(tmp, out)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- the tape (nr_tickertape, 2026-10-15)
# SF: "it still looks jerky/stuttering while it scrolls... can she do better?" -- yes. The stutter came from two clocks:
# this program drew whole band pictures and ffmpeg read them in bursts. Now FFMPEG moves the band itself, from its own
# frame clock: overlay x = -mod(t*SPEED, TAPE_L) -- exactly 4 px a frame at 24 fps, every frame. This program only
# paints the tape (a ring of TAPE_L px, plus a copy of its first W px at the end so the window never tears), always
# AHEAD of what is on screen, and never inside the visible window.
TAPE_L = 12288                     # one lap = 128 s at 96 px/s
AHEAD = 8 * SPEED                  # paint at least 8 s beyond the right edge (ffmpeg's real t can run ~3 s off the estimate)
FILL = 8 * SPEED                   # and keep ~8 s of band painted beyond that: a new gift shows within ~15 s


class Tape:
    def __init__(self):
        self.ring = Image.new("RGBA", (TAPE_L, BAND_H), (0, 0, 0, 0))
        self.cursor = None             # where the next segment starts, in band pixels since ffmpeg's t = 0

    def _put(self, im, c):
        r = int(c) % TAPE_L
        self.ring.paste(im, (r, 0))
        if r + im.width > TAPE_L:
            self.ring.paste(im, (r - TAPE_L, 0))      # the part that runs past the end goes to the start

    def clear(self, c, w):
        self._put(Image.new("RGBA", (int(w), BAND_H), (0, 0, 0, 0)), c)

    def put(self, seg, c):
        self.clear(c, seg.width)
        self._put(seg, c)

    def image(self):
        im = Image.new("RGBA", (TAPE_L + W, BAND_H), (0, 0, 0, 0))
        im.paste(self.ring, (0, 0))
        im.paste(self.ring.crop((0, 0, W, BAND_H)), (TAPE_L, 0))
        return im

    # The start-up step (--once) paints the first gifts at the right edge BEFORE ffmpeg starts; the loop carries on from
    # its tape and cursor, so the band is never empty after a restart.
    def save_state(self, out, idle_i):
        try:
            with open(out + ".state", "w", encoding="utf-8") as fh:
                json.dump({"cursor": self.cursor, "idle_i": idle_i, "at": time.time()}, fh)
        except OSError:
            pass

    @classmethod
    def resume(cls, out):
        """The start-up step's tape, if it is fresh (under 2 minutes) -- else None."""
        try:
            with open(out + ".state", encoding="utf-8") as fh:
                st = json.load(fh)
            if time.time() - float(st["at"]) > 120 or st.get("cursor") is None:
                return None, 0
            t = cls()
            with Image.open(out) as im:
                t.ring.paste(im.convert("RGBA").crop((0, 0, TAPE_L, BAND_H)), (0, 0))
            t.cursor = float(st["cursor"])
            os.remove(out + ".state")                     # used once
            return t, int(st.get("idle_i") or 0)
        except Exception:
            return None, 0


def read_t0(path):
    """The wall time at which the bridge started ffmpeg (its t = 0), or None while it has not started."""
    try:
        with open(path, encoding="utf-8") as fh:
            return float(fh.read().strip())
    except Exception:
        return None


def paint(tape, feed, ann, t0, now):
    """Paint the tape ahead of the visible window. Returns True when something changed."""
    pos = SPEED * max(0.0, now - t0) if t0 else 0.0          # band pixels already scrolled past the left edge
    if tape.cursor is None:
        tape.cursor = float(W) if not t0 else pos + W + AHEAD    # before ffmpeg starts, the first one waits at the edge
    if t0 and tape.cursor < pos + W + AHEAD:                   # fell behind (a pause, a slow start): jump ahead, never onto the screen
        tape.cursor = pos + W + AHEAD
    changed = False
    while tape.cursor < pos + W + AHEAD + FILL:
        item, new = feed.next()
        seg = segment(item, ann, new)
        tape.put(seg, tape.cursor)
        tape.cursor += seg.width
        changed = True
    if changed:
        tape.clear(tape.cursor, 2000)        # an old lap must never come back if this program stops
    return changed


def run(api, out, rounds=None, feed=None, ann=None, now=time.time, sleep=time.sleep, t0_path=None):
    feed = feed or Feed(api)
    ann = ann if ann is not None else load_ann()
    d = os.path.dirname(out) or "."
    t0_path = t0_path or os.path.join(d, "t0")
    bg_path = os.path.join(d, "band-bg.png")
    if rounds is None:
        threading.Thread(target=feed.poller, daemon=True).start()
    tape, idle_i = Tape.resume(out)
    if tape is None:
        tape = Tape()
    else:
        feed.idle_i = idle_i                               # carry on after the gifts the start-up step painted
    n, off_state = 0, None
    bg = band_bg()
    while rounds is None or n < rounds:
        n += 1
        off = os.path.exists(OFF)
        if off != off_state:                               # the brake: both band layers transparent, or back again
            if off:
                save(Image.new("RGBA", (TAPE_L + W, BAND_H), (0, 0, 0, 0)), out)
                save(Image.new("RGBA", (W, BAND_H), (0, 0, 0, 0)), bg_path)
            else:
                save(bg, bg_path)
                if off_state:                              # back from the brake: start fresh, ahead of the screen
                    tape = Tape()
            off_state = off
        if not off and paint(tape, feed, ann, read_t0(t0_path), now()):
            save(tape.image(), out)
        sleep(1.0)
    return tape


def main(argv):
    if "--fps" in argv:                                    # accepted for the old command line; ffmpeg sets the pace now
        i = argv.index("--fps")
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 3 or argv[0] not in ("--once", "--loop"):
        print(__doc__)
        return 64
    mode, api, out = argv
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if mode == "--once":                                   # the start-up step: the still strip + the first gifts at the edge
        d = os.path.dirname(out) or "."
        ok = save(band_bg(), os.path.join(d, "band-bg.png"))
        feed, tape = Feed(api), Tape()
        try:
            feed.fetch()
        except Exception:
            pass                                           # the station API is down: the call to donate only
        if os.path.exists(OFF):
            ok = save(Image.new("RGBA", (TAPE_L + W, BAND_H), (0, 0, 0, 0)), out) and ok
        else:
            paint(tape, feed, load_ann(), None, time.time())
            ok = save(tape.image(), out) and ok
            tape.save_state(out, feed.idle_i)
        return 0 if ok else 1
    run(api, out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print("render_ticker: %s" % e, file=sys.stderr)
        sys.exit(1)                                      # the band stops being painted; the feed never suffers
