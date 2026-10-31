#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_clock.py (nr_vclock, 2026-10-30) -- the video's time-left layer.

SF 2026-10-30: "maybe we need the countdown timer in the video feed too" (the site got it the same day, nr-timeleft).
This draws ONE transparent 1280x34 PNG that ffmpeg lays over the card at y CLOCK_Y (bridge-run.sh): on the eyebrow
line, right of "NODERUNNERS FM", a thin bar that fills (light on dark) and the time left, "-2:41". Shape and motion
carry it, not colour. Same numbers as the site: started + duration_s from /api/nowplaying, on the server's clock.

It is rewritten once a second while a song with a known length plays. Unknown start or length, past the end, or the
captain's brake /tmp/nr-clock-off -> an EMPTY layer. Any failure leaves the previous picture in place for a moment and
then empties it: the feed never depends on this succeeding.

Usage: render_clock.py --once|--loop <api_base> <out_png>
"""
import json, os, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_card as rc                                # the card's fonts, colours and eyebrow place -- one look
from PIL import Image, ImageDraw

LH = 34                                                 # the layer's height
CLOCK_Y = rc.TOP - 6                                    # where the bridge lays it (must match bridge-run.sh CLOCK_Y)
OFF = "/tmp/nr-clock-off"
BAR_W, BAR_H = 150, 5
FETCH_S = 2.0                                           # ask the API every 2 s; count the seconds in between


def eyebrow_end():
    fe = rc.font("b", 15)
    return rc.WORDS_X + sum(fe.getlength(ch) + 4 for ch in "NODERUNNERS FM")


def set_words(st):
    """nr_sets3 (2026-10-31, SF: the set countdown in the video feed too): the listener set's line, or ""."""
    if not st:
        return ""
    if st.get("paused"):
        return "LISTENER SET PAUSED"
    s = max(0, int(st.get("left") or 0))
    t = ("%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)
    if st.get("i"):
        return "LISTENER SET %d/%d · ENDS IN %s" % (st["i"], st["n"], t)
    return "LISTENER SET NEXT · %d SONGS · %s LONG" % (st["n"], t)


def draw(left, dur, st=None):
    """left/dur in seconds (or None: no song clock); st = the listener set's state (or None)."""
    img = Image.new("RGBA", (rc.W, LH), (0, 0, 0, 0))
    words = set_words(st)
    if left is None and not words:
        return img
    d = ImageDraw.Draw(img)
    x = int(eyebrow_end()) + 26
    y = (rc.TOP - CLOCK_Y) + 5                          # the eyebrow's own line
    if left is not None:
        d.rounded_rectangle((x - 2, y - 2, x + BAR_W + 2, y + BAR_H + 2), radius=3, fill=(0, 0, 0, 170))
        d.rounded_rectangle((x, y, x + BAR_W, y + BAR_H), radius=2, fill=(255, 255, 255, 60))
        done = int(round(BAR_W * (1 - left / dur)))
        if done > 0:
            d.rounded_rectangle((x, y, x + done, y + BAR_H), radius=2, fill=rc.FOAM)
        s = int(left + 0.999)
        tf = rc.font("b", 17)
        tt = "−%d:%02d" % (s // 60, s % 60)
        rc.text(d, (x + BAR_W + 12, y - 7), tt, tf, rc.WHITE)
        x = x + BAR_W + 12 + int(tf.getlength(tt)) + 26
    if words:
        # nr_sets3: shape carries it -- a thick white rule, then bold capitals, on a dark backing so any picture reads
        sf = rc.font("b", 15)
        w = int(sf.getlength(words))
        d.rounded_rectangle((x - 4, y - 8, x + 12 + w + 6, y + 13), radius=3, fill=(0, 0, 0, 170))
        d.rectangle((x, y - 5, x + 3, y + 10), fill=rc.WHITE)
        rc.text(d, (x + 12, y - 6), words, sf, rc.WHITE)
    return img


class Clock:
    def __init__(self, api):
        self.api, self.t, self.np, self.st = api, 0.0, None, None

    def set_state(self):
        """nr_sets3: {i, n, left (s), paused} of the listener set, counted down between fetches, or None."""
        if self.st is None or os.path.exists(OFF):
            return None
        s = dict(self.st)
        s["left"] = max(0, s["end"] - time.time()) if not s.get("paused") else 0
        return s

    def left(self):
        """(left, dur) in seconds, or None."""
        now = time.time()
        if now - self.t >= FETCH_S:
            self.t = now
            try:
                with urllib.request.urlopen(self.api + "/api/nowplaying", timeout=4) as r:
                    d = json.load(r)
                np = d.get("now_playing") or {}
                st, du, sv = float(np.get("started") or 0), float(np.get("duration_s") or 0), float(d.get("server_time") or 0)
                self.np = (st + du, du, sv - time.time()) if st > 0 and du > 0 and sv > 0 else None
                ss = d.get("set")                       # nr_sets3: the listener set, if one holds the air
                self.st = ({"i": int(ss.get("i") or 0), "n": int(ss.get("n") or 0), "paused": bool(ss.get("paused")),
                            "end": time.time() + float(ss.get("left_s") or 0)} if isinstance(ss, dict) and ss.get("n") else None)
            except Exception as e:
                rc.complain("clock: %s" % e)
                self.np, self.st = None, None
        if self.np is None or os.path.exists(OFF):
            return None
        end, dur, skew = self.np
        left = end - (time.time() + skew)
        if left < 0 or left > dur + 5:
            return None
        return min(left, dur), dur


def main(argv):
    if len(argv) != 3 or argv[0] not in ("--once", "--loop"):
        print(__doc__)
        return 64
    mode, api, out = argv
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    clock, last = Clock(api), "start"
    while True:
        try:
            v = clock.left()
            sst = clock.set_state()                     # nr_sets3
            words = set_words(sst)
            key = (None if v is None else (int(v[0] + 0.999), int(round(BAR_W * (1 - v[0] / v[1])))), words)
            if key != last or not os.path.exists(out):
                rc.save(draw(v[0], v[1], sst) if v else draw(None, None, sst), out)
                last = key
        except Exception as e:
            rc.complain("clock draw: %s" % e)
        if mode == "--once":
            return 0
        time.sleep(0.25)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
