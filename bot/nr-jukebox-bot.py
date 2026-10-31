#!/usr/bin/env python3
"""
nr-jukebox-bot.py -- the Noderunners Radio jukebox bot for Telegram.

WHAT THIS IS
    A thin Telegram front-end over the station's existing HTTP API. You search
    the library, you tap a track, the STATION mints a Lightning invoice, you pay
    it from your own wallet, the track goes in the queue.

WHAT THIS DELIBERATELY IS NOT -- read this before "improving" it
    It holds NO custodial balance. There is no top-up, no account, no wallet,
    no LNbits key anywhere in this file. Sats never pass through this process.

    That is not an oversight, it is the whole point. The bot this replaces kept
    a balance per Telegram user on infrastructure the station did not control.
    When its maintainer became unreachable, every user's sats became
    unreachable with them. Nobody could move that money -- not the station, not
    the users. It is being switched off by 1 November 2026 and the balances go
    with it.

    So: if a future change would make this bot hold, owe, or spend somebody
    else's sats, that change is wrong. Say no. The correct answer to "can you
    store my balance" is "no, and here is why."

    The entire secret inventory of this process is ONE Telegram bot token. If
    this bot is completely compromised, an attacker gets to send messages. They
    do not get anyone's money, because there is none here to get.

TWO LANES
    The station's own catalogue at 21 sats, covered by the BUMA/STEMRA/Sena
    webcast licence. And the Wavlake value-for-value lane at 210 sats, where 206
    go straight to the artist's own Lightning address -- paid by the station's
    passthrough wallet, never by this process.

    Neither number is a price. 21 sats is about one eurocent: it is anti-spam
    friction, the same job a captcha does, so the queue cannot be flooded. 210
    sats is value-for-value, and almost all of it belongs to the artist. This is
    a hobby station. Nobody here is selling anything.

    THE V4V LANE IS OFF BY DEFAULT -- set NR_V4V_ENABLED=1 to turn it on.
    That default is deliberate. Wavlake is an open upload platform, so anything
    reachable from a Telegram DM for ten cents can put a stranger's audio, and a
    stranger's chosen artist and title text, on a public broadcast. The guard is
    NR_ADMIN_CHAT_ID: every V4V queue is announced there immediately, so it is
    seen rather than discovered. Turn the lane on only with that set.

RUNTIME
    Python 3.12, standard library + requests. No Telegram framework: the Bot API
    is plain HTTPS and a framework would be a dependency, a supply chain and a
    second way to hold state. Matches the house style of library_api.py.

    Run as its own systemd unit, as its own unprivileged user, with the token in
    an EnvironmentFile at mode 0600. Never pass the token as an argument -- this
    file refuses to start if it finds the token in its own command line.
"""

import base64
import binascii
import heapq
from datetime import datetime
from zoneinfo import ZoneInfo
import html
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
from urllib.parse import quote

import requests

# --------------------------------------------------------------------------
# configuration -- everything from the environment, nothing on the command line
# --------------------------------------------------------------------------
TOKEN = os.environ.get("NR_JUKEBOX_BOT_TOKEN", "").strip()
API_BASE = os.environ.get("NR_API_BASE", "http://127.0.0.1:7100").rstrip("/")
SITE = os.environ.get("NR_SITE", "https://noderunnersradio.com").rstrip("/")

# Where to echo every queued track. Telegram becomes the audit log: who put what
# on the public airwaves. Empty = no echo (works, but you fly blind).
ADMIN_CHAT = os.environ.get("NR_ADMIN_CHAT_ID", "").strip()
GROUP_CHAT = os.environ.get("NR_GROUP_CHAT_ID", "").strip()   # nr_groupping: the public group, for money-stop pings only

# Bare text (no slash) is treated as a search. In a DM that is friendly. In a
# group it would hijack every conversation, so groups need an explicit /play.
GROUP_NEEDS_COMMAND = os.environ.get("NR_GROUP_NEEDS_COMMAND", "1") == "1"

# --- bounds. Every one of these is a real attack the review found. -----------
MAX_QUERY_CHARS = 60         # unbounded q amplifies into the Wavlake cache
NOTE_MAX = 80                # the station's own cap (library_api NOTE_MAX); longer is trimmed, never refused
# nr_full (2026-09-09, SF): "Keep the character limit for everyone else but me." The captain's own two
# accounts (GIFT_USERS) may write this much instead. It MUST match library_api's NOTE_MAX_FULL, or the
# station trims what this bot let through. Every cut below is made by SENDER, never by hope.
NOTE_MAX_FULL = 160
NOTE_LATE_S = 900            # how long a paid-and-gone invoice is remembered, so a late line gets a real answer
SEARCH_COOLDOWN_S = 3.0      # per user, stops search flooding
MAX_ROWS = 6                 # search rows shown at once -- ONE PAGE
# nr_page (2026-09-10, SF: "I want that previous and next button"): how many rows we FETCH and REMEMBER,
# so paging needs no second API call. 24 is the station's own ceiling (SEARCH_LIMIT_MAX).
MAX_ROWS_KEPT = 24
# nr_deep (2026-09-10, SF: "I see only 24 results while there are loads more dire straits songs
# available. I want to be able to next and previous scroll through them all"): 24 is one BATCH, not
# the end of the road. Tapping Next past the last remembered row asks the station for the next batch
# and appends it, so paging walks the whole result set. MAX_ROWS_DEEP is where that walk stops: 482
# is every Dire Straits row in the catalogue, so 600 covers the case that started this and still
# bounds a query that matches half the library.
MAX_ROWS_DEEP = 600
MAX_ROWS_TOTAL = 4000        # hard ceiling on remembered rows across ALL users. Was 400 when a search
                             # kept 6 rows, then 1600 at 24 kept rows. ONE fully-walked search can now
                             # reach 600 rows by itself, so this is ~6 of those or ~160 ordinary ones.
MAX_TRACKED = 50             # hard cap on watched invoices, station-wide
# nr_live (2026-09-10, SF: "what happens if a user does not pay but keeps selecting songs and goes
# back after the selection was made without paying"): the Back button releases the hold, which was
# the ONLY thing bounding how many invoices one person could mint -- tap, Back, tap, Back, forever,
# every one a real invoice on the node. Three at a time. Enough to undo a mis-tap twice over, which
# is what Back is for, and not enough to paper the group with QR codes.
MAX_OPEN_INVOICES = 3        # live, unpaid invoices one person may hold at once
STALE_UPDATE_S = 60          # never act on a command Telegram redelivers late
INVOICE_TTL_S = 900          # mirrors the server's PENDING_PLAY_TTL_S
ROW_TTL_S = 3600             # search rows we remember so a tap can find them
QUEUE_FULL_AT = 12           # refuse to mint when the deck is already stacked
STATUS_TIMEOUT = 20          # cap for /api/play/status. NEVER on the update loop,
                             # and NEVER 100 -- that froze the whole bot for 100s.

# The value-for-value lane. 210 sats, and 206 of them go straight to the artist
# from the station's own passthrough wallet -- the bot never touches the money.
#
# It is OFF by default and that is deliberate. Wavlake is an open upload
# platform, so a stranger can upload their own audio, with their own chosen
# artist and title text, and put it on a public broadcast for about ten cents.
# Turning this on means accepting that. The guard is NR_ADMIN_CHAT_ID: every
# V4V queue is announced there with the requester's handle, so it is visible
# immediately rather than discovered later.
V4V_ENABLED = os.environ.get("NR_V4V_ENABLED", "0") == "1"

# --- pinned now-playing card + chat hygiene (all optional, all off by default) -
# The bot keeps ONE pinned message in HOME_CHAT, edited in place with whatever is
# on air. A standing notice (e.g. "withdraw from the old bot") rides at the top of
# that same card, so there is only ever one pin to manage. Empty HOME_CHAT = the
# card is off and none of this runs.
HOME_CHAT = os.environ.get("NR_HOME_CHAT_ID", "").strip()
# v15/v18 (SF 2026-09-02): "someone added music" is announced in ONE place -- the community
# GROUP (HOME_CHAT, t.me/noderunnersradio) unless NR_ANNOUNCE_CHAT names another chat. At
# startup the bot checks it can see that chat; otherwise announcements stay off and it says
# so once in the home group. Posts are silent (no push notification) and are never tidied.
ANNOUNCE_CHAT = os.environ.get("NR_ANNOUNCE_CHAT", "").strip() or HOME_CHAT
ANNOUNCE_SEED_S = 60          # after a start: only remember what is already there
ANNOUNCE_FORGET_S = 900       # forget a request 15 min after it was last seen
CARD_POLL_S = max(5, int(os.environ.get("NR_CARD_POLL_S", "20") or "20"))  # >=5s
# The default notice line. Set it in the env so it survives a restart. /notice
# overrides it live; /notice off hides it. Empty = no line unless set at runtime.
CARD_NOTICE = os.environ.get("NR_CARD_NOTICE", "").strip()

# Self-tidy: the bot deletes its OWN old group messages after this many seconds,
# so the group does not fill with jukebox chatter. It NEVER deletes the pinned
# card, a payment receipt, or anything in a private chat. 0 = never tidy.
# Who may /gift: fixed name list, SF's ruling 2026-08-14 ("restricted to
# t.me/noderunnersfm and t.me/plebroyale"). Sturdier than a live admin lookup,
# which cost SF a rejection when Telegram answered slowly.
GIFT_USERS = {u.strip().lower().lstrip("@")
              for u in os.environ.get("NR_GIFT_USERS",
                                      "plebroyale,noderunnersfm").split(",")
              if u.strip()}


def _note_cap(user) -> int:
    """How long a line this person may write. The captain's own two accounts -- the same list that may
    /gift -- get the longer one (SF 2026-09-09). A missing username simply gets the ordinary cap."""
    return NOTE_MAX_FULL if ((user or {}).get("username") or "").lower().lstrip("@") in GIFT_USERS else NOTE_MAX


def _tglen(s: str) -> int:
    """What TELEGRAM counts, which is UTF-16 units, not Python characters. An emoji weighs 2 there and
    1 here, so counting with len() under-counts an emoji-heavy line and a "safe" caption gets refused."""
    return len(str(s).encode("utf-16-le")) // 2


def _fit(txt: str, limit: int = 3800) -> str:
    """Telegram REFUSES a message over 4096 characters, and a refused message is a break, not a
    truncation. Cut on a LINE boundary so an HTML tag is never cut in half -- a half tag is refused
    too, which is the very thing we are avoiding. With no line break to cut on, cut before the first
    tag instead, which is always safe."""
    if _tglen(txt) <= limit:
        return txt
    # cut by TELEGRAM's budget, not by Python index: an emoji is 1 character here and 2 units there, so
    # slicing at [:limit] can still hand back twice the limit and get the message refused.
    head = txt[:limit]
    while head and _tglen(head) > limit:
        n = len(head) * limit // _tglen(head)
        head = head[:n if 0 < n < len(head) else len(head) - 1]
    cut = head.rsplit("\n", 1)[0] if "\n" in head else head.split("<")[0]
    return cut + "\n…"


MSG_TTL_S = int(os.environ.get("NR_BOT_MSG_TTL_S", "180") or "180")
LIST_TTL_S = 600        # a LIVE search list lingers longer -- it invites a tap
DEAD_TTL_S = 60         # a cancelled/stale list is furniture: swept in a minute
RECEIPT_GRACE_S = 180   # an EXPIRED invoice notice lingers this long, then goes
MAX_EXPIRE = 800        # hard ceiling on remembered message ids, like MAX_ROWS_TOTAL

LOG = logging.getLogger("nr-jukebox-bot")

# The house button row (SF 2026-08-14: "all the other cards should also offer
# the same three buttons"). Listen opens the site; Queue/History answer in
# chat and tidy themselves away.
def _btns():
    return [[("🎧 Listen", "url:" + SITE),
             ("📜 Queue", "q:1"), ("🕘 History", "h:1")]]

# --------------------------------------------------------------------------
# secret hygiene
# --------------------------------------------------------------------------
# The token lives in the URL path of every Telegram call. requests puts the FULL
# URL into its exception messages, so a single logged exception leaks the token
# into the journal forever. Every log line about Telegram goes through _safe().
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
# what the station's own _clean_note strips before anything airs. Used only to warn a requester that the line
# they were shown is not quite the line that goes out (review finding).
# nr_at (2026-09-09): "@" left this list -- a handle typed here now survives and becomes a real mention.
_NOTE_TIDIED = re.compile(r"https?://|\w\.\w|\d{7,}")
_QS_SECRET = re.compile(r"([?&](?:nt|note|who)=)[^&\s]*")


# nr_at (2026-09-09, SF): "If a song is requested via telegram, the @ should be on telegram only, not on
# the other channels -- and visa versa." A handle is not the same person on two networks, and Telegram
# turns "@name" into a link to whoever owns that name HERE. So an @ survives only on the channel it was
# typed on. The station stamps every request with note_via; anything that did not come in through this
# bot loses its @ before it is shown. No stamp (an older station, or the web) counts as not ours.
# The X and Nostr announcers strip every @ on their own account (nr-announcer), so they need nothing.
_AT = re.compile(r"[@\uff20]+")


def _mine(t) -> bool:
    """True only when the station says this line was typed here, in Telegram."""
    return str((t or {}).get("note_via") or "").strip().lower() == "tg"


def _no_at(s: str) -> str:
    """Take the @ out. The words stay; only the tag goes."""
    return _AT.sub("", s)


def _nm(s) -> str:
    """A track name on its way into a Telegram message. Telegram linkifies "@name" in ANY text, so a
    track TITLED "@someone" tags a stranger no matter which door the request came in by -- the note rule
    cannot cover that, because a title is not a note. An @ in a track name is nearly always incidental
    and never a deliberate mention, so it always goes. Escaping happens here too, so a name can never
    reach a parse_mode=HTML message unescaped."""
    return html.escape(_no_at(str(s or "?")))


def _nw(t) -> str:
    """note (80) + optional name/handle (24) as one line: 'note -- who'. Empty when no note.
    nr_at: the @ stays only on a line that was typed here."""
    note = str((t or {}).get("note") or "")[:NOTE_MAX_FULL]   # nr_full: the station capped it already; do not re-cut at 80
    who = str((t or {}).get("who") or "")[:24]
    if not _mine(t):
        note, who = _no_at(note), _no_at(who)
    return (note + (" \u2014 " + who if who else "")) if note else ""


def _safe(text: object) -> str:
    """Strip anything that looks like a bot token out of a message."""
    s = str(text)
    s = _TOKEN_RE.sub("bot<REDACTED>", s)
    if TOKEN and TOKEN in s:
        s = s.replace(TOKEN, "<REDACTED>")
    return s


def _refuse_token_in_argv() -> None:
    """A token on the command line is world-readable via /proc/<pid>/cmdline.

    This box has a history of exactly that (an Icecast source password and an
    Owncast stream key both sat in argv). Make the mistake impossible instead of
    documenting it."""
    try:
        with open("/proc/self/cmdline", "rb") as fh:
            argv = fh.read().decode("utf-8", "replace")
    except OSError:
        argv = " ".join(sys.argv)
    if TOKEN and TOKEN in argv:
        sys.exit("REFUSING TO START: the bot token is in my own command line, "
                 "where any local user can read it. Put it in "
                 "/etc/nr-jukebox-bot.env (mode 0600) instead.")


# --------------------------------------------------------------------------
# Telegram transport
# --------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._s = requests.Session()

    def call(self, method: str, http_timeout: int = 20, **params):
        """One Bot API call. Returns the 'result' payload, or None on failure.

        NOTE the parameter name. Telegram's getUpdates takes its OWN `timeout`
        (how long to hold the long poll open), and that must travel in **params
        to Telegram. If this argument were called `timeout` it would swallow it
        and the poll would return instantly -- a tight loop straight into
        Telegram's rate limiter.

        Never raises upward and never lets the URL reach a log line."""
        try:
            r = self._s.post(self._base + method, data=params,
                             timeout=http_timeout)
            body = r.json()
        except Exception as e:
            LOG.warning("telegram %s failed: %s", method, _safe(e))
            return None
        if not body.get("ok"):
            # description is Telegram's own text and carries no token
            LOG.warning("telegram %s rejected: %s", method,
                        _safe(body.get("description", "")))
            return None
        return body.get("result")

    def send(self, chat_id, text, buttons=None, reply_to=None, silent=False):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": "true"}
        if silent:
            p["disable_notification"] = "true"
        if buttons is not None:
            p["reply_markup"] = _keyboard(buttons)
        if reply_to:
            p["reply_to_message_id"] = reply_to
        return self.call("sendMessage", **p)

    def edit(self, chat_id, message_id, text, buttons=None):
        with _lock:
            is_photo = (chat_id, message_id) in _photo_msgs
        if is_photo:
            # nr_qr (2026-09-08): an invoice is a photo now; its words live in the caption
            p = {"chat_id": chat_id, "message_id": message_id, "caption": text, "parse_mode": "HTML"}
            if buttons is not None:
                p["reply_markup"] = _keyboard(buttons)
            return self.call("editMessageCaption", **p)
        p = {"chat_id": chat_id, "message_id": message_id, "text": text,
             "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if buttons is not None:
            p["reply_markup"] = _keyboard(buttons)
        return self.call("editMessageText", **p)

    def send_photo(self, chat_id, png, caption, buttons=None, reply_to=None):
        """nr_qr (2026-09-08): one picture with a caption (the invoice QR). Returns the message, or None.
        Remembers the message id, so a later edit() of it goes to the caption."""
        p = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        if buttons is not None:
            p["reply_markup"] = _keyboard(buttons)
        if reply_to:
            p["reply_to_message_id"] = reply_to
        try:
            r = self._s.post(self._base + "sendPhoto", data=p,
                             files={"photo": ("invoice.png", png, "image/png")}, timeout=30)
            body = r.json()
        except requests.ConnectionError as e:
            LOG.warning("telegram sendPhoto could not connect: %s", _safe(e))
            return False                     # nothing left this box: the caller may send the plain form
        except Exception as e:
            LOG.warning("telegram sendPhoto failed: %s", _safe(e))
            return None                      # transport: the photo MAY have landed; never send a second invoice
        if not body.get("ok"):
            LOG.warning("telegram sendPhoto rejected: %s", _safe(body.get("description", "")))
            return False                     # Telegram said no: nothing landed; the caller may retry plainer
        res = body.get("result") or {}
        with _lock:
            _photo_msgs[(chat_id, res.get("message_id"))] = time.time()
            if len(_photo_msgs) > 500:
                for k in sorted(_photo_msgs, key=_photo_msgs.get)[:len(_photo_msgs) - 500]:
                    _photo_msgs.pop(k, None)
        return res

    def answer(self, callback_id, text=""):
        return self.call("answerCallbackQuery", callback_query_id=callback_id,
                         text=text[:200])

    def pin(self, chat_id, message_id, notify=False):
        # silent by default: a pinned now-playing card must not ping the group
        # every time it moves.
        return self.call("pinChatMessage", chat_id=chat_id, message_id=message_id,
                         disable_notification=("false" if notify else "true"))

    def unpin(self, chat_id, message_id):
        return self.call("unpinChatMessage", chat_id=chat_id,
                         message_id=message_id)

    def delete(self, chat_id, message_id):
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def get_chat(self, chat_id):
        return self.call("getChat", chat_id=chat_id)

    def chat_member(self, chat_id, user_id):
        # bounded: this runs on the update loop (admin check), which must not hang
        return self.call("getChatMember", http_timeout=10,
                         chat_id=chat_id, user_id=user_id)

    def me(self):
        return self.call("getMe")


# nr_qr8 (2026-09-09): the invoice PICTURE for the Telegram jukebox bot -- a WIDE canvas (2:1, 720x360). Left: a white
# square with the QR (LIGHTNING:LNBC... upper-case, error L) at a whole number of pixels per module, black on white so
# every wallet reads it. Right: the site's colours -- the dark "ether" teal with a slightly skewed gradient, the amount in
# bitcoin orange (Lato Black, all caps, tracked like the site's labels) top-aligned with the QR's first module, and three
# JUSTIFIED lines in the site's light "foam"; the station's wordmark (/opt/nr-jukebox-bot/nr-logo.png, the site's logo.svg
# rasterized) in the band between them. No footer (SF 2026-09-09). Black-and-white first: the split, the size of the
# number and the block of text carry the meaning; the colours are the bonus.
# Needs python3-segno (the QR) and python3-pil + the Lato fonts (present on the ship; DejaVu is the fallback). Without PIL the
# bare QR is returned; without segno nothing is returned and the caller sends the old text message.
def _invoice_png(bolt11, sats):
    try:
        import io
        import segno
    except Exception as e:
        LOG.warning("qr: segno missing: %s", _safe(e))
        return None
    try:
        qr = segno.make(("lightning:" + bolt11).upper(), error="l")
    except Exception as e:
        LOG.warning("qr: %s", _safe(e))
        return None
    def png_of(scale, border):
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=scale, border=border)
        return buf.getvalue()
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        try:
            return png_of(4, 4)                          # no PIL: the bare QR still works (measured: scale 4 scans)
        except Exception as e:
            LOG.warning("qr: %s", _safe(e))
            return None
    try:
        W, H = 720, 360
        ETHER, LINE, SIGNAL, FOAM = (10, 26, 31), (32, 66, 76), (247, 147, 26), (230, 239, 240)   # the site's palette (LINE a shade up)
        # the dark side: a linear gradient, skewed about 12 degrees, ether -> line (PIL only, no numpy)
        g = Image.linear_gradient("L").rotate(-78, resample=Image.BILINEAR).crop((40, 40, 216, 216)).resize((W, H))
        img = Image.composite(Image.new("RGB", (W, H), LINE), Image.new("RGB", (W, H), ETHER), g)
        # the light side: a white square, the QR centred at whole pixels per module (v9 -> 6 px, 342 px wide)
        img.paste((255, 255, 255), (0, 0, H, H))
        mods = qr.symbol_size(scale=1, border=2)[0]
        scale = max(2, (H - 12) // mods)
        code = Image.open(io.BytesIO(png_of(scale, 2))).convert("RGB")
        ox, oy = (H - code.width) // 2, (H - code.height) // 2
        img.paste(code, (ox, oy))
        top = oy + 2 * scale                             # the first black module's top edge
        d = ImageDraw.Draw(img)
        def font(size, weight="Regular"):
            for p in ("/usr/share/fonts/truetype/lato/Lato-%s.ttf" % weight,
                      "C:/Noderunners Radio Project/plan/bot/fonts/Lato-%s.ttf" % weight,
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if weight != "Regular" else ""),
                      "C:/Windows/Fonts/%s" % ("arialbd.ttf" if weight != "Regular" else "arial.ttf")):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
            return ImageFont.load_default()
        x0, x1 = H + 24, W - 24                          # the text column (312 px)
        # the amount: all caps, tracked, top-aligned with the QR's first module, sized to fit the column
        label = "%s SATS" % sats
        track = 4
        size = 88
        while size > 40:
            big = font(size, "Black")
            if sum(d.textlength(ch, font=big) for ch in label) + track * (len(label) - 1) <= x1 - x0:
                break
            size -= 2
        x = x0
        for ch in label:
            d.text((x, top), ch, fill=SIGNAL, font=big, anchor="lt")
            x += d.textlength(ch, font=big) + track
        head_bottom = top + big.getbbox(label, anchor="lt")[3]
        # three justified lines, the block bottom-aligned with the QR's last module
        body = font(31)
        words = "Scan with your wallet, or unfold the invoice below and tap to copy.".split()
        lines, cur = [], []
        for w in words:
            if cur and d.textlength(" ".join(cur + [w]), font=body) > x1 - x0:
                lines.append(cur)
                cur = []
            cur.append(w)
        lines.append(cur)
        lh = 46
        bottom = oy + code.height - 2 * scale            # the last black module's bottom edge
        y = bottom - 4 - lh * (len(lines) - 1)           # baseline of the first line
        for i, ln in enumerate(lines):
            widths = [d.textlength(w, font=body) for w in ln]
            last = i == len(lines) - 1
            if len(ln) == 1 or (last and sum(widths) < 0.6 * (x1 - x0)):
                d.text((x0, y), " ".join(ln), fill=FOAM, font=body, anchor="ls")
            else:
                gap = (x1 - x0 - sum(widths)) / (len(ln) - 1)
                xx = x0
                for w, wl in zip(ln, widths):
                    d.text((xx, y), w, fill=FOAM, font=body, anchor="ls")
                    xx += wl + gap
            y += lh
        # the station's wordmark (nr_qr8, SF: "room for the logo between 21 sats and the text"), centred in the band;
        # a missing or broken file costs nothing but the logo
        try:
            text_top = (bottom - 4 - lh * (len(lines) - 1)) + body.getbbox("Sl", anchor="ls")[1]   # first baseline + (negative) ascent
            for lp in ("/opt/nr-jukebox-bot/nr-logo.png", "C:/Noderunners Radio Project/plan/bot/nr-logo.png"):
                try:
                    logo = Image.open(lp).convert("RGBA")
                except Exception:
                    continue
                lw = x1 - x0
                logo = logo.resize((lw, max(1, round(logo.height * lw / logo.width))), Image.LANCZOS)
                band_top, band_bottom = head_bottom + 8, text_top - 8
                ly = band_top + (band_bottom - band_top - logo.height) // 2
                if logo.height <= band_bottom - band_top:
                    img.paste(logo, (x0, ly), logo)
                break
        except Exception as e:
            LOG.warning("qr: logo skipped: %s", _safe(e))
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        LOG.warning("qr: wide picture failed, sending the bare QR: %s", _safe(e))
        try:
            return png_of(4, 4)
        except Exception:
            return None


def _scrub(e) -> str:
    """requests puts the WHOLE url in its exception text. The status poll carries the invoice's note token --
    a write capability for the life of that invoice -- and the requester's own words. Neither belongs in a log
    line, so both are cut out here (review finding, 2026-09-09)."""
    return _QS_SECRET.sub(r"\1<hidden>", _safe(e))


def _keyboard(rows):
    """rows = [[(label, callback_data), ...], ...] -> JSON for reply_markup."""
    import json as _json
    return _json.dumps({"inline_keyboard": [
        [{"text": lbl, "callback_data": cb} if not cb.startswith("url:")
         else {"text": lbl, "url": cb[4:]} for lbl, cb in row]
        for row in rows]})


# --------------------------------------------------------------------------
# station API
# --------------------------------------------------------------------------
class Station:
    def __init__(self, base: str):
        self._base = base
        self._s = requests.Session()

    def get(self, path, timeout=15, **params):
        try:
            r = self._s.get(self._base + path, params=params, timeout=timeout)
            return r.status_code, (r.json() if r.content else {})
        except Exception as e:
            LOG.warning("station GET %s failed: %s", path, _scrub(e))
            return 0, {}

    def post(self, path, data, timeout=25):
        try:
            r = self._s.post(self._base + path, data=data, timeout=timeout)
            return r.status_code, (r.json() if r.content else {})
        except Exception as e:
            LOG.warning("station POST %s failed: %s", path, _scrub(e))
            return 0, {}


# --------------------------------------------------------------------------
# the only state this process keeps -- and what it costs to lose it
# --------------------------------------------------------------------------
# Losing ANY of this on restart costs nobody a single sat. The thing that must
# survive a restart -- the payment_hash -- is not kept here at all: it rides in
# the Telegram message's own callback_data, so Telegram is the datastore and the
# [I have paid] button still works after a redeploy. That is the design.
_lock = threading.Lock()
_rows: dict[str, tuple] = {}        # token -> (artist, title, expires_at, src, guid, owner_uid, note,
                                    #           album, duration_s, sats, n, sid, query)
                                    # "n" is the number the person SEES; "sid" groups one search, so any
                                    # page can be redrawn from here without asking the station again.
                                    # APPEND ONLY. _gc reads v[2] and the tap handler reads row[3..8]
                                    # positionally behind len() guards, so a new field goes on the END.
_last_search: dict[int, float] = {} # telegram user id -> last search time
_more: dict[str, tuple] = {}        # nr_deep: sid -> (query, next_offset, more, expires_at). The
                                    # CURSOR for one search: how many rows the station has already
                                    # handed over, and whether it said there was anything behind
                                    # them. Losing it on a restart costs a re-typed search and not
                                    # one sat: the rows already fetched keep working, they simply
                                    # stop growing.
_open_invoice: dict[int, tuple] = {}# user id -> (payment_hash, chat, msg, exp). THE HOLD, and only
                                    # that: "zap the one you have before asking for another". The
                                    # Back button releases it. It is NOT the watch list -- it was,
                                    # and that is exactly what broke (see _live).
_live: dict[str, tuple] = {}        # nr_live: payment_hash -> (uid, chat, msg, exp). Every invoice
                                    # still worth polling, whether or not its owner still holds the
                                    # hold. The watcher gates on THIS. Setting a card aside must
                                    # never stop the bot noticing the money: the station queues a
                                    # late payment regardless, so a bot that stopped watching left
                                    # a paid listener staring at a QR code that never went away.
_relayed: set = set()                # payment hashes already alarmed to the admin chat (this process)
_notes_relayed: set = set()          # admin note ids already relayed (this process)
_watch: list = []                   # heap of (due_at, hash, chat, msg, started)
_inflight: set = set()              # hashes with a status call outstanding
_inv_note: dict[str, dict] = {}     # payment_hash -> {"nt": token, "note": text, "sent": bool, "on": bool}
                                    # nr_note: the message a requester wants shown with their song. Written in
                                    # the SAME locked block as _open_invoice, so the sweeper cannot race it
                                    # (the lesson from the Nostr bot's "gone" bug, 2026-09-08).
_note_late: dict[tuple, float] = {} # (chat, invoice msg) -> when it closed. A line typed a moment too late is
                                    # then answered honestly instead of being run as a library search.
_inv_src: dict[str, str] = {}       # payment_hash -> "library"/"wavlake", for an
                                    # accurate V4V echo (never guessed from text)
_photo_msgs: dict[tuple, float] = {}  # (chat_id, message_id) of invoice PHOTOS -> sent_at (nr_qr: edits go to the caption)
_expire: dict[tuple, float] = {}    # (chat_id, message_id) -> delete_at. The bot's
                                    # own GROUP chatter, tidied after it goes stale.
                                    # Never holds the card, a receipt, or a DM.


def _cell(s, w: int) -> str:
    """One fixed-width column. Cut with an ellipsis rather than wrapped, so the columns stay lined up."""
    s = " ".join(str(s or "").split())
    return (s[:w - 1] + "…") if len(s) > w else s.ljust(w)


def _mmss(d) -> str:
    try:
        d = int(d or 0)
    except (TypeError, ValueError):
        d = 0
    return ("%d:%02d" % divmod(d, 60)) if d > 0 else "-"


def _page_view(sid: str, page: int):
    """Draw one page of a search from _rows alone -- no second API call, and no second dictionary to
    leak: the rows ARE the memory, already swept by _gc and already capped by MAX_ROWS_TOTAL.

    THE RULE: a row keeps the number it was given when the search ran. NEVER renumber per page. If the
    number someone reads stopped matching the button they press, that is the one way paging could ever
    charge for the wrong song. Returns (text, grid, total); text is None when the search is gone."""
    with _lock:
        mine = [(tok, r) for tok, r in _rows.items() if len(r) > 12 and r[11] == sid]
    if not mine:
        return None, None, 0
    mine.sort(key=lambda kr: kr[1][10])
    total = len(mine)
    pages = (total + MAX_ROWS - 1) // MAX_ROWS
    page = max(0, min(page, pages - 1))
    chunk = mine[page * MAX_ROWS:(page + 1) * MAX_ROWS]
    if not chunk:
        return None, None, 0
    query, uid = chunk[0][1][12], chunk[0][1][5]
    # nr_page: decided over the WHOLE result set, never per page, or the layout jumps while paging.
    # Searching a band is the common case, and dropping the column buys 16 characters on a phone.
    # nr_fit (2026-09-10): a Telegram code block WRAPS, it does not scroll. The first cut measured 63
    # columns and every row arrived broken in two with LEN on a line of its own. 40 is the width that
    # survives a phone held upright, which is where the group actually reads this.
    # Case-INSENSITIVE, or "DIRE STRAITS" and "Dire Straits" count as two artists and the column stays
    # for nothing. The name shown is whichever spelling the catalogue uses most.
    from collections import Counter as _Counter
    _names = _Counter((r[0] or "").strip() for _t, r in mine if (r[0] or "").strip())
    one_artist = len({n.lower() for n in _names}) == 1 and bool(_names)
    artist_name = _names.most_common(1)[0][0] if _names else ""
    # nr_space (2026-09-10, SF: "might still be some spacing improvement for the rows?"): a space
    # AFTER every separator, so no text ever touches a bar. It costs three columns, and 40 is still
    # the width that survives a phone held upright, so the ALBUM column pays for it -- the title is
    # the thing being read and it keeps almost all of its room. The number column widens to three
    # because deep paging reaches row 482, and "482" does not fit in two.
    # nr_narrow (2026-09-10, Justin Flation in the group, with a screenshot: "Not good on
    # Smartphone"): both layouts were too wide and BOTH broke in two on a phone -- 43 columns with
    # the artist column, 40 without. Measured from his screenshot, that phone holds about 37
    # characters; 34 is the target because cutting it to the last character his particular phone
    # happens to fit is how this went wrong the first time. A smaller screen, a larger accessibility
    # font or another Telegram build each take that margin away, and nobody reports it twice.
    #
    # LEN IS DROPPED WHEN THE ARTIST COLUMN IS THERE. With several artists on the page the length is
    # the least useful thing on it -- you tell those rows apart by who made them. On a ONE-ARTIST
    # search it stays, because that is exactly where it earns its place: nineteen pressings of "Walk
    # Of Life" differ by album and by length and by nothing else. That case pays in width instead.
    #   one artist : 3 + 2 + 13 + 2 + 7 + 2 + 5 = 34
    #   mixed      : 3 + 2 +  9 + 2 + 9 + 2 + 7 = 34
    # nr_flow (2026-09-10, Justin Flation: "It is still worse than before. Is it possible to set this
    # field as a dynamic large one?"): the answer to his question is NO, and that is the point. A
    # Telegram code block has no width control, no dynamic sizing and no marquee -- its font size is
    # the READER'S setting. So the number of characters that fit is a property of HIS phone, and we
    # cannot know it. I guessed it twice: 43 wrapped, 34 wrapped (his screenshot breaks near 28). A
    # third guess would be the same mistake with a smaller number.
    #
    # So the TABLE GOES. It only ever existed to line columns up, that only works at a known width,
    # and there is no known width. Flowing rich text has no alignment to lose: Telegram wraps it at
    # any width and any font size, and it grows with the reader's own settings -- the "dynamic" he
    # is actually asking for.
    #
    # SF's original complaint is still answered, and better: the TITLE is bold and first, the artist
    # and album sit underneath in italics, and nothing is cut to seven characters.
    def _line(num, title, album, length, artist=""):
        # every field is capped: the catalogue holds 683,000 files from many sources and one corrupt
        # tag with a 500-character title would otherwise eat the 4096-character message on its own.
        t = html.escape(" ".join((title or "?").split())[:70])
        top = "<b>%s</b> · <b>%s</b>" % (num, t)
        if length and length != "-":
            top += " · %s" % length
        under = []
        if not one_artist and (artist or "").strip():
            under.append(html.escape(" ".join(artist.split())[:44]))
        if (album or "").strip():
            under.append(html.escape(" ".join(album.split())[:52]))
        return top + ("\n<i>%s</i>" % " · ".join(under) if under else "")
    body = [_line(r[10], r[1], r[7], _mmss(r[8]), r[0]) for _tok, r in chunk]
    # nr_sampleprice (2026-09-24, SF): "1: 21⚡", not "1 · 21⚡"
    btns = [("%d: %s⚡%s" % (r[10], r[9], " V4V" if r[3] == "wavlake" else ""), tok)
            for tok, r in chunk]
    grid = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    # nr_deep: Next also appears on the LAST remembered page, when the cursor says the station has
    # more behind it. Tapping it fetches the next batch -- see _top_up. The cursor is read under the
    # same lock as the rows, so the button can never offer a page the cursor has already ruled out.
    with _lock:
        cur = _more.get(sid)
    can_fetch = bool(cur) and bool(cur[2]) and total < MAX_ROWS_DEEP
    nav = []
    if page > 0:
        nav.append(("◂ Previous", "m:%s:%d" % (sid, page - 1)))
    if page + 1 < pages or can_fetch:
        nav.append(("Next ▸", "m:%s:%d" % (sid, page + 1)))
    if nav:
        grid.append(nav)
    grid.append([("✖ Cancel", "x:%s" % uid)])
    # the FIRST line keeps its exact shape: /gift reads the query back out of it.
    # cap what is ECHOED. cmd_play already caps a typed query at MAX_QUERY_CHARS, but this text is also
    # rebuilt from a stored row on every nav tap, and html.escape can turn one apostrophe into six --
    # so an uncapped echo is the one part of this message whose length is not already bounded.
    txt = "Found these for <b>%s</b> — tap a number to request it:\n" % html.escape(query[:MAX_QUERY_CHARS])
    if one_artist:
        txt += "\n<b>%s</b>" % html.escape(artist_name[:60])
    # nr_flow: each field was escaped as it was capped, above -- so this joins already-safe pieces and
    # the bold and italic tags survive. Six rows of two lines with every field bounded comes to well
    # under 1,200 characters, so Telegram's 4096 stays far away.
    txt += "\n" + "\n".join(body)
    # nr_deep: the "+" is an honest "at least this many" -- the station said there is more behind
    # what we hold, and Next will go and get it.
    if pages > 1 or can_fetch:
        txt += "\nShowing %d–%d of %d%s." % (chunk[0][1][10], chunk[-1][1][10], total,
                                             "+" if can_fetch else "")
    return txt, grid, total


def _gc(now: float) -> None:
    """Drop anything past its usefulness. Cheap, runs on the watcher tick."""
    with _lock:
        for k, v in [(k, v) for k, v in _rows.items() if v[2] < now]:
            _rows.pop(k, None)
        # hard ceiling by COUNT, not just by time: evict lowest-expiry first so a
        # burst of searches can never grow _rows without bound.
        if len(_rows) > MAX_ROWS_TOTAL:
            for k, _v in sorted(_rows.items(),
                                key=lambda kv: kv[1][2])[:len(_rows) - MAX_ROWS_TOTAL]:
                _rows.pop(k, None)
        for uid, v in [(u, v) for u, v in _open_invoice.items() if v[3] < now]:
            _open_invoice.pop(uid, None)
        # nr_live: the watch list ages on the same clock. Nothing else drops it, so if this line
        # were missing a never-paid invoice would be polled until the process restarted.
        for ph, v in [(p, v) for p, v in _live.items() if v[3] < now]:
            _live.pop(ph, None)
        for uid, t in [(u, t) for u, t in _last_search.items()
                       if now - t > 3600]:
            _last_search.pop(uid, None)
        # nr_deep: a cursor never outlives its rows. Drop it on its own clock, and drop it early
        # once every row of its search is gone -- rows are swept above, so this reads the truth.
        _sids = {v[11] for v in _rows.values() if len(v) > 12}
        for s in [s for s, v in _more.items() if v[3] < now or s not in _sids]:
            _more.pop(s, None)
        # drop source records whose invoice is no longer being watched.
        # nr_live: this used to read _open_invoice, so a Back threw the listener's line away before
        # the payment landed and it never reached the song. It follows the WATCH now, not the hold.
        open_hashes = set(_live)
        for ph in [p for p in _inv_src if p not in open_hashes]:
            _inv_src.pop(ph, None)
        for ph in [p for p in _inv_note if p not in open_hashes]:
            _inv_note.pop(ph, None)
        for k in [k for k, t in _note_late.items() if now - t > NOTE_LATE_S]:
            _note_late.pop(k, None)


def _pack(payment_hash: str) -> str:
    """payment_hash -> callback_data.

    The raw hex hash is exactly 64 bytes and Telegram's callback_data limit is
    64 bytes, so hex plus ANY prefix overflows and the button silently dies.
    base64url of the raw bytes is 44 chars, leaving room for a prefix."""
    return "p:" + base64.urlsafe_b64encode(
        bytes.fromhex(payment_hash)).decode().rstrip("=")


def _unpack(data: str) -> str | None:
    try:
        b = data[2:]
        b += "=" * (-len(b) % 4)
        return base64.urlsafe_b64decode(b).hex()
    except (ValueError, binascii.Error):
        return None


# --------------------------------------------------------------------------
# the bot
# --------------------------------------------------------------------------
class Bot:
    def __init__(self, tg: Telegram, st: Station):
        self.tg, self.st = tg, st
        self.stop = threading.Event()
        self.bot_id = None          # our own Telegram id, from getMe (card adoption)
        self.bot_username = None     # our own @name, to ignore other bots' commands
        self._card_mid = None       # message_id of the pinned now-playing card
        self._card_text = None      # last text we rendered, to skip no-op edits
        self._ann_seen = {}         # v15: req_id -> {"ts","mid","note","tries"}
        self._ann_seed_until = None # v15: seeding window after start
        self._ann_ok = False        # v15: target verified as a channel we can post to
        # runtime notice override. None = fall back to the CARD_NOTICE env default;
        # "" = the owner said /notice off, show no line; str = show that line.
        self._notice = None

    def _notice_line(self):
        return self._notice if self._notice is not None else CARD_NOTICE

    # ---------------- helpers ----------------
    def _live_session(self):
        """Returns the DJ names if a live sesh is on, else None.

        While a sesh is open the jukebox is shut -- a DJ has the deck. Checking
        first is cheaper and kinder than minting an invoice the server would
        refuse with 409."""
        code, body = self.st.get("/api/livesplit", timeout=8)
        if code == 200 and body.get("live"):
            return body.get("dj") or "a guest DJ"
        return None

    def _queue_state(self):
        """(now_playing_key, {keys in queue}, queue_length) -- lowercased."""
        code, body = self.st.get("/api/nowplaying", timeout=8)
        if code != 200:
            return None, set(), 0
        def key(t):
            if not t:
                return None
            return f"{(t.get('artist') or '').lower()}|{(t.get('title') or '').lower()}"
        q = body.get("queue") or []
        return key(body.get("now_playing")), {key(t) for t in q if t}, len(q)

    def _admin_echo(self, text):
        """-> the Telegram result, or None when it did not go out (the caller may care)."""
        if ADMIN_CHAT:
            return self.tg.send(ADMIN_CHAT, text)
        return None

    # ---------------- chat hygiene ----------------
    def _say(self, chat, text, buttons=None, reply_to=None, ttl=None):
        """Send, and remember to tidy it away later. Same signature as tg.send,
        so command code reads unchanged. Only GROUP chatter is ever tidied; a
        private chat and the pinned card are left alone (see _track)."""
        r = self.tg.send(chat, text, buttons=buttons, reply_to=reply_to)
        if r:
            self._track(chat, r.get("message_id"),
                        MSG_TTL_S if ttl is None else ttl)
        return r

    def _finish(self, chat, mid, text, buttons, ttl):
        """nr_qr2 (2026-09-08): the last word on an invoice. A text message is edited in place; a PICTURE (the QR) is
        deleted and a plain text takes its place, because a photo message cannot become a text one. Returns the
        message id that carries the receipt. SF: 'the message with the QR should ditch the QR after payment'."""
        with _lock:
            is_photo = (chat, mid) in _photo_msgs
        if is_photo:
            r = self.tg.send(chat, text, buttons=buttons)
            if r and r.get("message_id"):
                self.tg.delete(chat, mid)
                with _lock:
                    _photo_msgs.pop((chat, mid), None)
                    _expire.pop((chat, mid), None)
                mid = r["message_id"]
            else:
                self.tg.edit(chat, mid, text, buttons=buttons)   # the send failed: keep the picture, fix its caption
        else:
            self.tg.edit(chat, mid, text, buttons=buttons)
        self._track(chat, mid, ttl)
        return mid

    def _track(self, chat, mid, ttl):
        """Mark one of our own messages for later self-deletion -- but only in a
        group (chat id < 0), never in a DM, and never when tidying is switched
        off. A ttl<=0 also means keep."""
        if mid is None or ttl <= 0 or MSG_TTL_S <= 0:
            return
        try:
            if int(chat) >= 0:      # DMs are positive ids: nothing to tidy there
                return
        except (TypeError, ValueError):
            return
        with _lock:
            _expire[(chat, mid)] = time.time() + ttl
            if len(_expire) > MAX_EXPIRE:
                stale = sorted(_expire.items(), key=lambda kv: kv[1])
                for k, _d in stale[:len(_expire) - MAX_EXPIRE]:
                    _expire.pop(k, None)

    def _sweep(self, now):
        """Delete our own group messages whose time is up. Runs on the watcher
        tick. Never touches a live invoice or the pinned card.

        The check-and-claim happens UNDER the lock, per message, right before the
        network delete -- not from a stale snapshot. That closes the race where a
        row list hits its TTL at the very moment it is tapped: _mint would add the
        invoice to _open_invoice and pop it from _expire in between, and a
        snapshot taken earlier would delete the live invoice anyway."""
        with _lock:
            due = list(_expire.keys())
        for cm in due:
            with _lock:
                d = _expire.get(cm)
                if d is None or d > now:
                    continue                         # gone or renewed under us
                open_ids = {(v[1], v[2]) for v in _open_invoice.values()}
                is_card = (self._card_mid is not None
                           and str(cm[0]) == str(HOME_CHAT)
                           and cm[1] == self._card_mid)
                # claim it (stop tracking) whether or not we delete: a live
                # invoice or the card must simply drop out of the janitor.
                _expire.pop(cm, None)
                skip = (cm in open_ids) or is_card
            if not skip:
                self.tg.delete(cm[0], cm[1])

    def _is_admin(self, chat, uid):
        """True if this user runs the group. The bot only pins/notices on the say
        of a group admin -- never a random member."""
        info = self.tg.chat_member(chat, uid)
        if not info:
            return False
        return info.get("status") in ("administrator", "creator")

    # ---------------- pinned now-playing card ----------------
    def _render_card(self):
        code, body = self.st.get("/api/nowplaying", timeout=8)
        body = body or {}
        np = body.get("now_playing") if code == 200 else None
        if code == 200:
            try:
                self._announce_requests(body)          # v15: channel post for new paid tracks
            except Exception as e:
                LOG.warning("announce error: %s", _safe(e))
        # ORDER MATTERS: Telegram's collapsed pin bar previews the FIRST line,
        # so the track leads and the notice rides below (SF, 2026-08-13).
        parts = []
        if np:
            parts.append("🎶 <b>" + _nm(np.get("artist")) +
                         " — " + _nm(np.get("title")) + "</b>")
            if np.get("note"):
                parts.append("💬 <i>" + html.escape(_nw(np)) + "</i>")
        else:
            parts.append("🎶 <b>Noderunners Radio</b> — always on.")
        q = [t for t in (body.get("queue") or []) if t] if code == 200 else []
        if q:
            parts.append("▶ Next: " +
                         _nm(q[0].get("artist")) + " — " +
                         _nm(q[0].get("title")) +
                         (" 💬 <i>" + html.escape(_nw(q[0])) + "</i>" if q[0].get("note") else ""))
        live = self._live_session()
        if live:
            parts.append("🎛 Live sesh — " + html.escape(live) + " has the deck.")
        notice = self._notice_line()
        if notice:
            parts.append("————————")
            parts.append(html.escape(notice))   # SF decorates his own notice
        return "\n".join(parts)

    def _announce_probe(self):
        """v15/v18: once at start. The target must be a group, supergroup or channel the bot can see
        (a user account would fail on every post)."""
        if not ANNOUNCE_CHAT:
            return
        info = self.tg.get_chat(ANNOUNCE_CHAT) or {}
        ctype = info.get("type")
        if ctype not in ("channel", "supergroup", "group"):
            LOG.error("announce OFF: %s is type=%r, not a group or channel", ANNOUNCE_CHAT, ctype)
            if HOME_CHAT:
                self._say(HOME_CHAT, "⚠️ Announcements are OFF: <code>%s</code> is not a group or channel "
                          "I can post to. Fix NR_ANNOUNCE_CHAT and restart me." % html.escape(ANNOUNCE_CHAT), ttl=0)
            return
        if HOME_CHAT and str(info.get("linked_chat_id") or "") == str(HOME_CHAT):
            LOG.warning("announce: the channel is LINKED to the home group -- Telegram will mirror every post there")
        self._ann_ok = True
        LOG.info("announce ON -> %s (%s)", ANNOUNCE_CHAT, info.get("title") or "")

    def _announce_text(self, t):
        st = t.get("set") if isinstance(t.get("set"), dict) else None
        if st and st.get("n"):
            # nr_sets3 (2026-10-31): a listener set is ONE post that says what it is, with the set's own shoutout.
            # Every song of a set shares one req_id, so this runs once; whichever song the bot sees first, the words
            # are the same (the first song is already loaded into the player before it shows in the queue).
            note = _nw(dict(t, note=st.get("note") or ""))
            who = str(t.get("who") or "")[:24]
            if who and not _mine(t):
                who = _no_at(who)
            lead = ("🎶 " + html.escape(who) + " zapped a listener set into the broadcast:" if who
                    else "🎶 Someone zapped a listener set into the broadcast:")
            txt = "%s\n<b>%d songs, back to back</b>" % (lead, int(st.get("n") or 0))
            if note:
                txt += "\n💬 <i>%s</i>" % html.escape(note)
            return txt + "\n▶ noderunnersradio.com", note
        note = _nw(t)
        who = str(t.get("who") or "")[:24] if t.get("note") else ""
        if who and not _mine(t):
            who = _no_at(who)    # nr_at: a name from another network never tags anyone here
        # nr_freeplay2 (SF 2026-10-19): a free play from the boombox QR says who really gave what
        lead = (("🎁 The captain gifted a jukebox play to a random anon:" if t.get("kind") == "free"
                 else "🎁 The captain put a track on the house:") if t.get("lane") == "gift"
                else ("🎶 " + html.escape(who) + " zapped a track into the broadcast:" if who
                      else "🎶 Someone zapped a track into the broadcast:"))
        txt = "%s\n<b>%s — %s</b>" % (lead, _nm(t.get("artist")), _nm(t.get("title")))
        if note:
            txt += "\n💬 <i>%s</i>" % html.escape(note)
        return txt + "\n▶ noderunnersradio.com", note

    def _announce_requests(self, body):
        """v15: ONE silent channel post per requested track (keyed on the API's req_id). After
        a start, the first ANNOUNCE_SEED_S only remember what is already there. A key is kept
        alive while the track is visible and forgotten ANNOUNCE_FORGET_S after it was last seen.
        If a note is hidden later (kill switch / note_hidden), the post is EDITED without it."""
        if not (ANNOUNCE_CHAT and self._ann_ok):
            return
        now = time.time()
        if self._ann_seed_until is None:
            self._ann_seed_until = now + ANNOUNCE_SEED_S
        seeding = now < self._ann_seed_until
        items = []
        np = body.get("now_playing")
        if np and np.get("requested") and np.get("req_id"):
            items.append(np)
        items += [t for t in (body.get("queue") or []) if t and t.get("requested") and t.get("req_id")]
        for t in items:
            k = str(t.get("req_id"))
            rec = self._ann_seen.get(k)
            txt, note = self._announce_text(t)
            if rec is None:
                rec = {"ts": now, "mid": None, "note": note, "tries": 0, "seeded": seeding}
                self._ann_seen[k] = rec
            rec["ts"] = now
            if rec.get("seeded"):
                continue                                   # was already there when we started
            if rec["mid"] is None and rec["tries"] < 3:
                rec["tries"] += 1
                r = self.tg.send(ANNOUNCE_CHAT, txt, silent=True)   # channel post: never tidied, never pinned
                if r:
                    rec["mid"] = r.get("message_id"); rec["note"] = note
                elif rec["tries"] >= 3:
                    LOG.warning("announce: giving up on %s after 3 tries", k)
            elif rec["mid"] is not None and note != rec.get("note") and not note:
                # the note was hidden after we posted: take it off the channel post too
                if self.tg.edit(ANNOUNCE_CHAT, rec["mid"], txt) is not None:
                    rec["note"] = note
        for k, rec in list(self._ann_seen.items()):
            if now - rec.get("ts", 0) > ANNOUNCE_FORGET_S:
                self._ann_seen.pop(k, None)

    def _adopt_card(self):
        """Decide what to do about a possibly-existing pinned card WITHOUT ever
        risking a duplicate. Returns one of:
          ("adopt", mid)   -- our card is already pinned; reuse it.
          ("empty", None)  -- confirmed nothing of ours is pinned; safe to create.
          ("unknown", None)-- a lookup failed; do NOT create, retry next tick.
        The 'unknown' case is the whole point: getChat or getMe failing must never
        make us pin a SECOND card over one that may already be there."""
        if not self.bot_id:
            me = self.tg.me()           # a restart may have missed the first getMe
            if me:
                self.bot_id = me.get("id")
                if not self.bot_username:
                    self.bot_username = (me.get("username") or "").lower()
        info = self.tg.get_chat(HOME_CHAT)
        if not info:
            return ("unknown", None)    # getChat failed -- cannot rule out a card
        pinned = info.get("pinned_message")
        if not pinned:
            return ("empty", None)      # confirmed: nothing pinned
        if not self.bot_id:
            return ("unknown", None)    # cannot confirm ownership -- do not risk it
        frm = pinned.get("from") or {}
        text = pinned.get("text") or ""
        if frm.get("id") == self.bot_id and (("🎶" in text) or ("📌" in text)):
            return ("adopt", pinned.get("message_id"))
        return ("empty", None)          # someone else's pin -- we still want ours

    def _ensure_card(self):
        text = self._render_card()
        # the card is the bot's permanent face: Queue/History live HERE so the
        # self-tidy can keep sweeping the chat without hiding the ship's state
        # (SF, 2026-08-14: "queue and history disappeared").
        btn = _btns()
        with _lock:
            mid = self._card_mid
        if mid is None:
            state, amid = self._adopt_card()
            if state == "unknown":
                return                          # transient failure: never double-pin
            if state == "adopt":
                mid = amid
                with _lock:
                    self._card_mid = amid
                    self._card_text = None      # force the refresh edit below
            else:
                # confirmed empty -> create and pin exactly one card
                r = self.tg.send(HOME_CHAT, text, buttons=btn)
                if not r:
                    return
                new_mid = r.get("message_id")
                pinned = self.tg.pin(HOME_CHAT, new_mid)
                # A silent pin failure hid for DAYS (SF, 2026-08-14: "the bot
                # never pinned a message") -- duplicate cards on every restart
                # and nobody the wiser. Say it out loud, once per run.
                if pinned is None and not getattr(self, "_pin_warned", False):
                    self._pin_warned = True
                    self._say(HOME_CHAT,
                        "⚠️ I could not PIN the card. Make me a group admin "
                        "with the 'Pin messages' right, then restart me.")
                with _lock:
                    self._card_mid = new_mid
                    self._card_text = text
                return
        with _lock:
            unchanged = (text == self._card_text)
        if unchanged:
            return
        r = self.tg.edit(HOME_CHAT, mid, text, buttons=btn)
        if r is None:
            # the card was deleted under us: forget it and rebuild next tick
            with _lock:
                self._card_mid = None
                self._card_text = None
        else:
            with _lock:
                self._card_text = text

    def card_loop(self):
        """One thread that keeps the pinned card in step with the deck -- and (v15) feeds the
        group announcer, also when there is no pinned card."""
        if not HOME_CHAT and not ANNOUNCE_CHAT:
            return
        self.stop.wait(3)           # let getMe / startup settle
        try:
            self._announce_probe()
        except Exception as e:
            LOG.warning("announce probe error: %s", _safe(e))
        while not self.stop.is_set():
            try:
                if HOME_CHAT:
                    self._ensure_card()
                else:
                    code, body = self.st.get("/api/nowplaying", timeout=8)
                    if code == 200:
                        self._announce_requests(body or {})
            except Exception as e:
                LOG.warning("card error: %s", _safe(e))
            self.stop.wait(CARD_POLL_S)

    # ---------------- commands ----------------
    def _price_words(self):
        """nr_pricemode (2026-09-15): the price words follow the station's pricing switch, asked at most every 5 min."""
        now = time.time()
        c = getattr(self, "_pw", None)
        if c and now - c[0] < 300:
            return c[1]
        words = "21 sats"
        try:
            code, d = self.st.get("/api/stats", timeout=8)
            if code == 200 and isinstance(d, dict):
                n = int(d.get("min_sats") or 21)
                words = ("%d sats (a bit more for a long song)" % n) if d.get("pricing") == "length" else ("%d sats" % n)
        except Exception:
            pass
        self._pw = (now, words)
        return words

    def cmd_start(self, chat, user):
        name = html.escape((user.get("first_name") or "there")[:32])
        self._say(chat,
            f"Hello {name}. This is the <b>Noderunners Radio</b> jukebox.\n\n"
            f"Send me an artist or a song and I will look for it. If it is "
            f"aboard, you <b>zap {self._price_words()}</b> ⚡ straight from your own wallet "
            f"and it goes in the queue.\n\n"
            f"<b>I never hold your sats.</b> There is no balance here, no "
            f"top-up and no account. Every zap goes from your wallet to the "
            f"station, once, for one song.\n\n"
            f"⚠️ <b>Used the old jukebox bot?</b> Withdraw your balance from it "
            f"now. It runs on a machine the station cannot reach and it is being "
            f"switched off — <b>1 November at the latest</b>. Nobody here can "
            f"recover what is left on it. /old for the details.\n\n"
            f"/play — ask for a song\n"
            f"/now — what is on air\n"
            f"/help or /faq — the longer version",
            buttons=_btns())

    def cmd_help(self, chat):
        self._say(chat,
            "<b>How this works</b>\n\n"
            "1. Send me a song — <code>/play daft punk one more time</code>, or "
            "just type it here.\n"
            "2. I show what is aboard. Tap the one you want.\n"
            f"3. You get a Lightning invoice for <b>{self._price_words()}</b>. Zap it from "
            "your own wallet.\n"
            "4. Scan the QR with your wallet, or unfold the invoice and copy it. I check on my own.\n"
            "5. Want a line shown with your song? Put it after a <code>|</code> "
            "(<code>/play bonobo kiara | hello from Tilburg</code>), or reply to the invoice before you pay. "
            "An <code>@name</code> in that line tags them here in the group, and only here — a handle is "
            "not the same person on another network.\n"
            "6. It joins the queue.\n\n"
            f"<b>Why zap {self._price_words()}?</b> It is not a price and it is not a "
            "purchase. It is a small toll that proves a person is asking, not "
            "a flood — anti-spam, nothing more. The music itself is covered by "
            "the licences the station already holds.\n\n"
            "<b>Why call it a zap?</b> ⚡ A bolt of Lightning: small, fast, "
            "from you to the ship. Zeus threw them too, and he never asked "
            "for change.\n\n"
            "<b>Where are my sats kept?</b> They are not. This bot has no "
            "wallet and no balance. If it vanished tonight you would lose "
            "nothing, because it never held anything of yours.\n\n"
            "<b>Nothing found?</b> The station plays its own library. Not "
            "everything is aboard yet.\n\n"
            "<b>A live sesh is on?</b> A DJ has the deck and the jukebox is "
            "shut until they finish. Come listen.\n\n"
            "<b>Commands</b>\n"
            "/play — ask for a song\n"
            "/play random — let the dice pick (Next ▸ deals more)\n"
            "/now — what is on air\n"
            "/queue — what is lined up\n"
            "/history — what just played\n"
            "/old — about the old bot and your balance in it\n\n"
            "Listen, watch live seshes + tip the crew: noderunnersradio.com\n"
            "DJ or host? Your door: noderunnersradio.com/dj/",
            buttons=_btns())

    def cmd_queue(self, chat):
        """What is lined up. Read-only, no money, safe to spam."""
        code, body = self.st.get("/api/nowplaying", timeout=8)
        if code != 200:
            return self._say(chat, "Cannot reach the deck right now.")
        q = [t for t in (body.get("queue") or []) if t]
        if not q:
            return self._say(chat,
                "Nothing lined up. Send me a song and it goes straight on.",
                buttons=_btns())
        txt = "<b>Lined up</b>\n" + "\n".join(
            "{}. {} — {}{}".format(i,
                _nm(t.get("artist")),
                _nm(t.get("title")),
                (" 💬 <i>" + html.escape(_nw(t)) + "</i>") if t.get("note") else "")
            for i, t in enumerate(q[:15], 1))
        if len(q) > 15:
            txt += "\n… and %d more" % (len(q) - 15)
        self._say(chat, _fit(txt), buttons=_btns())    # nr_full: fifteen long lines would be REFUSED

    def cmd_history(self, chat):
        """What just played. Shows the clock only -- the date is noise here."""
        code, body = self.st.get("/api/history", timeout=8, n=10)
        if code != 200:
            return self._say(chat, "Cannot reach the play log right now.")
        h = body.get("history") or []
        if not h:
            return self._say(chat, "Nothing in the log yet.")
        def line(t):
            # some log rows carry no timestamp; show the track without a gap.
            # The log speaks UTC; listeners live at Dutch local time -- showing
            # the raw clock made a track that JUST aired look two hours old
            # ("but it didn't play?", 2026-08-14). Convert for display only.
            clock = ""
            ts = t.get("ts") or ""
            if ts:
                try:
                    clock = (datetime.fromisoformat(ts)
                             .astimezone(ZoneInfo("Europe/Amsterdam"))
                             .strftime("%H:%M"))
                except (ValueError, KeyError):
                    clock = ts[11:16]      # unparseable: raw beats nothing
            head = clock + "  " if clock else ""
            return "{}{} — {}".format(head,
                _nm(t.get("artist")),
                _nm(t.get("title")))
        txt = "<b>Just played</b>\n" + "\n".join(line(t) for t in h)
        self._say(chat, txt, buttons=_btns())

    def cmd_old(self, chat):
        self._say(chat,
            "⚠️ <b>The old jukebox bot is being retired.</b>\n\n"
            "The old bot kept a balance for each user. It runs on "
            "infrastructure that belongs to its original developer, who is out "
            "for health reasons. <b>The station has no access to that machine "
            "and cannot move funds on it.</b> It will be switched off by "
            "<b>1 November 2026</b>, possibly sooner.\n\n"
            "<b>Open the old bot and withdraw your balance to your own "
            "Lightning wallet. Today.</b> Anything left when it goes dark is "
            "gone, and the station will not be able to help — there is no door "
            "for us to open. We are telling you early because we would rather "
            "you kept your money.\n\n"
            "This bot is built the other way round on purpose: <b>it never "
            "holds your sats.</b> Nothing to top up, nothing to migrate, "
            "nothing to lose.\n\n"
            "🛡 <b>Nobody will ever ask you for a wallet string, a link code, a "
            "seed or a password — not the station, not any bot, not in a DM.</b> "
            "Anyone who does is stealing from you. Withdraw using the old bot "
            "itself, never by sending a code to someone.\n\n"
            "Tap the button below — it opens the old bot directly. That "
            "chat is the ONLY safe place to withdraw.",
            buttons=[[("💰 Open the old bot and withdraw",
                       "url:https://t.me/Jukebox_Lightning_bot")]])

    def cmd_now(self, chat):
        code, body = self.st.get("/api/nowplaying", timeout=8)
        if code != 200:
            return self._say(chat, "Cannot reach the deck right now. "
                                   "Try again in a minute.")
        np = body.get("now_playing")
        if not np:
            txt = "Nothing on air right now."
        else:
            txt = (f"🔊 <b>{_nm(np.get('artist'))} — "
                   f"{_nm(np.get('title'))}</b>")
            if np.get("note"):
                txt += f"\n💬 <i>{html.escape(_nw(np))}</i>"
        q = [t for t in (body.get("queue") or []) if t]
        if q:
            txt += "\n\n<b>Next up</b>\n" + "\n".join(
                f"{i}. {_nm(t.get('artist'))} — "
                f"{_nm(t.get('title'))}"
                + (f" 💬 <i>{html.escape(_nw(t))}</i>" if t.get('note') else "")
                for i, t in enumerate(q[:5], 1))
        live = self._live_session()
        if live:
            txt += f"\n\n🎛 <b>Live sesh on</b> — {html.escape(live)} has the deck."
        self._say(chat, _fit(txt), buttons=_btns())    # nr_full: six long lines would be REFUSED

    def cmd_play(self, chat, user, query, reply_to=None):
        uid = user.get("id")
        now = time.time()

        # nr_note (2026-09-09, SF): everything after a "|" is a message to show with the song.
        # The split happens BEFORE the length cap, so a long message can never eat the song title.
        raw = "".join(c for c in query if c >= " ")
        query, _, note = raw.partition("|")
        query = query[:MAX_QUERY_CHARS].strip()
        note = note.strip()[:_note_cap(user)]      # nr_full: the captain writes longer, nobody else
        if not query:
            return self._say(chat, "Send me a song. "
                                   "<code>/play bonobo kiara</code>\n"
                                   "Add a line to show with it: <code>/play bonobo kiara | hello from Tilburg</code>",
                             reply_to=reply_to)

        with _lock:
            last = _last_search.get(uid, 0.0)
            on_cooldown = now - last < SEARCH_COOLDOWN_S
            open_inv = _open_invoice.get(uid)
        if on_cooldown:
            # In a DM (reply_to is None) say a word, so a fast re-send never looks
            # like a dead bot; a group stays quiet to avoid noise.
            if reply_to is None:
                self._say(chat, "Give me a few seconds between searches.")
            return
        if open_inv and open_inv[3] > now:
            return self._say(chat,
                "You already have an invoice open. Zap that one, or let it "
                "expire, before asking for another song.", reply_to=reply_to)

        live = self._live_session()
        if live:
            return self._say(chat,
                f"🎛 A live sesh is on — <b>{html.escape(live)}</b> has the deck. "
                f"The jukebox reopens when the sesh ends.",
                buttons=_btns(), reply_to=reply_to)

        # Arm the cooldown only now, once a search is actually about to issue: a
        # request rejected above (invoice open, live sesh) must not extend it.
        with _lock:
            _last_search[uid] = now

        # Ask for the ship's own library first, always. When the V4V lane is
        # on, ask for it separately and append -- never let a mixed search bury
        # the 21-sat row under 210-sat rows for the same song.
        # /play random | shuffle (2026-08-31, SF): deal a hand from the ship's
        # own sampler -- the SAME index the search reads, so nothing can surface
        # here that a search could not. Same rows, same 21-sat tap-to-pay road;
        # the dice only replace the typing. New hand: send /play random again -- or tap Next past the last page,
        # which deals another hand into the same list (nr_randomdeep, 2026-10-05).
        rolling = query.lower() in ("random", "shuffle")
        if rolling:
            code, body = self.st.get("/api/sample", timeout=15, n=str(MAX_ROWS_KEPT))
        else:
            code, body = self.st.get("/api/search", timeout=15,
                                     q=query, src="library", limit=str(MAX_ROWS_KEPT))
        if code != 200:
            return self._say(chat, "Search is down for a moment. "
                                   "Try again shortly.", reply_to=reply_to)
        results = (body.get("results") or [])[:MAX_ROWS_KEPT]
        # nr_deep: how many LIBRARY rows the station has handed over -- the offset the next batch
        # starts at. Counted BEFORE the V4V lane appends below, because those rows come from the
        # other lane and the station's offset knows nothing about them. /play random deals from the
        # sampler, which has no offset; its cursor only says "deal again" (nr_randomdeep, see _top_up).
        lib_n = 0 if rolling else len(results)

        # nr_wavfirst (2026-09-12): the Wavlake lane is ALWAYS asked (a common phrase fills the page with
        # library rows, and the V4V artist was never shown). A Wavlake row whose artist+title carry every
        # word of the query is an exact ask and goes FIRST; the others follow the library rows if there is room.
        if V4V_ENABLED and not rolling:
            def _key(t):
                return (f"{(t.get('artist') or '').lower()}|"
                        f"{(t.get('title') or '').lower()}")
            have = {_key(t) for t in results}
            wcode, wbody = self.st.get("/api/search", timeout=20,
                                       q=query, src="wavlake")
            if wcode == 200:
                qwords = query.lower().split()
                extra = [t for t in (wbody.get("results") or []) if _key(t) not in have]
                def _exact(t):
                    hay = ((t.get("artist") or "") + " " + (t.get("title") or "")).lower()
                    return all(w in hay for w in qwords)
                first = [t for t in extra if _exact(t)][:MAX_ROWS]
                rest = [t for t in extra if not _exact(t)]
                room = max(0, MAX_ROWS - len(results) - len(first))
                results = first + results + rest[:room]
        if not results:
            return self._say(chat,
                f"Nothing aboard for <b>{html.escape(query)}</b>. The station "
                f"plays its own library, so not everything is here yet.\n"
                f"Have the track? Tag @plebroyale here and say you want to gift "
                f"it to the radio.",   # nr_botgift (SF 2026-10-24)
                reply_to=reply_to)

        exp, sid = now + ROW_TTL_S, secrets.token_urlsafe(6)
        with _lock:
            for n, t in enumerate(results, 1):
                # random, collision-proof, unguessable -- no uid%100000 bucket
                # that two users could ever land on in the same second.
                tok = "r:" + secrets.token_urlsafe(6)
                src = (t.get("source") or "library").lower()
                # guid is what the server re-resolves against Wavlake before it
                # takes a single sat -- carry it, never invent it. uid is the
                # OWNER: only they may turn this row into an invoice. (This comment
                # said "the last field" until 2026-09-10; two fields were appended
                # after it, and a stale note about this very tuple has misled once
                # already. Say which field you mean, never where it sits.)
                try:
                    d = int(t.get("duration_s") or 0)
                except (TypeError, ValueError):
                    d = 0
                # nr_version (2026-09-10): the album and the length ride along so the TAP can tell the
                # station which of four "Walk Of Life" rows it means. Without them the station takes the
                # first name match, and two of those four rows mint an invoice for a different one.
                _rows[tok] = (t.get("artist") or "", t.get("title") or "", exp,
                              src, t.get("guid") or t.get("source_uri") or "", uid, note,
                              (t.get("album") or "").strip(), d, t.get("sats", 21), n, sid, query)
            # nr_deep: the cursor, opened under the same lock as the rows it belongs to. The V4V lane
            # can only append when the library returned less than one page, and "more" can only be
            # true when it returned a full batch, so those two never overlap.
            # nr_randomdeep (2026-10-05, SF: "/play random ... will not go beyond 25 tracks. I'd like to be able to
            # browse much further"): the dice open a cursor too; Next past the hand deals another one.
            _more[sid] = (query, lib_n, True if rolling else bool(body.get("more")), exp)
        # v16 (2026-08-28, SF: four "Faith" remixes truncated to identical buttons): the FULL details
        # ride in the message and the button is just a numbered handle. Since 2026-09-10 those details
        # are a monospace TABLE WITH HEADERS, because "Money For Nothing" is a Dire Straits song AND a
        # Dire Straits album, so three dot-separated things told nobody which was which. That rendering
        # lives in _page_view, so page 1 and page 3 are drawn by exactly the same code.
        txt, grid, _total = _page_view(sid, 0)
        if not txt:
            return self._say(chat, "Search hiccuped. Try again.", reply_to=reply_to)
        # nr_note: a pasted title like "Bonobo - Kiara | Official Audio" would otherwise put "Official Audio"
        # on the site, the video feed, X and Nostr as somebody's personal message, without them ever seeing a
        # word of it. So the split is shown back HERE, before any tap and before any money (review finding).
        # The FIRST line keeps its exact shape: /gift reads the query back out of it.
        seen = ("\n\nYour line, shown with the song: <i>%s</i>" % html.escape(note)) if note else ""
        self._say(chat, txt + seen, buttons=grid, reply_to=reply_to, ttl=LIST_TTL_S)

    def _top_up(self, sid: str) -> int:
        """nr_deep: fetch the next batch of one search and append it. Returns how many rows were added.

        THE NUMBERING RULE HOLDS. A row keeps the number it was given, forever; new rows continue from
        the highest number already handed out. Nothing is renumbered, so a number somebody read three
        pages back still names the same song when they scroll back to it.

        The new rows inherit the ORIGINAL expiry, deliberately. One search then ages as one thing: a
        long walk cannot outlive the rows it started from, and the count ceiling in _gc -- which evicts
        by expiry -- can never take page 1 out from under page 9.

        A row we already hold is not appended twice. The station's ordering is stable in fact (measured
        on the live catalogue: zero mismatches between the first 200 rows at LIMIT 200 and at LIMIT
        1200), but the scanner can insert a row between two batches, and that shifts every offset after
        it. This is the guard for that, and it is cheap."""
        with _lock:
            cur = _more.get(sid)
            mine = [r for r in _rows.values() if len(r) > 12 and r[11] == sid]
        if not cur or not mine or not cur[2]:
            return 0
        query, off, _flag, exp = cur
        if len(mine) >= MAX_ROWS_DEEP:
            with _lock:                       # the wall, written down rather than re-discovered
                if sid in _more:
                    _more[sid] = (query, off, False, exp)
            return 0
        # the SAME call cmd_play makes, on the same loop, at the same measured cost (~0.4 s). The slow
        # call this loop must never make is the invoice status check -- see the "p:" branch.
        # nr_randomdeep: /play random walks on with ANOTHER hand from the sampler (no offset there; held rows are
        # skipped below exactly as for a search, so a song never shows twice in one list).
        rolling = query.lower() in ("random", "shuffle")
        if rolling:
            code, body = self.st.get("/api/sample", timeout=15, n=str(MAX_ROWS_KEPT))
        else:
            code, body = self.st.get("/api/search", timeout=15, q=query, src="library",
                                     limit=str(MAX_ROWS_KEPT), offset=str(off))
        if code != 200:
            LOG.warning("top-up %s failed: HTTP %s", sid, code)
            return 0
        batch = (body.get("results") or [])[:MAX_ROWS_KEPT]
        uid, note = mine[0][5], mine[0][6]
        added = 0
        with _lock:
            if sid not in _more:              # swept while we were on the wire
                return 0
            held = [r for r in _rows.values() if len(r) > 12 and r[11] == sid]
            seen = {((r[0] or "").lower(), (r[1] or "").lower(), (r[7] or "").lower(), r[8])
                    for r in held}
            n = max((r[10] for r in held), default=0)
            for t in batch:
                try:
                    d = int(t.get("duration_s") or 0)
                except (TypeError, ValueError):
                    d = 0
                key = ((t.get("artist") or "").lower(), (t.get("title") or "").lower(),
                       (t.get("album") or "").strip().lower(), d)
                if key in seen:
                    continue
                seen.add(key)
                n += 1
                added += 1
                tok = "r:" + secrets.token_urlsafe(6)
                _rows[tok] = (t.get("artist") or "", t.get("title") or "", exp,
                              (t.get("source") or "library").lower(),
                              t.get("guid") or t.get("source_uri") or "", uid, note,
                              (t.get("album") or "").strip(), d, t.get("sats", 21), n, sid, query)
            # nr_randomdeep: the sampler says nothing about "more" -- it goes on while a hand still brings new songs
            _more[sid] = (query, off + len(batch), (added > 0 if rolling else bool(body.get("more"))) and bool(batch), exp)
        return added

    def cmd_note(self, chat, user, text, reply_to):
        """A reply to one's own open invoice sets the message shown with the song. Returns True when this
        message was handled as a note (or as a late one), False when it was not about a note at all."""
        uid = user.get("id")
        with _lock:
            open_inv = _open_invoice.get(uid)
        if not open_inv or open_inv[1] != chat or open_inv[2] != reply_to:
            with _lock:
                late = _note_late.get((chat, reply_to))
            if late:
                # answered, not swallowed: without this the line falls through and is run as a song search
                self._say(chat, "That song is already on its way, so the line missed it. Next time send it "
                                "before you pay, or put it after a <code>|</code> in the request.",
                          ttl=MSG_TTL_S)
                return True
            return False
        ph = open_inv[0]
        raw_note = "".join(c for c in text if c >= " ").strip()
        note = raw_note[:_note_cap(user)]          # nr_full: the captain writes longer, nobody else
        if not note:
            return False
        with _lock:
            rec = _inv_note.get(ph)
            if rec is None:
                return False
            if not rec.get("on") or not rec.get("nt"):
                self._say(chat, "Messages are switched off at the moment, so I cannot add that one.",
                          ttl=MSG_TTL_S)
                return True
            rec["note"] = note
            rec["sent"] = False          # the watcher carries it on its next poll, within a few seconds
        extra = " I trimmed it to fit." if len(raw_note) > _note_cap(user) else ""
        if _note_cap(user) == NOTE_MAX and _NOTE_TIDIED.search(note):
            extra += " The station takes links out before it airs."
        self._say(chat, "Got it. <i>%s</i> goes out with your song.%s" % (html.escape(note), extra),
                  ttl=MSG_TTL_S)
        return True

    def cmd_notice(self, chat, user, rest):
        """Owner sets the standing line on the pinned card -- e.g. the old-bot
        withdrawal notice. To make it survive a restart, put the same text in
        NR_CARD_NOTICE in the env file.

        Guarded THREE ways, because this text lands on the station's trusted
        pinned card and the whole bot exists to fight withdrawal scams:
          1. the card must exist (HOME_CHAT set),
          2. the command must be sent IN the home group -- NOT any other group
             where a stranger happened to make the bot an admin, and
          3. the sender must be an admin of that home group.
        Miss any one and the notice does not change."""
        if not HOME_CHAT:
            return self._say(chat, "There is no pinned card yet. Set "
                                   "<code>NR_HOME_CHAT_ID</code> first.")
        if str(chat) != str(HOME_CHAT):
            # do not even hint elsewhere that a home card exists
            return
        if not self._is_admin(chat, user.get("id")):
            return self._say(chat, "Only a group admin can set the notice.")
        arg = rest.strip()
        if arg.lower() in ("off", "clear", "none", "-"):
            self._notice = ""
            self._say(chat, "Notice cleared. The card shows just the track.")
        elif not arg:
            cur = self._notice_line() or "(none)"
            return self._say(chat, "Current notice: " + html.escape(cur) +
                             "\nSet one: <code>/notice your text</code>. "
                             "Clear it: <code>/notice off</code>.")
        else:
            self._notice = arg[:200]
            self._say(chat, "Notice set. It rides on the pinned card in a moment.")
        # Force the card to redraw on the card thread's next tick. We do NOT call
        # _ensure_card() here: it does network I/O, and this runs on the update
        # loop, which must never block (that was the 100s-freeze bug).
        with _lock:
            self._card_text = None

    def cmd_chatid(self, chat):
        """Show this chat's id, so the owner can fill in NR_HOME_CHAT_ID."""
        self._say(chat, f"This chat id is <code>{chat}</code>.")

    def cmd_gift(self, chat, user, rest, msg):
        """Captain's thank-you: reply /gift to someone's message and their song
        queues FREE, settled instantly. Deliberately NO free-play balances or
        vouchers -- SF's no-credit law. Generosity = /gift again tomorrow.

        Guards: home group only, group admins only, must be a REPLY (that is
        how the bot knows who gets the public credit)."""
        if not HOME_CHAT or str(chat) != str(HOME_CHAT):
            return
        # fixed allowlist, never a live lookup (see GIFT_USERS above)
        if (user.get("username") or "").lower() not in GIFT_USERS:
            return
        replied = msg.get("reply_to_message") or {}
        target = replied.get("from") or {}
        if not target:
            return self._say(chat,
                "Reply to someone's message with /gift so I know who to thank. "
                "Add a song to override: <code>/gift artist title</code>.")
        # the captain's own words win; otherwise the replied message names it
        query = (rest or "").strip()
        if target.get("is_bot"):
            # replying on the bot's own results card (SF's instinct, 2026-08-14):
            # recover the query from the card's first line; credit goes uncredited
            # because Telegram does not hand us the original requester from here.
            t = (replied.get("text") or "")
            if not query and t.startswith("Found these for ") and " — tap" in t:
                query = t[len("Found these for "):t.index(" — tap")]
            target = {}
        if not query:
            query = (replied.get("text") or "").strip()
            if query.startswith("/"):
                query = query[len(query.split()[0]):].strip() if query.split() else ""
        query = "".join(c for c in query if c >= " ")[:MAX_QUERY_CHARS].strip()
        if not query:
            return self._say(chat, "That message names no music. Use "
                                   "<code>/gift artist title</code>.")

        code, body = self.st.get("/api/search", timeout=15, q=query, src="library")
        results = (body.get("results") or []) if code == 200 else []
        if not results:
            return self._say(chat, f"Nothing aboard for <b>{html.escape(query)}</b> "
                                   f"— no gift this time.")
        top = results[0]
        gcode, gbody = self.st.post("/api/admin/giftplay",
                                    {"artist": top.get("artist") or "",
                                     "title": top.get("title") or ""})
        if gcode != 200:
            return self._say(chat, "Gift failed: " +
                             html.escape(str(gbody.get("error", "unknown"))))
        if target:
            name = html.escape((target.get("first_name") or "someone")[:32])
            handle = target.get("username")
            who = f"@{html.escape(handle)}" if handle else f"<b>{name}</b>"
            head = f"🎁 <b>On the house for {who}</b>"
            tail = " Thanks for helping the ship!"
        else:
            head = "🎁 <b>On the house</b>"
            tail = ""
        self._say(chat,
            f"{head} — "
            f"{html.escape(top.get('artist') or '?')} — "
            f"{html.escape(top.get('title') or '?')} is queued.{tail}",
            ttl=LIST_TTL_S)

    # ---------------- callbacks ----------------
    def on_callback(self, cq):
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        user = cq.get("from") or {}
        cid = cq.get("id")
        who = user.get("id")

        if data.startswith("q:"):
            # public read-only, same as typing /queue -- no owner binding needed
            self.tg.answer(cid)
            return self.cmd_queue(chat)
        if data.startswith("h:"):
            self.tg.answer(cid)
            return self.cmd_history(chat)

        if data.startswith("d:"):
            # nr_drop: "I am done with those." Stops the watching and frees the slots at once.
            #
            # WHAT IT COSTS, exactly and only: the Telegram receipt. The invoices stay payable -- nothing
            # can un-mint a BOLT11 -- so if one is zapped after all, the station's paid-play sweeper
            # still queues the song and the announcer still says so. Nobody is watching for it here any
            # more, that is all. Said on the button and again in the answer, because a promise about
            # somebody's money must never be quietly narrowed.
            owner = data[2:]
            if owner and owner != str(who):
                return self.tg.answer(cid, "Not your request.")
            with _lock:
                mine_inv = [(p, v[1], v[2]) for p, v in _live.items() if v[0] == who]
            for p, c, m in mine_inv:
                # take any card that is still ON the screen with them: leaving a live QR whose bot has
                # stopped watching would give somebody a [Check my zap] button that answers nothing.
                # The ones Back already removed are deleted twice, which Telegram does not mind.
                if (c, m) != (chat, mid):
                    self.tg.delete(c, m)
                self._forget(p)
            n = len(mine_inv)
            self.tg.answer(cid, "Dropped %d. Ask for a song whenever you like." % n)
            r = self.tg.edit(chat, mid,
                "Dropped %d invoice%s. Ask for a song whenever you like.\n"
                "<i>If you zap one of them after all, your song still plays.</i>"
                % (n, "" if n == 1 else "s"), buttons=[])
            self._track(chat, mid, DEAD_TTL_S)
            return r

        if data.startswith("x:"):
            # only the requester may cancel their own search (matters in groups)
            owner = data[2:]
            if owner and owner != str(who):
                return self.tg.answer(cid, "Not your request.")
            self.tg.answer(cid)
            r = self.tg.edit(chat, mid, "Cancelled.", buttons=[])
            self._track(chat, mid, DEAD_TTL_S)   # dead list: fast broom
            return r

        if data.startswith("p:"):
            # A status check can hold for seconds. NEVER run it here: this is the
            # single update loop and it would freeze the whole bot for everyone.
            # Hand the hash to the dedicated watcher thread and return at once.
            ph = _unpack(data)
            if not ph:
                return self.tg.answer(cid, "")
            now = time.time()
            with _lock:
                watched = any(w[1] == ph for w in _watch)
                if not watched and len(_watch) < MAX_TRACKED:
                    heapq.heappush(_watch, (now, ph, chat, mid, now))
            return self.tg.answer(cid, "Looking for your zap…")

        if data.startswith("m:"):
            # nr_page: another page of a search already fetched. No API call and no new state -- the
            # rows are still in _rows and they carry their own numbers, so nothing is renumbered.
            parts = data.split(":", 2)
            if len(parts) != 3:
                return self.tg.answer(cid, "")
            sid = parts[1]
            try:
                pg = int(parts[2])
            except (TypeError, ValueError):
                return self.tg.answer(cid, "")
            with _lock:
                owner = next((r[5] for r in _rows.values() if len(r) > 12 and r[11] == sid), None)
                have = sum(1 for r in _rows.values() if len(r) > 12 and r[11] == sid)
            if owner is None:
                self.tg.answer(cid, "That search is stale — search again.")
                r = self.tg.edit(chat, mid, "That search expired. Send the song again.", buttons=[])
                self._track(chat, mid, DEAD_TTL_S)
                return r
            if owner != who:
                return self.tg.answer(cid, "Not your request.")
            # nr_deep: past the last remembered page? Go and get the next batch first. Answer the
            # callback BEFORE the fetch so Telegram stops spinning while it runs. A batch that is
            # entirely rows we already hold adds nothing, so walk on a few rather than stall; four
            # is the bound, and in practice the first one always lands.
            # nr_pagefill (2026-09-14): top up when the requested page would be INCOMPLETE, not only when it
            # starts beyond the held rows -- 24 library + 1 Wavlake row drew a lonely fifth screen ("25–25 of 25+").
            if (pg + 1) * MAX_ROWS > have:
                self.tg.answer(cid, "Fetching more…")
                for _ in range(4):
                    if self._top_up(sid):
                        break
                txt, grid, _total = _page_view(sid, pg)
                if not txt:
                    return
                # nothing new: _page_view clamps to the last page it HAS, and can_fetch is now
                # false there, so the Next button disappears by itself. That is the ending.
                return self.tg.edit(chat, mid, txt, buttons=grid)
            txt, grid, _total = _page_view(sid, pg)
            if not txt:
                return self.tg.answer(cid, "")
            self.tg.answer(cid)
            return self.tg.edit(chat, mid, txt, buttons=grid)

        if data.startswith("b:"):
            # nr_back (2026-09-10, SF: "Can we give the invoice block a 'back' button? If yes, we need to
            # cancel the invoice."): the hold IS cancelled -- that is the thing that was actually in the
            # way. The invoice is not, because it cannot be: a BOLT11 stays payable until it expires and
            # no button here can reach into a wallet that may already be paying it. The card is taken
            # off the screen and the watcher stays on the hash: if they zap it anyway the song
            # queues and a receipt is posted, exactly as the paid-play reconciliation promises.
            parts = data.split(":", 2)
            if len(parts) != 3:
                return self.tg.answer(cid, "")
            sid = parts[1]
            try:
                pg = int(parts[2])
            except (TypeError, ValueError):
                return self.tg.answer(cid, "")
            with _lock:
                owner = next((r[5] for r in _rows.values() if len(r) > 12 and r[11] == sid), None)
                # release ONLY the hold that sits on THIS card. An old Back, tapped later, must never
                # unlock an invoice minted since -- that would be a second open invoice for one person.
                inv = _open_invoice.get(who)
                mine = bool(inv and inv[1] == chat and inv[2] == mid)
                if mine:
                    _open_invoice.pop(who, None)
            if not mine and owner != who:
                return self.tg.answer(cid, "Not your request.")
            # nr_live (2026-09-10, SF: "pressing the back button, I think we can assume an enduser
            # changed their mind... without cluttering the TG chat with too much bot messages"):
            # the card GOES, and nothing is posted in its place.
            #
            # Editing a photo message can only change its CAPTION -- the QR picture itself cannot be
            # edited away -- so a "set aside" note would have left a dead QR sitting in the group
            # for every change of mind. Deleting it is the tidy answer, and it costs nothing:
            #   * the WATCH continues (that is the whole point of _live), so if they zap it after
            #     all, _finish posts the receipt as a FRESH message -- it already sends-then-
            #     deletes for photo cards, so no code there has to know this happened;
            #   * and if they never zap it, the expiry edit lands on a message that is gone, so
            #     nobody is told anything at all. Silence is the right amount of noise for a mind
            # changed. The only thing said is a toast, which lives in Telegram and not in the chat.
            #
            # The money promise is untouched. The invoice stays payable for its 15 minutes and the
            # station queues a late payment on its own -- nothing here can, or tries to, un-mint it.
            self.tg.answer(cid, "Set aside — pick another one.")
            self.tg.delete(chat, mid)
            txt, grid, _total = _page_view(sid, pg)
            if not txt:
                return
            return self._say(chat, txt, buttons=grid, ttl=LIST_TTL_S)

        if data.startswith("r:"):
            with _lock:
                row = _rows.get(data)
            if not row or row[2] < time.time():
                self.tg.answer(cid, "That search is stale — search again.")
                r = self.tg.edit(chat, mid,
                                 "That search expired. Send the song again.",
                                 buttons=[])
                self._track(chat, mid, DEAD_TTL_S)   # dead list: fast broom
                return r
            # only the requester may turn their own row into an invoice
            if len(row) > 5 and row[5] != who:
                return self.tg.answer(cid, "Not your request.")
            self.tg.answer(cid, "Making your invoice…")
            # nr_back: the page this row sits on, worked out from the number it already carries --
            # no new state, and it cannot drift out of step with the numbering rule.
            try:
                _bk = "%s:%d" % (row[11], max(0, (int(row[10]) - 1) // MAX_ROWS)) if len(row) > 12 else ""
            except (TypeError, ValueError):
                _bk = ""
            return self._mint(chat, mid, user, row[0], row[1],
                              src=row[3], guid=row[4], note=(row[6] if len(row) > 6 else ""),
                              album=(row[7] if len(row) > 7 else ""),
                              duration_s=(row[8] if len(row) > 8 else 0), back=_bk)

        return self.tg.answer(cid)      # nr_qr5: an unknown or retired button never leaves the spinner turning

    # ---------------- the money path ----------------
    def _mint(self, chat, mid, user, artist, title, src="library", guid="", note="",
              album="", duration_s=0, back=""):
        uid = user.get("id")
        now = time.time()

        with _lock:
            open_inv = _open_invoice.get(uid)
            tracked = len(_watch)
            # nr_live: how many live invoices this person is already holding, set aside or not
            n_live = sum(1 for v in _live.values() if v[0] == uid and v[3] > now)
        # nr_live: a refusal must not strand somebody on a dead message. "m:" redraws the very page
        # they tapped from, in place -- the same button Previous and Next use.
        _bkb = [[("◂ Back to the list", "m:" + back)]] if back else []
        if open_inv and open_inv[3] > now:
            return self.tg.edit(chat, mid,
                "You already have an invoice open. Zap that one first.",
                buttons=_bkb)
        if n_live >= MAX_OPEN_INVOICES:
            # nr_drop (2026-09-10, SF: "I forgot to copy the invoice of one of the selections and
            # now I'm stuck"): Back takes a card OFF the screen, so an invoice it counts here can be
            # one the person can neither see nor pay. Counting those against them with no way to act
            # is a trap, and it is mine. So say the third option out loud and give them the button.
            return self.tg.edit(chat, mid,
                "You have %d invoices open already. Zap one of them, let them expire (15 min), "
                "or drop them and start over." % n_live,
                buttons=_bkb + [[("✖ Drop the %d and start over" % n_live, "d:%d" % uid)]])

        # do not take money for a song that is already lined up
        np_key, q_keys, q_len = self._queue_state()
        key = f"{artist.lower()}|{title.lower()}"
        if key == np_key or key in q_keys:
            return self.tg.edit(chat, mid,
                f"<b>{_nm(artist)} — {_nm(title)}</b> is already "
                f"lined up. No sense paying twice.", buttons=[])
        if q_len >= QUEUE_FULL_AT:
            return self.tg.edit(chat, mid,
                "The deck is full right now. Try again in a bit.", buttons=[])

        # artist/title must go back EXACTLY as they came out of search: the
        # server re-looks-up the track and demands an exact match
        form = {"artist": artist, "title": title, "via": "tg"}
        # nr_version (2026-09-10): artist + title is NOT a unique key -- the library holds one song on
        # several albums at several lengths. These two say which row was actually tapped. Optional at
        # the station: one that does not know them ignores them and behaves exactly as it always did.
        if album:
            form["album"] = str(album)[:200]
        try:
            if int(duration_s or 0) > 0:
                form["duration_s"] = str(int(duration_s))
        except (TypeError, ValueError):
            pass
        # nr_note: the message goes in the MINT when we already have it, so a fast payment cannot beat it.
        # "who" is never sent: a Telegram profile name is not a name the person chose to publish.
        # nr_at: "via" tells the station the line was typed here, so its @ may stay a real mention in this
        # group and is taken out on every other surface. An older station simply ignores the field.
        if note:
            form["note"] = note
        # nr_full (2026-09-09, SF): "It should drop links by spammers and other people, but not by me."
        # The same two accounts that may /gift may put a link in their line. Sent at the MINT, not with the
        # note, so a line added later by replying to the invoice is covered too. The station does not take
        # our word for it: it honours this only on a direct call from the box that Caddy never touched, so
        # nobody on the website can claim it. Worst case if this bot is fully compromised: a link in a
        # shoutout. No money moves on this path -- the same trade the captain already took for /gift.
        if (user.get("username") or "").lower().lstrip("@") in GIFT_USERS:
            form["full"] = "1"
        if src == "wavlake":
            if not guid:
                return self.tg.edit(chat, mid,
                    "That Wavlake track lost its id. Search again.", buttons=[])
            form["source"] = "wavlake"
            form["guid"] = guid
        code, body = self.st.post("/api/play", form)
        if code == 409:
            return self.tg.edit(chat, mid,
                "🎛 A live sesh just started — the DJ has the deck. "
                "No zap taken.", buttons=[])
        if code == 404:
            return self.tg.edit(chat, mid,
                "That one slipped off the shelf. Search again.", buttons=[])
        if code != 200 or not body.get("bolt11"):
            return self.tg.edit(chat, mid,
                "The Lightning rail did not answer. No zap taken — "
                "try again in a minute.", buttons=[])

        ph = body["payment_hash"]
        sats = body.get("sats", 21)
        exp = now + INVOICE_TTL_S

        watch_it = tracked < MAX_TRACKED
        tail = ("Tap &amp; pay the invoice to play your song."
                if watch_it
                else "Tap the button below once you have zapped.")
        # nr_note: say it BEFORE you pay -- the station stops taking a message the moment the song queues,
        # which is about three seconds after the payment lands (review finding).
        # nr_full (2026-09-09): Telegram REFUSES a photo caption over 1024 characters, and the invoice
        # alone is 258 or more. A long line plus a long track name could push it over and the invoice
        # picture would be refused outright. So the line is trimmed to whatever room is left: SF's rule
        # is that a truncation is fine and a break is not. 200 is a deliberate over-allowance for the
        # fixed words around it (measured at 134), and over-counting is the safe direction.
        # The track name is capped for DISPLAY too: the catalogue holds 683,000 files from many sources
        # and one corrupt tag with a 500-character title would blow the caption on its own.
        _a, _t = artist[:120], title[:120]
        _room = 1024 - 200 - _tglen(body["bolt11"]) - _tglen(_a) - _tglen(_t)
        _cap_note = note[:_room] if _room > 0 else ""
        while _cap_note and _tglen(_cap_note) > _room:
            _cap_note = _cap_note[:-1]      # an emoji weighs 2 in Telegram's count, 1 in Python's
        if body.get("notes_enabled"):
            tail += (("\nYour line: <i>%s</i> — reply here to change it, before you pay."
                      % html.escape(_cap_note)) if _cap_note
                     else ("\nYour line is on the song -- it is only too long to show here."
                           if note else
                           "\nReply here with a line, before you pay, and it shows with your song."))
        elif note:
            tail += "\nMessages are switched off just now, so your line was not added."
        head = (f"<b>{_nm(_a)} — {_nm(_t)}</b>\n"
                f"⚡ <b>Zap {sats} sats</b> · expires in 15 min\n")
        # nr_qr (2026-09-08, SF: KISS): the invoice is a PICTURE now. No Zapped button. A Telegram copy button would
        # hold 256 characters and every invoice from this node is 258 or more (measured), so the code text stays the
        # copy path: FOLDED under the picture (nr_qr6, SF: "just show the folded string") -- three lines and a chevron,
        # unfold, tap, copy. The search list is deleted once the picture is up. Any failure falls back to the old text
        # message: the money path never breaks.
        btns = [] if watch_it else [[("Check my zap", _pack(ph))]]
        # nr_back (2026-09-10, SF): a way out that is not "wait fifteen minutes". It appears only when
        # we still know which page to go back to, so it can never lead nowhere. "b:<sid>:<page>" is 14
        # bytes at the widest, well under Telegram's 64-byte callback_data.
        if back:
            btns = btns + [[("◂ Back to the list", "b:" + back)]]
        inv = f"<code>{body['bolt11']}</code>"
        with _lock:
            _expire.pop((chat, mid), None)      # the list holds a live invoice from here on; the janitor keeps off
        pmid = None
        r = False       # nr_qr5: no picture at all -> the plain text invoice below (r used to be unbound here)
        png = _invoice_png(body["bolt11"], sats)     # nr_qr3: the wide picture (QR left, amount right)
        if png:
            r = self.tg.send_photo(chat, png,
                head + f"<blockquote expandable>{inv}</blockquote>\n{tail}", buttons=btns)
            if r is False:      # Telegram refused the folded form: send it plain (a transport failure is NOT retried)
                r = self.tg.send_photo(chat, png, head + f"{inv}\n<i>Tap the invoice to copy it.</i> {tail}", buttons=btns)
            pmid = (r or {}).get("message_id") if r else None
        if pmid:
            self.tg.delete(chat, mid)
            with _lock:
                _expire.pop((chat, mid), None)
            mid = pmid
        elif r is None:
            # the picture MAY have landed after a slow upload: never a second invoice; the list becomes the receipt
            self.tg.edit(chat, mid, head + "Your invoice went out as a picture just below. " + tail, buttons=btns)
        else:
            self.tg.edit(chat, mid,
                head + f"\n{inv}\n\n<i>Tap the invoice to copy it.</i> {tail}",
                buttons=btns)

        with _lock:
            _open_invoice[uid] = (ph, chat, mid, exp)
            _live[ph] = (uid, chat, mid, exp)   # nr_live: watched until paid, expired, or forgotten
            _inv_src[ph] = src   # remember the lane, for an honest V4V echo
            # nr_note: "on" is the station's real switch. note_token comes back whether messages are on or
            # off, so it can never answer that question (review finding).
            _inv_note[ph] = {"nt": body.get("note_token") or "", "note": note, "sent": bool(note),
                             "on": bool(body.get("notes_enabled"))}
            _note_late.pop((chat, mid), None)
            # this message is now a live invoice -- the janitor must never delete
            # it out from under a paying listener.
            _expire.pop((chat, mid), None)
            if watch_it:
                heapq.heappush(_watch, (now + 3, ph, chat, mid, now))

    def _check_payment(self, ph, chat, mid, started):
        """One status call. Re-arms the watcher unless we reached an end state.

        Returns True when this hash is finished with and should not be polled
        again."""
        with _lock:
            if ph in _inflight:
                return False
            _inflight.add(ph)
        params = {"payment_hash": ph}
        with _lock:
            n = _inv_note.get(ph)
            if n and n.get("on") and n.get("nt") and not n.get("sent") and n.get("note"):
                params["nt"] = n["nt"]
                params["note"] = n["note"]
        try:
            code, body = self.st.get("/api/play/status", timeout=STATUS_TIMEOUT, **params)
        finally:
            with _lock:
                _inflight.discard(ph)

        if code == 404:
            self._forget(ph)
            # an expired-invoice notice is not a receipt -- let it self-tidy.
            # nr_qr2: a QR of a dead invoice must not linger; the picture goes.
            self._finish(chat, mid,
                "That invoice expired. If you did zap it, tell the station and "
                "we will sort it out.", [], RECEIPT_GRACE_S)
            return True

        if code == 500:
            # paid, and the queue step failed. The listener's money is gone and
            # the song is not coming. Never hide this.
            self.tg.edit(chat, mid,
                "Your zap landed but the track failed to queue. <b>The station "
                "has been told</b> and will make it right.", buttons=[])
            if ph not in _relayed:      # the sweeper relay may already have said it
                if self._admin_echo(f"⚠️ ZAPPED BUT NOT QUEUED — hash {ph[:16]}… "
                                    f"chat {chat}. Refund or replay owed."):
                    _relayed.add(ph)    # marked only when it really went out
            self._forget(ph)
            return True

        if code != 200:
            return False   # transport wobble: keep waiting
        if "note" in params:
            with _lock:
                n = _inv_note.get(ph)
                # only the text that actually went is marked sent: a line replaced while this call was in
                # flight must still go out on the next poll (review finding).
                if n and n.get("note") == params["note"]:
                    n["sent"] = True
            LOG.info("note attached to %s", ph[:12])

        if body.get("queued"):
            # answer the question every payer asks next: where am I in line?
            pos = ""
            try:
                _, _, q_len = self._queue_state()
                if q_len:
                    pos = f"\nYou’re #{q_len} in the queue."
            except Exception:
                pass
            # SF 2026-08-13: the paid checkmark should linger, then leave the
            # group tidy. GROUP receipts only — _track never touches a DM, and
            # the paid-but-NOT-queued branch (code 500, above) stays untracked
            # forever: that one is money owed; it must never vanish quietly.
            # nr_qr2: the QR picture is deleted here; the receipt is plain text.
            self._finish(chat, mid,
                f"⚡ <b>Zapped and queued.</b> "
                f"{html.escape(body.get('message', ''))}{pos}",
                _btns(), RECEIPT_GRACE_S)
            msg = html.escape(str(body.get("message", "")))   # nr_esc: the listener's copy is escaped; this one was not
            # drive the stranger-upload warning off the KNOWN lane, not a guess
            # at the station's free-text message
            with _lock:
                is_v4v = _inv_src.get(ph) == "wavlake"
            self._admin_echo(("⚡ V4V QUEUED — a stranger's upload is going on air: "
                              if is_v4v else "queued via telegram: ") + msg)
            self._forget(ph)
            return True

        if body.get("paid"):
            # Paid, waiting on the station-wide 10s cooldown. MUST keep polling:
            # stopping here is what silently eats someone's sats.
            msg = body.get("message") or "Zap seen — waiting for the deck…"
            # the station's own text still says "Paid!"; reword on the way out
            msg = msg.replace("Paid!", "Zap seen!").replace("Paid", "Zapped")
            self.tg.edit(chat, mid, f"⚡ {html.escape(msg)}", buttons=[])
        return False

    _unqueued_next = 0.0

    def _relay_unqueued(self, now):
        """Every 60 s: read the server's 'paid but could not queue' list (peek, nothing removed),
        tell the admin chat about at most 5 new ones with a breath between them, then ack exactly
        those. A human hears it on the phone; nothing is retried, credited or refunded by code."""
        if not ADMIN_CHAT or now < self._unqueued_next:
            return
        self._unqueued_next = now + 60
        code, body = self.st.get("/api/admin/unqueued?peek=1", timeout=8)
        if code != 200:
            return
        done, sent = [], 0
        for it in (body or {}).get("items") or []:
            if not isinstance(it, dict):
                continue
            ph = str(it.get("payment_hash") or "")
            if not ph:
                continue
            if ph in _relayed:
                done.append(ph)        # already said (by the bot's own alarm or an earlier relay)
                continue
            if sent >= 5:
                break                  # the rest next minute: no bursts on the captain's phone
            try:
                r = self.tg.send(ADMIN_CHAT,
                    "⚠️ <b>Paid but NOT queued</b>\n"
                    + html.escape("%s — %s" % (it.get("artist") or "?", it.get("title") or "?")) + "\n"
                    + "why: " + html.escape(str(it.get("error") or "?")[:200]) + "\n"
                    + "hash: <code>" + html.escape(ph[:16]) + "…</code>\n"
                    + "Nothing was retried or credited. Refund by hand if you want to.")
            except Exception as e:
                r = None
                LOG.warning("relay send raised: %s", _safe(e))
            if r:
                _relayed.add(ph); done.append(ph); sent += 1
                time.sleep(1.1)
            else:
                LOG.error("relay could not post to the admin chat: %s", json.dumps(it)[:300])
                break
        if done:
            self.st.get("/api/admin/unqueued?ack=" + ",".join(done), timeout=8)

    _notes_next = 0.0

    def _relay_notes(self, now):
        """nr_notes_channel (2026-09-08): every 60 s, read the station's admin notes (the X announcer's
        alarms today, the watchdog's later), tell the admin chat at most 3 of them with a breath between,
        then ack exactly those. Same shape and the same restraint as _relay_unqueued."""
        if not ADMIN_CHAT or now < self._notes_next:
            return
        self._notes_next = now + 60
        code, body = self.st.get("/api/admin/notes?peek=1", timeout=8)
        if code != 200:
            return
        done, sent = [], 0
        for it in (body or {}).get("items") or []:
            if not isinstance(it, dict):
                continue
            nid = str(it.get("id") or "")
            if not nid:
                continue
            if nid in _notes_relayed:
                done.append(nid)
                continue
            if sent >= 3:
                break
            try:
                r = self.tg.send(ADMIN_CHAT, "\U0001F4E3 <b>%s</b>\n%s" % (
                    html.escape(str(it.get("src") or "note")[:32]), html.escape(str(it.get("text") or "")[:600])))
            except Exception as e:
                r = None
                LOG.warning("notes relay send raised: %s", _safe(e))
            if r:
                _notes_relayed.add(nid); done.append(nid); sent += 1
                if it.get("group") and GROUP_CHAT:
                    # nr_groupping (SF 2026-09-12): a note marked for the group goes to the public group too,
                    # tagging the two captains. The DM above keeps the details; the group gets one line.
                    try:
                        self.tg.send(GROUP_CHAT, "\U0001F4E3 @noderunnersfm @plebroyale \u2014 %s" % html.escape(str(it.get("text") or "")[:300]))
                    except Exception as e:
                        LOG.warning("group ping send raised: %s", _safe(e))
                time.sleep(1.1)
            else:
                LOG.error("notes relay could not post to the admin chat: %s", json.dumps(it)[:300])
                break
        if done:
            self.st.get("/api/admin/notes?ack=" + ",".join(done), timeout=8)

    def _forget(self, ph):
        with _lock:
            for uid, v in list(_open_invoice.items()):
                if v[0] == ph:
                    # nr_note: remember which message this was. Someone who pays first and types their line a
                    # second later then gets a straight answer, instead of having it run as a song search.
                    _note_late[(v[1], v[2])] = time.time()
                    _open_invoice.pop(uid, None)
            _live.pop(ph, None)      # nr_live: an end state stops the watching, a Back does not
            _inv_src.pop(ph, None)
            _inv_note.pop(ph, None)

    # ---------------- watcher: ONE thread, not one per invoice ----------------
    def watcher(self):
        """A single thread walking a due-time heap.

        One thread per invoice would be a self-inflicted denial of service: 100
        open invoices at a 3s interval is ~33 requests/second against a
        ThreadingHTTPServer with no rate limiter, plus an LNbits call each."""
        while not self.stop.is_set():
            now = time.time()
            # housekeeping must never kill this thread -- it also drives every
            # payment confirmation. A wobble in _gc/_sweep is logged, not fatal.
            try:
                _gc(now)
                self._sweep(now)    # tidy our own stale group messages
            except Exception as e:
                LOG.warning("housekeeping error: %s", _safe(e))
            try:
                self._relay_unqueued(now)
            except Exception as e:
                LOG.warning("unqueued relay error: %s", _safe(e))
            try:
                self._relay_notes(now)
            except Exception as e:
                LOG.warning("notes relay error: %s", _safe(e))
            item = None
            with _lock:
                if _watch and _watch[0][0] <= now:
                    item = heapq.heappop(_watch)
            if item is None:
                self.stop.wait(1.0)
                continue

            _, ph, chat, mid, started = item
            # A hash resolved on the main thread (a manual tap that reached an
            # end-state) has already been _forget()-ten. Do NOT re-poll it: that
            # is what clobbers a "Paid and queued" message with a false "expired".
            with _lock:
                # nr_live: was _open_invoice, which the Back button releases -- so a set-aside
                # invoice fell out of the watch here and was never polled again. _live is dropped
                # only by _forget (an end state) or by its own expiry in _gc.
                still_watched = ph in _live
            if not still_watched:
                continue
            age = now - started
            if age > INVOICE_TTL_S:
                # 2026-09-03: never leave a dead invoice on screen. If a payment lands anyway
                # (the captain's own wallet), the server's sweeper still queues the track.
                try:
                    self.tg.edit(chat, mid,
                        "⏳ This invoice expired (15 min). Tap /play again for a fresh one.", buttons=[])
                    self._track(chat, mid, RECEIPT_GRACE_S)   # a notice, not a receipt: let it self-tidy
                except Exception as e:
                    LOG.warning("expiry edit failed: %s", _safe(e))
                self._forget(ph)
                continue
            try:
                done = self._check_payment(ph, chat, mid, started)
            except Exception as e:
                LOG.warning("watcher error: %s", _safe(e))
                done = False
            if not done:
                # back off: eager while they are probably paying, patient after
                gap = 3 if age < 60 else (10 if age < 300 else 30)
                with _lock:
                    heapq.heappush(_watch, (time.time() + gap, ph, chat, mid,
                                            started))

    # ---------------- update loop ----------------
    def run(self):
        # Discard the backlog. Telegram redelivers unconfirmed updates for 24h;
        # starting at offset 0 would re-mint invoices for commands people sent
        # hours ago. This is the bug the existing chat relay still has.
        offset = 0
        seed = self.tg.call("getUpdates", offset=-1, timeout=0, http_timeout=20)
        if seed:
            offset = seed[-1]["update_id"] + 1
            LOG.info("discarded backlog, starting at offset %d", offset)

        # our own id + @name: the id lets a restart re-adopt its pinned card, the
        # name lets us ignore a command explicitly addressed to a DIFFERENT bot.
        me = self.tg.me()
        if me:
            self.bot_id = me.get("id")
            self.bot_username = (me.get("username") or "").lower()

        threading.Thread(target=self.watcher, daemon=True).start()
        if HOME_CHAT or ANNOUNCE_CHAT:
            threading.Thread(target=self.card_loop, daemon=True).start()
        LOG.info("announce=%s", ANNOUNCE_CHAT or "off")
        LOG.info("nr-jukebox-bot up; library lane only; no custody; "
                 "card=%s tidy=%ss", "on" if HOME_CHAT else "off",
                 MSG_TTL_S if MSG_TTL_S > 0 else "off")

        fails = 0
        while not self.stop.is_set():
            # timeout=45 goes to TELEGRAM (hold the poll open); http_timeout
            # must outlast it or requests aborts every poll.
            ups = self.tg.call("getUpdates", http_timeout=60, timeout=45,
                               offset=offset,
                               allowed_updates='["message","callback_query"]')
            if ups is None:
                fails += 1
                self.stop.wait(min(60, 2 ** min(fails, 6)))
                continue
            fails = 0
            for u in ups:
                # Advance the offset BEFORE acting. At-most-once is right here:
                # losing an update costs a re-typed search, while repeating one
                # mints a second invoice.
                offset = max(offset, u["update_id"] + 1)
                try:
                    self.handle(u)
                except Exception as e:
                    LOG.warning("update failed: %s", _safe(e))

    def handle(self, u):
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])

        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if time.time() - msg.get("date", 0) > STALE_UPDATE_S:
            return   # a redelivered command is never worth executing

        chat = (msg.get("chat") or {}).get("id")
        is_dm = (msg.get("chat") or {}).get("type") == "private"
        user = msg.get("from") or {}

        # nr_note (2026-09-09, SF): a REPLY to your own invoice is the message to show with the song. It is
        # only read that way when it replies to that exact message, so a group stays sane.
        rep = (msg.get("reply_to_message") or {}).get("message_id")
        if rep and not text.startswith("/") and self.cmd_note(chat, user, text, rep):
            return

        first = text.split()[0]
        rest = text[len(first):].strip()
        # "/queue@SomeBot" -> cmd "/queue", target "somebot". In a GROUP a command
        # addressed to another bot is not ours -- ignore it so we do not both
        # answer the same "/queue" (the old bot is still in the chat). In a DM the
        # @name is irrelevant, so we always answer there.
        cmd, _, target = first.lower().partition("@")
        if (not is_dm and target and self.bot_username
                and target != self.bot_username):
            return

        if cmd in ("/start",):
            return self.cmd_start(chat, user)
        if cmd in ("/help", "/faq"):
            return self.cmd_help(chat)
        if cmd in ("/old", "/migrate", "/withdraw", "/balance"):
            return self.cmd_old(chat)
        if cmd in ("/now", "/nowplaying"):
            return self.cmd_now(chat)
        if cmd in ("/queue", "/q"):
            return self.cmd_queue(chat)
        if cmd in ("/history", "/played", "/log"):
            return self.cmd_history(chat)
        if cmd in ("/notice",):
            return self.cmd_notice(chat, user, rest)
        if cmd in ("/chatid", "/id"):
            return self.cmd_chatid(chat)
        if cmd in ("/gift",):
            return self.cmd_gift(chat, user, rest, msg)
        if cmd in ("/listen", "/tune"):
            return self._say(chat, "🎧 <b>Noderunners Radio</b> — always on.",
                             buttons=_btns())
        if cmd in ("/play", "/add", "/request"):
            return self.cmd_play(chat, user, rest,
                                 reply_to=None if is_dm else msg.get("message_id"))
        if text.startswith("/"):
            return
        if is_dm or not GROUP_NEEDS_COMMAND:
            return self.cmd_play(chat, user, text)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _refuse_token_in_argv()
    if not TOKEN:
        sys.exit("NR_JUKEBOX_BOT_TOKEN is not set. It belongs in "
                 "/etc/nr-jukebox-bot.env at mode 0600 — never in argv.")
    # Fail CLOSED: the open Wavlake lane puts a stranger's upload on a public
    # broadcast, and its only guard is the admin echo. No admin chat, no lane.
    if V4V_ENABLED and not ADMIN_CHAT:
        sys.exit("REFUSING TO START: NR_V4V_ENABLED=1 but NR_ADMIN_CHAT_ID is "
                 "empty. Every V4V queue must be seen, not discovered. Set "
                 "NR_ADMIN_CHAT_ID, or turn the V4V lane off.")

    bot = Bot(Telegram(TOKEN), Station(API_BASE))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: bot.stop.set())
    try:
        bot.run()
    finally:
        LOG.info("nr-jukebox-bot down")


if __name__ == "__main__":
    main()
