#!/usr/bin/env python3
"""
library_api.py -- minimal HTTP API backing the site's search box.

    GET  /api/search?q=...        -> JSON list of playable local tracks
    POST /api/request             -> body: q=... ; records a miss in the wanted list
    POST /api/play                -> body: artist=...&title=... ; creates a PLAY_PRICE_SATS
                                      LNbits invoice, does NOT queue yet
    GET  /api/play/status?payment_hash=... -> polls payment; queues the track on air
                                      the first time it sees the invoice paid

    GET  /api/admin/status?token=... -> now-playing + queue, needs JUKEBOX_ADMIN_TOKEN
    POST /api/admin/skip           -> body: token=... ; skips the current on-air track
    POST /api/admin/flush          -> body: token=... ; clears the whole jukebox queue

Talks to the local SQLite library index, the shared Redis wanted-list,
Liquidsoap's request socket, and (for /api/play) LNbits directly via
LNBITS_PLAY_INVOICE_KEY -- a dedicated wallet's invoice/read key, separate
from any other LNbits wallet on this box.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import redis
import requests
from resolvers.local import LocalResolver
from resolvers.musicbrainz import MusicBrainzResolver
from resolvers.wanted import WantedQueue
from resolvers.chain import ResolverChain
from resolvers.wavlake import WavlakeResolver
from resolvers.base import Track as _WTrack, RightsClass as _WRights, Availability as _WAvail
from liquidsoap_control import push_track, on_air_rid, queue_rids, request_metadata, skip, flush_and_skip
import liquidsoap_control as _lc   # nr_rid_check: raw socket access for the resolve check
from telegram_chat_relay import TelegramChatRelay

DB_PATH = "/var/lib/jukebox/library.db"
PLAYLOG_PATH = "/var/log/liquidsoap/playlog.tsv"
PORT = 7100
# nr_rank (2026-09-10): the most library rows /api/search will hand back, and the most either play
# re-lookup will consider. ONE number, so the two can never disagree -- if search can SHOW a row, the
# money path must be able to FIND it again, or the button is dead and somebody taps nothing.
SEARCH_LIMIT_MAX = 24


def _exact_hit(local, artist, title, fits):
    """nr_reach (2026-09-10, review finding): find the tapped row in the INDEX, not in a search window.

    /api/play never trusts a caller's path -- it re-looks the track up by name. That re-lookup reads a
    24-row search window, and the catalogue has 60 artist+title clusters BIGGER than that: "unknown
    artist / untitled" is 84 rows, "Unknown Artist / Track 10" is 65, and 38 of those clusters have
    more than 24 DISTINCT lengths, so the dedupe collapses none of them. Once the bot can page past
    row 24, the row somebody TAPPED can sit outside that window -- and the caller then queues a
    different recording and prices the invoice by ITS length. A review crew reproduced exactly that.

    So when the album/length hints match nothing in the window, ask the index. Same query the ghost
    guard runs further down, no LIMIT, so there is no cliff to fall off. Returns the first row that
    fits, or None -- and None simply means the old fallback runs, so this can never invent a 404."""
    try:
        con = sqlite3.connect(local.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM tracks WHERE lower(artist)=? AND lower(title)=?",
                           (artist.lower(), title.lower())).fetchall()
        con.close()
    except Exception:
        return None            # the money path must not die because a read failed
    for r in rows:
        try:
            t = local._row_to_track(r)
        except Exception:
            continue
        if fits(t):
            return t
    return None


def _int0(v) -> int:
    """A whole number, or 0. Never raises -- a junk duration cell must not refuse a living file."""
    try:
        return int(float(v or 0))
    except Exception:
        return 0
PLAY_COOLDOWN_S = 10  # basic anti-spam: don't let requests hammer the live queue
ADMIN_TOKEN = os.environ.get("JUKEBOX_ADMIN_TOKEN")

# ---- captain login (session-cookie auth for admin.html) ----------------------
# SF logs in once with username+password (password-manager friendly); the
# password lives here only as a salted PBKDF2 hash. A signed, expiring cookie
# then unlocks every admin endpoint -- both here AND in dj_registration.py,
# which shares ADMIN_SESSION_SECRET. The old ?token= path still works as a
# fallback but is no longer needed anywhere.
ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH")          # "salthex$pbkdf2hex"
ADMIN_SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET")
SESSION_COOKIE = "nr_admin_session"
SESSION_TTL_S = 30 * 24 * 3600  # stay logged in for 30 days


def _check_password(pw: str) -> bool:
    try:
        salt_hex, hash_hex = ADMIN_PASS_HASH.split("$", 1)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 600_000)
        return hmac.compare_digest(calc.hex(), hash_hex)
    except Exception:
        return False


def _make_session() -> str:
    exp = str(int(time.time()) + SESSION_TTL_S)
    sig = hmac.new(ADMIN_SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _session_valid(val: str) -> bool:
    if not ADMIN_SESSION_SECRET or not val or "." not in val:
        return False
    exp, sig = val.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(ADMIN_SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)

PLAY_PRICE_SATS = int(os.environ.get("PLAY_PRICE_SATS", "21"))

# --- duration pricing, LIBRARY lane only (SF's ruling 2026-08-09) -----------
# A flat fee lets one cent hog an hour. This fee is anti-spam friction, so it
# scales with airtime. Gentle monotonic staircase -- a shorter track is NEVER
# dearer than a longer one (sum of rising sigmoid ramps). First cut; tunable.
_PRICE_STEPS = ((13, 3, 2.4), (21, 3, 2.0), (40, 5, 1.7), (60, 6, 1.6), (100, 8, 2.0))
_PRICE_FLAT_UNDER_S = 360
_PRICE_CAP_SATS = int(os.environ.get("PLAY_PRICE_CAP_SATS", "2100000"))

# Toggle (SF 2026-08-09): duration pricing is OFF by default -- quiet room, not
# needed. Flip it live without a restart:
#   redis-cli set jukebox:duration_pricing on     (turn the curve back on)
#   redis-cli del jukebox:duration_pricing        (back to flat 21 sats)
# PLAY_PRICE_DURATION=1 in the env changes the default when the redis key is unset.
_PRICE_DEFAULT_ON = os.environ.get("PLAY_PRICE_DURATION", "0").strip().lower() not in ("0", "off", "false", "no", "")
_price_toggle = {"t": 0.0, "on": _PRICE_DEFAULT_ON}


def _duration_pricing_on() -> bool:
    """Is length-based pricing active right now? Reads a redis toggle, cached 15s
    so a single search does not hammer redis. redis key absent -> env default."""
    import time as _t
    now = _t.monotonic()
    if now - _price_toggle["t"] > 15:
        try:
            v = rds.get("jukebox:duration_pricing")
            if v is None:
                _price_toggle["on"] = _PRICE_DEFAULT_ON
            else:
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                _price_toggle["on"] = str(v).strip().lower() in ("on", "1", "true", "yes")
        except Exception:
            pass  # any redis hiccup -> keep last known state
        _price_toggle["t"] = now
    return _price_toggle["on"]


def _play_price(duration_s) -> int:
    """Sats to queue a library track of this length. Unknown or short -> the
    flat floor; that is deliberate, an unindexed duration must never overcharge."""
    if not _duration_pricing_on():
        return PLAY_PRICE_SATS
    import math
    try:
        d = int(duration_s or 0)
    except (TypeError, ValueError):
        d = 0
    if d <= _PRICE_FLAT_UNDER_S:
        return PLAY_PRICE_SATS
    m = d / 60.0
    s = 0.0
    for c, w, h in _PRICE_STEPS:
        s += h / (1.0 + math.exp(-(m - c) / w))
    return int(min(round(PLAY_PRICE_SATS * math.exp(s)), _PRICE_CAP_SATS))

# Wavlake lane (SF's rulings, memory project_wavlake_licensing_questions):
# 210 sats for now, spool each play to /tmp, NEVER into the rotation.
WAVLAKE_PLAY_PRICE_SATS = int(os.environ.get("WAVLAKE_PLAY_PRICE_SATS", "210"))
WAVLAKE_SPOOL_DIR = "/tmp/wavlake-spool"
wavlake = WavlakeResolver()  # the appId is an identifier, not a secret
# Artist auto-forward (SF's decision 2026-07-30). After a paid Wavlake play is
# queued, the full 210 sats leave the passthrough wallet through Wavlake's
# LNURL, which splits them 90% artist / 5% Wavlake / 5% referring app (the
# ship). Custody surface (feedback_legal_caution_first): the station holds the
# sats only from invoice-paid to this forward -- seconds instead of a manual
# wait -- and every attempt lands in the ledger below (part of the royalty
# record). Needs WAVLAKE_ADMIN_KEY AND the appId; missing either, the forward
# is logged-and-skipped and SF forwards by hand, exactly like before. A FAILED
# forward is deliberately NOT retried by code (SF's simplicity ruling): it
# lands on the wavlake:payouts:owed list and in the ledger, for human eyes.
LNBITS_PLAY_ADMIN_KEY = os.environ.get("LNBITS_PLAY_ADMIN_KEY", "")
WAVLAKE_FORWARD_SATS = int(os.environ.get("WAVLAKE_FORWARD_SATS", str(WAVLAKE_PLAY_PRICE_SATS)))
WAVLAKE_PAYOUT_LOG = os.environ.get("WAVLAKE_PAYOUT_LOG",
                                    "/home/sf/LightningJukeboxBot/wavlake-payouts.tsv")
# Pre-ledger V4V history seed (EpochNative, 2026-07-29: SF forwarded 210 by
# hand before the ledger existed). Everything after lives in the ledger.
WAVLAKE_OG_SEED_SATS = int(os.environ.get("WAVLAKE_OG_SEED_SATS", "210"))
WAVLAKE_OG_SEED_PLAYS = int(os.environ.get("WAVLAKE_OG_SEED_PLAYS", "1"))
LNBITS_BASE_URL = os.environ.get("LNBITS_BASE_URL", "http://127.0.0.1:5000")
LNBITS_PLAY_INVOICE_KEY = os.environ.get("LNBITS_PLAY_INVOICE_KEY")
# Dedicated passthrough wallet (SF's call 2026-07-30): mint the 210-sat
# invoices on their OWN LNbits wallet and forward from that same wallet. Its
# balance is then exactly "sats still owed to artists" -- the cleanest audit
# and custody story. Empty values fall back to the play wallet, so the lane
# keeps working before the wallet exists.
WAVLAKE_INVOICE_KEY = os.environ.get("WAVLAKE_INVOICE_KEY") or LNBITS_PLAY_INVOICE_KEY
WAVLAKE_ADMIN_KEY = os.environ.get("WAVLAKE_ADMIN_KEY") or LNBITS_PLAY_ADMIN_KEY
LNBITS_DONATE_INVOICE_KEY = os.environ.get("LNBITS_DONATE_INVOICE_KEY")
# STAGE WALLET ("NRFM live split base"): ALL live-session money flows through it.
# Splitpayments targets live on THIS wallet; the calm-seas donate jar never splits.
# While targets are set here, the site is "in live mode": donate invoices are
# minted on the stage wallet instead of the jar.
LNBITS_STAGE_ADMIN_KEY = os.environ.get("LNBITS_STAGE_ADMIN_KEY")
# Legacy jar admin key -- kept ONLY so lever flicks can sweep any old targets
# off the jar (pre-2026-07-23 splits lived there). Safe to drop later.
LNBITS_JAR_ADMIN_KEY = os.environ.get("LNBITS_DONATE_ADMIN_KEY")
LN_ADDRESS_DOMAIN = os.environ.get("LN_ADDRESS_DOMAIN", "lnbits.plebprojects.com")
ICECAST_STATUS_URL = os.environ.get("ICECAST_STATUS_URL", "http://127.0.0.1:8000/status-json.xsl")
# Free-text "Request it" (the wanted list) is the only place an outsider can
# store text on this station. SF found strangers testing the site 2026-07-29
# and ordered it shut until it has been thought through. Default CLOSED:
# set WANTED_LIST_OPEN=1 in the service env to reopen it, nothing else needed.
WANTED_LIST_OPEN = os.environ.get("WANTED_LIST_OPEN", "0") == "1"
DJ_SPLIT_PERCENT_DEFAULT = int(os.environ.get("DJ_SPLIT_PERCENT", "25"))  # rebuild rate: 75/25
PENDING_PLAY_TTL_S = 900  # the LNbits invoice must be paid within 15 minutes or it's dead
# 2026-09-03 (SF: "when I pay, it plays"): the play RECORD outlives the invoice by a day, so a payment
# that lands after the 15 minutes -- an LNbits-internal payment from the captain's own wallet, which
# the node never refuses -- still finds its track and queues it via the sweeper. Outside payers are
# refused by the node after 15 min as before, so this costs nothing and loses nothing.
PENDING_RECORD_TTL_S = 86400
_SWEEP_LATE_GAP_S = 300      # after the invoice expired: ask LNbits about it every 5 min, not every 5 s
_sweep_next: dict = {}       # payment_hash -> earliest next LNbits check (wall clock); pruned every pass
_unqueued_seen: set = set()  # payment hashes already reported to play:unqueued (one entry each per process)


def _record_ttl(payment_hash: str) -> int:
    """Remaining life of a play record, never shorter than 10 min (so a rewrite cannot kill it early)."""
    try:
        t = int(rds.ttl(_pending_play_key(payment_hash)) or 0)
    except Exception:
        t = 0
    return max(t, 600)


def _is_terminal(message: str) -> bool:
    """Only these answers from _try_queue mean 'this paid track will never air by itself':
    the drop cap was reached, the file is gone from the disks, or the record expired.
    Everything else (a first drop, a refused socket during a liquidsoap restart, a CDN hiccup)
    is transient and keeps the retry ladder it always had."""
    m = message or ""
    return ("not retrying" in m) or ("file missing from the library disks" in m) or (m == "expired")

rds = redis.Redis(db=2)
wanted = WantedQueue(rds=rds)
chain = ResolverChain(resolvers=[LocalResolver(DB_PATH)], wanted=wanted, base_price=21)
musicbrainz = MusicBrainzResolver()

TELEGRAM_CHAT_BOT_TOKEN = os.environ.get("TELEGRAM_CHAT_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # e.g. -1001672416970; optional filter
telegram_chat = None
if TELEGRAM_CHAT_BOT_TOKEN:
    telegram_chat = TelegramChatRelay(
        token=TELEGRAM_CHAT_BOT_TOKEN,
        chat_id=int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None,
        rds=rds,
    )
    telegram_chat.start()
_last_play_at = 0.0


def _catalog_names(path: str):
    """nr_names (2026-09-08): (artist, title) from library.db for a file path, or None. A fresh read-only connection
    per call, like the search; never raises."""
    if not path or not path.startswith("/"):
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT artist, title FROM tracks WHERE path=?", (path,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    a, t = (row[0] or "").strip(), (row[1] or "").strip()
    if a.casefold() in ("unknown artist", "unknown"):     # the scanner's placeholder is not an attribution
        a = ""
    if t.casefold() in ("unknown track", "unknown"):
        t = ""
    return (a, t)


def _track_summary(rid: str) -> dict:
    m = request_metadata(rid)
    artist = (m.get("artist") or "").strip()
    title = (m.get("title") or "").strip()
    # Untagged files used to air as nameless ghosts: an outsider paid to play
    # "u mad bro" on 2026-07-29 and the feed, queue and log all showed blanks.
    # nr_names (2026-09-08): a file with an artist tag but NO title tag aired as
    # "Unknown track" although the catalog knew its name. When either is missing,
    # ask the catalog first; then read the filename the way a human would.
    # the player's "filename" is the real path, used as is (a "?" is legal in a file name); only a bare
    # initial_uri may carry a query part
    path = (m.get("filename") or "").strip() or (m.get("initial_uri") or "").strip().split("?")[0]
    if not title or not artist:
        names = _catalog_names(path)
        if names:
            artist = artist or names[0]
            title = title or names[1]
    if not title:
        if path:
            base = os.path.splitext(os.path.basename(path))[0].replace("_", " ").strip()
            # the same prefix rule as the scanner and the play-log hook: "03. ", "12 - ", "A2. ", "B1 - "
            base = re.sub(r"^(?:[0-9]{1,3}|[A-Da-d][0-9]{1,2})[.)]?[ _-]+", "", base, count=1).strip()
            if " - " in base:
                a, t = base.split(" - ", 1)
                artist = artist or a.strip()
                title = t.strip() or base
            else:
                title = base or "Unknown track"
        else:
            title = "Unknown track"
    if artist.casefold() in ("unknown artist", "unknown"):
        artist = ""
    return {
        "rid": rid,
        "artist": artist or "",
        "title": title or "Unknown track",
        "album": m.get("album"),
        "status": m.get("status"),
        "filename": m.get("filename") or "",
        "initial_uri": m.get("initial_uri") or "",
    }


def _spool_wavlake(pending: dict) -> str:
    """Download the paid track to /tmp before it airs. The CDN stuttered on
    air (2026-07-29), so each play gets local buffering headroom: one song of
    disk, never the library. Old spools are swept -- delete after the play."""
    os.makedirs(WAVLAKE_SPOOL_DIR, exist_ok=True)
    now = time.time()
    for name in os.listdir(WAVLAKE_SPOOL_DIR):
        p = os.path.join(WAVLAKE_SPOOL_DIR, name)
        try:
            if now - os.path.getmtime(p) > 7200:
                os.remove(p)
        except OSError:
            pass
    path = os.path.join(WAVLAKE_SPOOL_DIR, pending["guid"] + ".mp3")
    if os.path.exists(path):
        return path
    resp = requests.get(pending["media_url"], stream=True, timeout=60,
                        headers={"User-Agent": "NoderunnersRadioJukebox/0.1"})
    resp.raise_for_status()
    part = path + ".part"
    with open(part, "wb") as fh:
        for chunk in resp.iter_content(1 << 16):
            fh.write(chunk)
    os.replace(part, path)
    return path


def _set_wavlake_notice(pending: dict) -> None:
    """Feed the Broadcast bar (SF's ruling): show that a Wavlake song was
    requested, and show the split. Wavlake's split: 90% artist, 5% Wavlake,
    5% referring app."""
    rds.setex("wavlake:notice", 600, json.dumps({
        "artist": pending.get("artist", ""), "title": pending.get("title", ""),
        "sats": WAVLAKE_PLAY_PRICE_SATS, "ts": int(time.time()),
    }))


def _create_play_invoice(memo: str, sats: int = PLAY_PRICE_SATS, key: str = "") -> dict:
    resp = requests.post(
        f"{LNBITS_BASE_URL}/api/v1/payments",
        headers={"X-Api-Key": key or LNBITS_PLAY_INVOICE_KEY, "Content-Type": "application/json"},
        json={"out": False, "amount": sats, "memo": memo,
              "expiry": PENDING_PLAY_TTL_S},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "payment_hash": data.get("payment_hash") or data.get("checking_id"),
        "bolt11": data.get("payment_request") or data.get("bolt11"),
    }


def _is_play_invoice_paid(payment_hash: str, key: str = "") -> bool:
    resp = requests.get(
        f"{LNBITS_BASE_URL}/api/v1/payments/{payment_hash}",
        headers={"X-Api-Key": key or LNBITS_PLAY_INVOICE_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    paid = data.get("paid")
    if paid is None:
        # some LNbits versions nest status under "details" or use "status": "success"
        paid = data.get("status") == "success" or bool(data.get("details", {}).get("paid"))
    return bool(paid)


def _create_donate_invoice(amount_sats: int, name: str = "", message: str = "", target: str = "") -> dict:
    what = "live-session tip" if target == "live" else "donation"
    memo = f"Noderunners Radio {what} from {name}" if name else f"Noderunners Radio {what}"
    if message:
        memo += f": {message}"
    # WHERE THE SATS LAND IS THE DONOR'S CHOICE (two buttons on the site, 2026-07-28):
    #   target=live     -> stage wallet, so it joins tonight's pot and the crew's
    #                      share of it. Only offered while a session is open.
    #   target=station  -> the donate jar, even mid-session: this one is the
    #                      ship's own keep and is NOT split with the DJs.
    #   no target       -> the original behaviour, kept so older pages and any
    #                      other caller still work: session open ? stage : jar.
    if target == "live":
        key = LNBITS_STAGE_ADMIN_KEY
    elif target == "station":
        key = LNBITS_DONATE_INVOICE_KEY
    else:
        key = LNBITS_STAGE_ADMIN_KEY if _stage_live() else LNBITS_DONATE_INVOICE_KEY
    resp = requests.post(
        f"{LNBITS_BASE_URL}/api/v1/payments",
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        json={"out": False, "amount": amount_sats, "memo": memo},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "payment_hash": data.get("payment_hash") or data.get("checking_id"),
        "bolt11": data.get("payment_request") or data.get("bolt11"),
    }


def _is_donate_invoice_paid(payment_hash: str) -> bool:
    # the invoice may live on the jar OR the stage wallet -- check both
    last_err = None
    for key in (LNBITS_DONATE_INVOICE_KEY, LNBITS_STAGE_ADMIN_KEY):
        if not key:
            continue
        try:
            resp = requests.get(
                f"{LNBITS_BASE_URL}/api/v1/payments/{payment_hash}",
                headers={"X-Api-Key": key},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            paid = data.get("paid")
            if paid is None:
                paid = data.get("status") == "success" or bool(data.get("details", {}).get("paid"))
            return bool(paid)
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("no donate wallet keys configured")


_donate_recent_cache = {"ts": 0.0, "items": None}


LINK_ALLOW_FILE = "/etc/nr-link-allow.txt"
_link_allow = {"ts": 0.0, "hosts": None}
# nr_linkallow (2026-09-10): a URL in a donation memo goes onto a LIVE BROADCAST sixty pixels tall, and
# until now nothing filtered it -- _donate_recent's "Sanitized" meant it exposed four fields and cut them
# to length, nothing more. Anyone paying 21 sats could put any link in front of the audience.
# SF's ruling: "I think things on plebprojects.com and bitcoinwiki.nl should be whitelisted for the
# messages." So the destination is what earns the exception, because a memo carries no identity to trust.
_URL_RE = re.compile(
    r"""(?ix)
    \b(?:
        (?:https?://|www\.)[^\s<>"']+          # a real URL, or a www. one
      |
        [a-z0-9](?:[a-z0-9-]*[a-z0-9])?          # or a bare domain someone typed
        (?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*
        \.(?:com|net|org|nl|io|de|be|co|me|xyz|app|dev|fm|tv|link|site|info|biz|ru|cn|top|club|online|live|cash|money|finance)
        (?:/[^\s<>"']*)?
    )""")


def _link_hosts():
    """The allow-list, re-read at most once a minute so SF can add a friend's site without a restart.
    NO FILE MEANS ALLOW NOTHING -- the safe direction for something that paints on a live broadcast."""
    now = time.time()
    if _link_allow["hosts"] is not None and now - _link_allow["ts"] < 60:
        return _link_allow["hosts"]
    hosts = set()
    try:
        with open(LINK_ALLOW_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.split("#", 1)[0].strip().lower().lstrip(".")
                if ln.startswith("http://") or ln.startswith("https://"):
                    ln = ln.split("//", 1)[1]
                ln = ln.split("/", 1)[0].strip()
                if ln and "." in ln:
                    hosts.add(ln)
    except OSError:
        pass
    _link_allow["ts"] = now
    _link_allow["hosts"] = hosts
    return hosts


def _host_allowed(host, hosts):
    """Exact domain or a subdomain of it. NEVER a substring: 'coinjoin.nl' must pass www.coinjoin.nl
    and refuse coinjoin.nl.evil.ru, which is exactly how a careless allow-list gets walked through."""
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("www.") and host[4:] in hosts:
        return True
    for d in hosts:
        if host == d or host.endswith("." + d):
            return True
    return False


def _strip_links(text):
    """Drop every link whose host is not on the list. The donor's WORDS always survive -- only the
    address goes -- so a message never turns into a refusal, it just stops being a signpost."""
    if not text or ("." not in text):
        return text
    hosts = _link_hosts()

    def _keep(m):
        raw = m.group(0)
        h = raw
        if "//" in h:
            h = h.split("//", 1)[1]
        h = h.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
        return raw if _host_allowed(h, hosts) else ""

    out = _URL_RE.sub(_keep, text)
    return " ".join(out.split())


def _donate_recent(limit=10):
    """Recent incoming donations across jar + stage, newest first -- feeds the overlay and the
    site alert popups. Exposes ONLY amount, when, and the name/message the donor typed. Cached ~10s.

    nr_linkallow (2026-09-10): this used to say "Sanitized", and that word was doing no work -- it
    meant four fields, truncated. Whatever a payer typed went onto a LIVE BROADCAST at sixty pixels.
    Links are now dropped unless their host is in /etc/nr-link-allow.txt. The words always survive."""
    now = time.time()
    if _donate_recent_cache["items"] is not None and now - _donate_recent_cache["ts"] < 10:
        return _donate_recent_cache["items"]
    items = []
    for key in (LNBITS_DONATE_INVOICE_KEY, LNBITS_STAGE_ADMIN_KEY):
        if not key:
            continue
        try:
            resp = requests.get(
                f"{LNBITS_BASE_URL}/api/v1/payments?limit=50",
                headers={"X-Api-Key": key},
                timeout=15,
            )
            resp.raise_for_status()
            for p in resp.json():
                amount = p.get("amount", 0)
                if amount <= 0:
                    continue
                if p.get("pending") is True or p.get("status") in ("pending", "failed"):
                    continue
                memo = p.get("memo") or ""
                extra = p.get("extra") or {}
                name, message = "", ""
                # site donations AND live tips: "Noderunners Radio donation
                # from {name}: {msg}" / "... live-session tip from {name}: {msg}"
                # (live memo said "tip from" -- names drowned all sesh night
                # 2026-07-30 until this line learned the second phrase)
                if "donation from " in memo or "tip from " in memo:
                    rest = memo.split(" from ", 1)[1]
                    name, _, message = rest.partition(": ")
                # lnurlp/zap comments ride in extra
                if not message:
                    c = extra.get("comment")
                    if isinstance(c, list):
                        c = c[0] if c else ""
                    message = c or ""
                # nr_linkallow: filtered HERE, at the source, so the overlay and the site's own
                # donation popups are covered by one rule that cannot drift apart.
                items.append({"ts": p.get("time") or 0, "sats": amount // 1000,
                              "name": _strip_links(name.strip())[:40],
                              "message": _strip_links(str(message).strip())[:255]})
        except Exception:
            pass
    items.sort(key=lambda x: x["ts"], reverse=True)
    items = items[:limit]
    _donate_recent_cache["ts"] = now
    _donate_recent_cache["items"] = items
    return items


# The radio raised ~3M sats across its pre-LNbits life (Geyser + years of
# donations -- what actually sits in the real jar). The payment-history sum
# below only sees the new plumbing, so the public "sats raised" adds this
# baseline. Override with env DONATE_BASELINE_SATS when SF refines the figure.
DONATE_BASELINE_SATS = int(os.environ.get("DONATE_BASELINE_SATS", "3000000"))
# lifetime sats already paid to DJs before settlement logging existed (2026-07-24, SF-confirmed)
DJ_PAID_SEED_SATS = int(os.environ.get("DJ_PAID_SEED_SATS", "13516"))

_donate_total_cache = {"ts": 0.0, "sats": None}


def _donate_total_sats() -> int:
    """Lifetime sats RECEIVED across jar + stage, from payment history --
    NOT balances. Balances undercount whenever the stage forwards the DJ
    share out; 'sats raised' must mean what listeners actually gave.
    Cached ~20s -- the site and console poll this often."""
    now = time.time()
    if _donate_total_cache["sats"] is not None and now - _donate_total_cache["ts"] < 20:
        return _donate_total_cache["sats"] + DONATE_BASELINE_SATS
    total_msat = 0
    seen_any = False
    for key in (LNBITS_DONATE_INVOICE_KEY, LNBITS_STAGE_ADMIN_KEY):
        if not key:
            continue
        try:
            resp = requests.get(
                f"{LNBITS_BASE_URL}/api/v1/payments?limit=10000",
                headers={"X-Api-Key": key},
                timeout=15,
            )
            resp.raise_for_status()
            for p in resp.json():
                amount = p.get("amount", 0)          # msat; negative = outgoing
                if amount <= 0:
                    continue
                if p.get("pending") is True or p.get("status") in ("pending", "failed"):
                    continue
                total_msat += amount
            seen_any = True
        except Exception:
            pass
    if not seen_any:
        raise RuntimeError("could not read any donate wallet payments")
    sats = total_msat // 1000
    _donate_total_cache["ts"] = now
    _donate_total_cache["sats"] = sats
    return sats + DONATE_BASELINE_SATS


def _splitpayments_get_targets(key=None):
    """Current Splitpayments targets on the stage wallet ([] = no split active)."""
    resp = requests.get(
        f"{LNBITS_BASE_URL}/splitpayments/api/v1/targets",
        headers={"X-Api-Key": key or LNBITS_STAGE_ADMIN_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() or []


def _splitpayments_set_targets(targets, key=None):
    """Replace the target list. Empty list turns the split off entirely."""
    key = key or LNBITS_STAGE_ADMIN_KEY
    resp = requests.put(
        f"{LNBITS_BASE_URL}/splitpayments/api/v1/targets",
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        json={"targets": targets},
        timeout=15,
    )
    if resp.status_code >= 300 and not targets:
        # some splitpayments versions clear via DELETE instead of an empty PUT
        resp = requests.delete(
            f"{LNBITS_BASE_URL}/splitpayments/api/v1/targets",
            headers={"X-Api-Key": key},
            timeout=15,
        )
    resp.raise_for_status()


def _sweep_jar_targets():
    """Best-effort: clear any leftover split targets from the OLD donate jar
    (splits lived there before the stage wallet took over). Never raises."""
    if not LNBITS_JAR_ADMIN_KEY:
        return
    try:
        _splitpayments_set_targets([], key=LNBITS_JAR_ADMIN_KEY)
    except Exception:
        pass


# ---- settlement-session model (whole sats, no dust -- SF's call 2026-07-23) --
# Lever ON opens a session: donations pile up in the stage wallet as a pot.
# Lever OFF settles: the DJ pool's percent of the pot is divided evenly, each
# share FLOORED to whole sats and paid to the DJ's Lightning Address (same-
# instance = internal, no fees). Every remainder stays with the radio.
# Splitpayments instant-forwarding is retired; sweeps below keep it dead.

LIVESESSION_KEY = "livesession:pool"


def _stage_balance_msat() -> int:
    resp = requests.get(
        f"{LNBITS_BASE_URL}/api/v1/wallet",
        headers={"X-Api-Key": LNBITS_STAGE_ADMIN_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    return int(resp.json().get("balance", 0))


def _load_session():
    raw = rds.get(LIVESESSION_KEY)
    return json.loads(raw) if raw else None


def _sess_update(mutate):
    """Atomic read->mutate->write of the live session (redis WATCH/MULTI), so two
    hands can never interleave on the money roster (bit us 2026-08-25: Silly Goose
    could not be removed and got paid). mutate(sess) returns the new session, or
    None to abort. Every mutation refuses while the session is settling.
    Returns (status, sess): "ok" | "none" | "closing" | "aborted" | "conflict"."""
    for _ in range(8):
        pipe = rds.pipeline()
        try:
            pipe.watch(LIVESESSION_KEY)
            raw = pipe.get(LIVESESSION_KEY)
            if raw is None:
                return "none", None
            sess = json.loads(raw)
            if sess.get("closing"):
                return "closing", sess
            new = mutate(sess)
            if new is None:
                return "aborted", sess
            pipe.multi()
            pipe.set(LIVESESSION_KEY, json.dumps(new))
            pipe.execute()
            return "ok", new
        except redis.WatchError:
            continue
        finally:
            try:
                pipe.reset()
            except Exception:
                pass
    return "conflict", None


# --- Wavlake deck (2026-08-19): the widest pool their PUBLIC api exposes ----------------
# There is no catalog-dump endpoint: only search and /content/rankings. Probed 2026-08-19 --
# sort=sats works for days 1/7/30/90 at limit=100; days=180/365, limit=200 and sort=recent all
# answer 500. So the deck is the UNION of those four windows, deduped by track id: a few
# hundred tracks instead of 40, reshuffled on every tap of SHUFFLE. Cached 15 min.
_WAV_TOP = {"t": 0.0, "rows": []}
_WAV_WINDOWS = (1, 7, 30, 90)
_WAV_BUDGET_S = 14.0


def _wavlake_top(n: int):
    """n random rows out of the pooled Wavlake deck. Returns None only when nothing is
    cached and the feed does not answer at all."""
    import random as _random
    now = time.time()
    if now - _WAV_TOP["t"] > 900 or not _WAV_TOP["rows"]:
        started = now
        seen, rows = set(), []
        for days in _WAV_WINDOWS:
            if time.time() - started > _WAV_BUDGET_S:
                break                      # never hold an HTTP handler hostage
            try:
                r = requests.get("https://wavlake.com/api/v1/content/rankings",
                                 params={"sort": "sats", "days": days, "limit": 100}, timeout=5)
                r.raise_for_status()
                items = r.json() or []
            except Exception:
                continue                   # one bad window must not empty the deck
            for it in items:
                if not isinstance(it, dict) or not it.get("id") or not it.get("title"):
                    continue
                gid = str(it.get("id"))
                if gid in seen:
                    continue
                seen.add(gid)
                rows.append({"artist": it.get("artist") or "", "title": it.get("title") or "",
                             "album": it.get("albumTitle") or "", "source": "wavlake",
                             "guid": gid, "sats": WAVLAKE_PLAY_PRICE_SATS,
                             "duration_s": it.get("duration")})
        if rows:
            _WAV_TOP["rows"] = rows
            _WAV_TOP["t"] = time.time()
    rows = _WAV_TOP["rows"]
    if not rows:
        return None
    return _random.sample(rows, min(n, len(rows)))


# --- captain's console: MUSIC + CHAT tabs (2026-08-23, SF: "buttons, not commands").
# Review-hardened same day: every deck/pool mutation holds BOTH a thread lock (concurrent
# clicks) and an flock on /var/lib/jukebox/.deck.lock shared with nr-shuffle (concurrent
# reshuffle); edits only ever touch the deck BEYOND cursor+WINDOW, so the play-count cursor
# arithmetic never sees shifted indices; every decision is journalled to console-adds.txt /
# console-removes.txt, which nr-rebuild-all consults, so a rebuild cannot silently undo the
# captain. NOTE: deploying this orphans the parked plan/nr-notes.py chain -- re-anchor it on
# this build before it ever runs.
import fcntl as _fcntl
import tempfile
ROTATION_FULL = "/var/lib/jukebox/rotation-full.m3u"
ROTATION_DECK = "/var/lib/jukebox/deck.m3u"
ROTATION_CUR  = "/var/lib/jukebox/deck.cursor"
DECK_LOCKFILE = "/var/lib/jukebox/.deck.lock"
CONSOLE_ADDS    = "/var/lib/jukebox/console-adds.txt"
CONSOLE_REMOVES = "/var/lib/jukebox/console-removes.txt"
_ROT_WINDOW = int(os.environ.get("WINDOW_SIZE", "800") or "800")   # same knob nr-shuffle reads
_ROT_TLOCK = threading.Lock()
_ROT_CACHE = {"t": 0.0, "s": set()}


class _DeckLock:
    """Thread lock + file lock, so console clicks serialize against each other AND against
    the 6-hourly nr-shuffle run (which takes the same flock)."""
    def __enter__(self):
        _ROT_TLOCK.acquire()
        self._fh = None
        try:
            self._fh = open(DECK_LOCKFILE, "a")
            _fcntl.flock(self._fh, _fcntl.LOCK_EX)
        except OSError:
            pass                     # no lockfile is a degradation, never a refusal
        return self
    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                _fcntl.flock(self._fh, _fcntl.LOCK_UN)
                self._fh.close()
        except OSError:
            pass
        _ROT_TLOCK.release()
        return False


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [l.rstrip("\n") for l in fh]
    except OSError:
        return []


def _read_set(path):
    return {l for l in _read_lines(path) if l.strip() and not l.startswith("#")}


def _read_cursor():
    raw = (_read_lines(ROTATION_CUR) or [""])[0].strip()
    return int(raw) if raw.isdigit() else 0


def _rot_set():
    now = time.time()
    if now - _ROT_CACHE["t"] > 60:
        _ROT_CACHE["s"] = {l for l in _read_lines(ROTATION_FULL) if l.strip()}
        _ROT_CACHE["t"] = now
    return _ROT_CACHE["s"]


def _chown_like(path, st):
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass                         # non-root run: owner stays, mode still set below


def _rot_daily_backup():
    """One backup of pool+deck+cursor per day; every click is undoable to this morning."""
    day = time.strftime("%Y%m%d")
    import shutil as _sh
    for p in (ROTATION_FULL, ROTATION_DECK, ROTATION_CUR):
        bak = "%s.bak-console-%s" % (p, day)
        if os.path.exists(p) and not os.path.exists(bak):
            _sh.copy2(p, bak)
            try:
                _chown_like(bak, os.stat(p))
            except OSError:
                pass


def _rot_write(path, lines):
    """Atomic write via a unique temp name; owner and mode kept where possible."""
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    _chown_like(tmp, st)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)
    _ROT_CACHE["t"] = 0.0


def _journal_intent(path, add):
    """The captain's decision, durable: nr-rebuild-all replays these after any rebuild."""
    keep, drop = (CONSOLE_ADDS, CONSOLE_REMOVES) if add else (CONSOLE_REMOVES, CONSOLE_ADDS)
    s = _read_set(keep)
    if path not in s:
        with open(keep, "a", encoding="utf-8") as fh:
            fh.write(path + "\n")
    d = _read_set(drop)
    if path in d:
        rest = [l for l in _read_lines(drop) if l != path]
        try:
            _rot_write(drop, rest)
        except OSError:
            pass


def _rot_add(path):
    import sqlite3 as _sq, random as _rand
    con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    row = con.execute("SELECT artist, title FROM tracks WHERE path=?", (path,)).fetchone()
    con.close()
    if not row:
        return False, "that path is not in the library index -- only indexed tracks rotate"
    if not os.path.exists(path):
        return False, "the file is not on disk right now -- refusing"
    if path in _read_set("/var/lib/jukebox/blocklist.txt"):
        return False, "this track was EXPELLED (blocklist.txt) -- un-expel it first"
    with _DeckLock():
        if path in _rot_set():
            return False, "already in the rotation pool"
        _rot_daily_backup()
        with open(ROTATION_FULL, "a", encoding="utf-8") as fh:
            fh.write(path + "\n")
        _journal_intent(path, add=True)
        note = "in the pool; it joins the deck at the next reshuffle"
        try:
            deck = _read_lines(ROTATION_DECK)
            if deck and deck[-1] == "":
                deck.pop()
            cur = _read_cursor()
            lo = min(cur + _ROT_WINDOW, len(deck))   # never inside the counted/dealt band (S3)
            pos = _rand.randint(lo, len(deck))
            deck.insert(pos, path)
            _rot_write(ROTATION_DECK, deck)
            days = max(round((pos - cur) * 4 / 60 / 24), 1)
            note = "in the pool and dealt into this cycle (rough guess: on air in ~%d day(s), if rotation keeps the air)" % days
        except (OSError, ValueError):
            pass
        _ROT_CACHE["t"] = 0.0
    return True, "%s -- %s" % (" - ".join(x for x in row if x), note)


def _rot_remove(path):
    with _DeckLock():
        if path not in _rot_set():
            return False, "not in the rotation pool"
        _rot_daily_backup()
        _rot_write(ROTATION_FULL, [l for l in _read_lines(ROTATION_FULL) if l.strip() and l != path])
        _journal_intent(path, add=False)
        try:
            deck = _read_lines(ROTATION_DECK)
            cur = _read_cursor()
            keep_to = min(cur + _ROT_WINDOW, len(deck))  # the dealt band keeps its indices (S3)
            kept = deck[:keep_to] + [l for l in deck[keep_to:] if l != path]
            if len(kept) != len(deck):
                _rot_write(ROTATION_DECK, kept)
        except (OSError, ValueError):
            pass
        _ROT_CACHE["t"] = 0.0
    return True, "out of the pool; it can keep airing for up to ~2 days (already dealt), then never"


_STATS = {"t": 0.0, "n": 0}   # /api/stats cache: the index count moves rarely, ask sqlite hourly


def _read_plays(n: int) -> list:
    """The last n aired tracks, newest first. One parser shared by /api/history
    and /api/rss, so the log format only ever has to be understood in one place."""
    plays = []
    try:
        with open(PLAYLOG_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 6 or parts[1] != "play":
                    continue
                ts, _event, _source, _rights, artist, title = parts
                if not artist and not title:
                    continue                          # skip blank transition rows
                plays.append({"ts": ts, "artist": artist, "title": title})
    except FileNotFoundError:
        pass
    return plays[-n:][::-1]


def _stage_live() -> bool:
    """Live mode = a settlement session is currently open."""
    try:
        return _load_session() is not None
    except Exception:
        return False


def _paying_djs(sess) -> list:
    """The pool members who actually get paid. Crew entries ("for the love of
    it", SF's walk-up canon + 2026-07-30 ruling) show on the overlay and the
    site like everyone else, but the settlement math never sees them."""
    return [d for d in sess.get("djs", []) if d.get("address") and not d.get("nopay")]


def _session_pot(sess) -> dict:
    """Pot so far (sats) + projected whole-sat share per PAYING DJ. Read-only."""
    pot_msat = max(0, _stage_balance_msat() - int(sess.get("baseline_msat", 0)))
    n = max(1, len(_paying_djs(sess)))
    per_dj_sats = int(pot_msat * int(sess.get("percent", DJ_SPLIT_PERCENT_DEFAULT)) / 100 / n) // 1000
    return {"pot_sats": pot_msat // 1000, "per_dj_sats": per_dj_sats}


def _sweep_stage_targets():
    """Defensive: the dust-era instant splits must never fire again -- clear any
    Splitpayments targets still set on the stage wallet. Never raises."""
    if not LNBITS_STAGE_ADMIN_KEY:
        return
    try:
        _splitpayments_set_targets([], key=LNBITS_STAGE_ADMIN_KEY)
    except Exception:
        pass


def _lnurlp_pay_address(address: str, sats: int) -> str:
    """Pay a Lightning Address from the stage wallet, whole sats only.
    Same-instance addresses settle internally (no routing fees)."""
    name, _, domain = address.partition("@")
    resp = requests.get(f"https://{domain}/.well-known/lnurlp/{name}", timeout=15)
    resp.raise_for_status()
    callback = resp.json().get("callback")
    if not callback:
        raise RuntimeError(f"{address}: no lnurlp callback")
    sep = "&" if "?" in callback else "?"
    resp = requests.get(f"{callback}{sep}amount={sats * 1000}", timeout=15)
    resp.raise_for_status()
    bolt11 = resp.json().get("pr")
    if not bolt11:
        raise RuntimeError(f"{address}: lnurlp gave no invoice")
    resp = requests.post(
        f"{LNBITS_BASE_URL}/api/v1/payments",
        headers={"X-Api-Key": LNBITS_STAGE_ADMIN_KEY, "Content-Type": "application/json"},
        json={"out": True, "bolt11": bolt11},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("payment_hash") or data.get("checking_id") or "paid"


_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _lnurl_decode(lnurl: str) -> str:
    """Bech32 LNURL -> its https URL. Pure python, no new dependency. The
    checksum is not re-verified: the string arrives over TLS straight from
    Wavlake's API, and a corrupt one fails loudly at the HTTP step anyway."""
    s = lnurl.strip().lower()
    if s.startswith("lightning:"):
        s = s[len("lightning:"):]
    pos = s.rfind("1")
    if not s.startswith("lnurl") or pos < 1:
        raise ValueError("not an lnurl string")
    data = [_B32.find(c) for c in s[pos + 1:]]
    if -1 in data:
        raise ValueError("bad bech32 character in lnurl")
    data = data[:-6]  # drop the checksum
    acc, bits, out = 0, 0, bytearray()
    for v in data:
        acc = (acc << 5) | v
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return out.decode("utf-8")


def _pay_lnurl(lnurl: str, sats: int, wallet_key: str) -> str:
    """Pay a bech32 LNURL-pay. Same dance as _lnurlp_pay_address, but the
    starting point is an LNURL instead of a Lightning Address, and the paying
    wallet is the caller's choice."""
    url = _lnurl_decode(lnurl)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    meta = resp.json()
    callback = meta.get("callback")
    if not callback:
        raise RuntimeError("lnurl gave no callback")
    msat = sats * 1000
    lo = int(meta.get("minSendable", 1))
    hi = int(meta.get("maxSendable", 10 ** 12))
    if not lo <= msat <= hi:
        raise RuntimeError(f"{sats} sats is outside the lnurl bounds {lo}-{hi} msat")
    sep = "&" if "?" in callback else "?"
    resp = requests.get(f"{callback}{sep}amount={msat}", timeout=15)
    resp.raise_for_status()
    bolt11 = resp.json().get("pr")
    if not bolt11:
        raise RuntimeError("lnurl callback gave no invoice")
    resp = requests.post(
        f"{LNBITS_BASE_URL}/api/v1/payments",
        headers={"X-Api-Key": wallet_key, "Content-Type": "application/json"},
        json={"out": True, "bolt11": bolt11},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("payment_hash") or data.get("checking_id") or "paid"


def _log_wavlake_payout(row: dict) -> None:
    """One ledger line per forward attempt -- this is part of the royalty
    record. Tab-separated, LF-only (CRLF killed a feed once), never raises."""
    try:
        line = "\t".join(
            str(row.get(k, "")).replace("\t", " ").replace("\n", " ")
            for k in ("ts", "status", "sats", "artist", "title", "guid",
                      "play_hash", "detail"))
        with open(WAVLAKE_PAYOUT_LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception:
        logging.exception("wavlake payout ledger write failed")
    try:
        rds.rpush("wavlake:payouts", json.dumps(row))
    except Exception:
        pass


def _forward_wavlake_artist(pending: dict, play_hash: str) -> None:
    """Send the artist share on, right after the paid play is queued. Runs in
    a background thread so the listener's poll answer is not held up.

    One forward per play invoice: an atomic redis claim (the 2026-07-23
    double-settlement lesson). A FAILED forward is NOT retried by code -- a
    timeout can mean 'paid but no answer came back', and the one unforgivable
    failure here is paying twice. The ledger row plus the wavlake:payouts:owed
    list tell SF exactly what to check and forward by hand."""
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
           "sats": WAVLAKE_FORWARD_SATS,
           "artist": pending.get("artist", ""), "title": pending.get("title", ""),
           "guid": pending.get("guid", ""), "play_hash": play_hash}
    if not (WAVLAKE_ADMIN_KEY and wavlake.app_id):
        row.update(status="SKIPPED",
                   detail="no WAVLAKE_ADMIN_KEY or no appId -- forward by hand")
        _log_wavlake_payout(row)
        return
    if not rds.set("wavlake:payout:" + play_hash, "1", nx=True, ex=86400):
        return  # this play's forward was already claimed
    try:
        lnurl = wavlake._lnurl_sync(pending.get("guid", ""))
        if not lnurl:
            raise RuntimeError("Wavlake /lnurl returned nothing for this track")
        pay_hash = _pay_lnurl(lnurl, WAVLAKE_FORWARD_SATS, WAVLAKE_ADMIN_KEY)
        row.update(status="SENT", detail=pay_hash)
    except Exception as e:
        row.update(status="FAILED", detail=str(e)[:200])
        try:
            rds.rpush("wavlake:payouts:owed", json.dumps(row))
        except Exception:
            pass
    _log_wavlake_payout(row)
    if row["status"] == "SENT":
        # let the Broadcast bar say the sats went on to the artist
        try:
            raw = rds.get("wavlake:notice")
            if raw:
                n = json.loads(raw)
                if n.get("artist") == row["artist"] and n.get("title") == row["title"]:
                    n["forwarded"] = True
                    ttl = rds.ttl("wavlake:notice")
                    rds.setex("wavlake:notice", ttl if ttl and ttl > 0 else 600,
                              json.dumps(n))
        except Exception:
            pass


def _pending_play_key(payment_hash: str) -> str:
    return f"pending_play:{payment_hash}"


# --- nr_xlane (2026-09-12): the X lane's one-time pay codes -----------------------------------------
# The X bot (on this box) mints a code for a listener's search at the X price. The listener opens
# noderunnersradio.com/?jukebox&p=<code>, picks a song, and the invoice is minted HERE at the code's price
# with the name "an X listener" and the message the listener typed on X. One code, one invoice, one song.
X_WHO = "an X listener"
XCODE_TTL_S = 900                                   # a code lives as long as an invoice: 15 minutes
XCODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/1/i/l/o: a code is read off a phone screen
XCODE_LEN = 8
XCODE_Q_MAX = 120                                   # the same bound /api/request puts on a query
XCODE_SATS_MAX = 100000
SITE_URL = "https://noderunnersradio.com"


def _xcode_key(code: str) -> str:
    return f"xcode:{code}"


def _xcode_valid(code) -> bool:
    return isinstance(code, str) and len(code) == XCODE_LEN and all(c in XCODE_ALPHABET for c in code)


XCODE_ERR = "ERR"                                   # redis did not answer: not "expired", "try again"


def _xcode_load(code):
    """The code's record; None when the code is malformed, unknown, expired or under the station's own floor;
    XCODE_ERR when redis did not answer (the caller says "try again", never "expired")."""
    if not _xcode_valid(code):
        return None
    try:
        raw = rds.get(_xcode_key(code))
    except Exception:
        return XCODE_ERR
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except Exception:
        return None
    if int(rec.get("sats") or 0) < PLAY_PRICE_SATS:   # the API is the last guard on the price: never under the site's 21
        return None
    return rec


def _xcode_claim(code: str) -> bool:
    """One mint at a time per code (two taps within one LNbits round-trip must not make two invoices)."""
    try:
        return bool(rds.set("xcode:claim:" + code, "1", nx=True, ex=15))
    except Exception:
        return False


def _xcode_release(code: str) -> None:
    try:
        rds.delete("xcode:claim:" + code)
    except Exception:
        pass


def _xcode_new() -> str:
    """A fresh code that was never issued before. The issued-set is small and permanent: wallets and
    browsers remember links, and a recycled code must never open a stranger's song."""
    while True:
        code = "".join(_secrets.choice(XCODE_ALPHABET) for _ in range(XCODE_LEN))
        if rds.sadd("xcode:issued", code) == 1:
            return code


def _clean_query(raw) -> str:
    """A search as typed on X: one line, printable, whitespace collapsed, capped."""
    s = _ud.normalize("NFKC", str(raw or ""))
    s = "".join(c if c.isprintable() else " " for c in s)
    return " ".join(s.split())[:XCODE_Q_MAX].strip()


def _xlane_stamp(pending: dict, xrec: dict, code: str) -> None:
    """The X lane's fixed facts on a pending play: the name, the message from X, the door. No note_token, so
    the page draws no inputs and the status poll can never rename or rewrite this request."""
    # nr_xlane2: only the FIRST invoice on a link carries the requester's message; a further tap by someone
    # else is their own request, without a stranger's shoutout (SF 2026-09-12)
    pending["note"] = "" if xrec.get("_extra") else (_clean_note(xrec.get("note") or "") if _notes_on() else "")
    pending["who"] = X_WHO if _notes_on() else ""
    pending["via"] = "x"
    pending["full"] = False
    pending["note_token"] = ""
    pending["x_code"] = code
    pending["x_share"] = int(xrec.get("sats") or 0)


def _xlane_reply(reply: dict) -> dict:
    out = dict(reply)
    out.pop("note_token", None)
    out["notes_enabled"] = False
    out["note_max"] = 0
    return out


def _xcode_extra(code: str, xrec: dict, pending: dict, invoice: dict, reply: dict) -> None:
    """A further invoice on an already-bound link (another person, or another pick): remembered, never rebinding."""
    xrec.pop("_extra", None)
    xrec.setdefault("extra", []).append({"payment_hash": invoice["payment_hash"], "artist": pending.get("artist", ""),
                                         "title": pending.get("title", ""), "sats_total": int(reply.get("sats") or 0), "at": time.time()})
    try:
        ttl = rds.ttl(_xcode_key(code))
    except Exception:
        ttl = 0
    rds.setex(_xcode_key(code), max(int(ttl or 0), PENDING_PLAY_TTL_S), json.dumps(xrec))


def _xcode_bind(code: str, xrec: dict, pending: dict, invoice: dict, reply: dict) -> None:
    """The code now belongs to this song and this invoice, for the rest of its life."""
    xrec.update({"payment_hash": invoice["payment_hash"], "artist": pending.get("artist", ""),
                 "title": pending.get("title", ""), "source": pending.get("source", ""),
                 "guid": pending.get("guid", ""), "sats_total": int(reply.get("sats") or 0),
                 "minted_at": time.time(), "reply": reply})
    try:
        ttl = rds.ttl(_xcode_key(code))
    except Exception:
        ttl = 0
    # the code lives at least as long as the invoice it now holds (a tap near the end of the 15 minutes
    # must not leave a payable invoice behind a link that says "expired")
    rds.setex(_xcode_key(code), max(int(ttl or 0), PENDING_PLAY_TTL_S), json.dumps(xrec))


# --- nr sweeper (paid-but-never-played) ---
# The station takes the sats and queues the track in the same breath, and until
# now the ONLY thing that made that happen was a listener's browser polling
# /api/play/status. Close the tab at the wrong moment and the money was taken
# and the song never aired -- silently, with no retry.
#
# _try_queue is that step, lifted out of the request handler so a background
# sweeper can call it too. The lock matters: the old inline version read
# pending["queued"], did the work, then wrote queued=True with nothing held in
# between, so two concurrent callers could queue the SAME track twice. With a
# sweeper running that race stops being unlikely.
_queue_lock = threading.Lock()

# nr_rid_check (2026-08-16): "queued" used to mean only "the socket accepted the
# push". liquidsoap can still drop the request when it cannot open the file (the
# 700-mode permission wall did exactly that on 2026-08-14: paid, stamped queued,
# never aired, nobody told). Two changes:
#   1. the push waits for liquidsoap's own resolve budget (20 s) instead of the
#      5 s socket default -- otherwise a slow disk makes the answer "unknown";
#   2. after the push we ask liquidsoap for the request's status and only a
#      DEFINITIVE drop is reported as NOT queued (-> sweeper retries, bot alarm).
#      Unknown/slow is treated as queued so nothing is ever pushed twice.
_PUSH_TIMEOUT_S = 22.0
_DROP_RETRY_CAP = 3          # definitive drops before we stop pushing
_DROP_RETRY_GAP_S = 30       # seconds between retries after a definitive drop


def _gain_note(path):
    """Volume note for the annotate prefix: e.g. '-4.0 dB' when gains.db knows
    the track and it is louder than the -14 LUFS target; '' otherwise (unity).
    Attenuate-only and clamped at -18 HERE, so the player can never boost or
    slam. Any error -> '': a paid push must never fail over loudness."""
    try:
        import sqlite3
        db = sqlite3.connect("file:/var/lib/jukebox/gains.db?mode=ro", uri=True)
        try:
            row = db.execute("SELECT lufs FROM gains WHERE path=? AND rc=0",
                             (path,)).fetchone()
        finally:
            db.close()
        if not row or row[0] is None:
            return ""
        g = -14.0 - float(row[0])
        if g >= -0.05:
            return ""
        return "%.1f dB" % max(-18.0, g)
    except Exception:
        return ""


# --- nr_notes v2 (2026-08-17): a listener may attach a short NOTE to a paid request -----
# SF's rulings: typed on the site; very short (80 chars, "fits a fortune cookie"); NEVER in the play-log; post-hoc
# moderation. Review crew ruling: the note NEVER enters liquidsoap (annotate parsing runs the
# language preprocessor: "#{...}" would drop the paid request; file tags could masquerade;
# rids restart at 0 on a liquidsoap restart). It lives in redis only:
#   pending_note:<payment_hash>          the payer's note while the invoice is open
#                                        (write needs the per-invoice note_token from mint)
#   jukebox:paid_rid:<rid>  json{uri,note,lane,ts,req_id}  set under _queue_lock at the
#                                        confirmed queue (and for gifts); 6 h; the stored uri
#                                        must match the request's filename before we trust it
# Kill switch:  redis-cli -n 2 set jukebox:notes off      Hide one:  sadd jukebox:note_hidden <req_id>
import unicodedata as _ud
import secrets as _secrets
NOTE_MAX = 80
# nr_at (2026-09-09, SF): how many @handles one ordinary line may carry. Eighty characters hold a dozen
# tags, and a dozen tags for 21 sats is a spam engine. The captain's own line is not counted.
MENTION_MAX = 2
# nr_full (2026-09-09, SF): "Keep the character limit for everyone else but me." The captain's line has
# room for a link and a sentence. Granted by this station on proof of caller, never claimed by a caller.
NOTE_MAX_FULL = 160
_NOTE_LINKISH = re.compile(r"(?:://|www\.|t\.me/|[A-Za-z0-9]\.[A-Za-z]{2,}(?:/|$)|\d{1,3}(?:\.\d{1,3}){3})", re.I)


def _cut_safely(s: str, cap: int) -> str:
    """Cut to cap, but NEVER leave half a handle behind. "@verylongname" trimmed to "@verylong" can be a
    different, uninvolved person, and on Telegram that half-name is a live link to them. So if we really
    did cut, and the last word carries a handle, the whole word goes. The @ need not start the word --
    "(@verylongname" is the same hazard -- so the test looks INSIDE the word (SF 2026-09-09)."""
    if len(s) <= cap:
        return s.strip()
    words = s[:cap].split(" ")
    if words and re.search(r"@[A-Za-z0-9_]", words[-1]):
        words.pop()
    return " ".join(words).strip()


def _clean_note(raw, full: bool = False) -> str:
    """Plain words only. Cut FIRST (regex safety), NFC, drop format/control chars (keep ZWJ
    for emoji), cap combining marks, drop link-ish tokens and long digit runs, keep at most
    MENTION_MAX @handles, no quotes/backslashes, whitespace collapsed, hard cap NOTE_MAX.

    full=True is the captain's own line (SF: "It should drop links by spammers and other
    people, but not by me"): its links stay, its handles are not counted, and its cap is
    NOTE_MAX_FULL. It is granted by this station on proof of caller -- see _from_the_box --
    and never on a caller's say-so. It does NOT make a tag cross networks: that binding is
    enforced per surface and holds for everybody, the captain included (SF, 2026-09-09)."""
    cap = NOTE_MAX_FULL if full else NOTE_MAX
    s = str(raw or "")[:cap * 4]
    s = _ud.normalize("NFC", s)
    out, marks = [], 0
    for ch in s:
        cat = _ud.category(ch)
        if ch < " " or ch == "\x7f" or (cat == "Cf" and ch != "\u200d"):
            continue
        if cat in ("Mn", "Me"):
            marks += 1
            if marks > 2:
                continue
        else:
            marks = 0
        out.append(ch)
    s = "".join(out).replace('"', "'").replace("\\", " ")
    # nr_at: a handle may stay, so the station can thank someone by name. A stray "@" with nothing
    # behind it still goes.
    s = re.sub(r"@(?![A-Za-z0-9_])", "", s)
    if not full and s.count("@") > MENTION_MAX:
        parts = s.split("@")
        s = "@".join(parts[:MENTION_MAX + 1]) + "".join(parts[MENTION_MAX + 1:])
    s = re.sub(r"\d{7,}", "", s)
    if full:
        s = " ".join(s.split())          # nr_full: the captain's links stay whole
    else:
        s = " ".join(tok for tok in s.split() if not _NOTE_LINKISH.search(tok))
    return _cut_safely(s, cap)


def _notes_on() -> bool:
    try:
        v = rds.get("jukebox:notes")
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        return not (v is not None and str(v).strip().lower() in ("off", "0", "no", "false"))
    except Exception:
        return True


def _pending_note_key(payment_hash: str) -> str:
    return "pending_note:%s" % payment_hash


def _read_pending_note(payment_hash: str, full: bool = False) -> str:
    try:
        v = rds.get(_pending_note_key(payment_hash))
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        return _clean_note(v or "", full)
    except Exception:
        return ""


def _pending_name_key(payment_hash: str) -> str:
    return "pending_name:%s" % payment_hash


def _clean_who(raw) -> str:
    """The optional name / handle: same sanitiser as the note, 24 chars. Never "full": a name is a name,
    and a link inside one is somebody being clever. The 24 goes through the SAME guard as the note --
    this is the field most likely to hold a handle, so it is the last place to cut one in half."""
    return _cut_safely(_clean_note(raw), 24)


VIA_KNOWN = ("tg", "nostr", "web", "x")   # nr_xlane: the X lane is its own door


def _clean_via(raw) -> str:
    """Which door a request came in by. An allow-list, never a deny-list: a caller can say anything, and
    anything we do not know is simply the web. Surfaces use this to decide whether an @ in the line is a
    real mention on THEIR network or just words (SF 2026-09-09: "the @ should be on telegram only, not on
    the other channels -- and visa versa"). It grants nothing on its own."""
    v = str(raw or "").strip().lower()[:8]
    return v if v in VIA_KNOWN else "web"


def _read_pending_name(payment_hash: str) -> str:
    try:
        v = rds.get(_pending_name_key(payment_hash))
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        return _clean_who(v or "")
    except Exception:
        return ""


def _mark_paid_rid(rid_raw, uri: str, note: str = "", lane: str = "paid", who: str = "",
                   via: str = "", full: bool = False):
    """Remember which liquidsoap request ids WE pushed, with the file we pushed (self-check
    against rid reuse after a liquidsoap restart), the note and the lane. 6 h."""
    try:
        rid = (rid_raw or "").strip().splitlines()[0].strip() if (rid_raw or "").strip() else ""
        if not rid.isdigit():
            return
        req_id = "%s-%s" % (rid, hashlib.sha1((uri or "").encode("utf-8", "replace")).hexdigest()[:8])
        rds.setex("jukebox:paid_rid:%s" % rid, 6 * 3600,
                  json.dumps({"uri": uri or "", "note": _clean_note(note, full), "lane": lane,
                              "who": _clean_who(who), "via": _clean_via(via), "full": bool(full),
                              "ts": int(time.time()), "req_id": req_id}))
    except Exception:
        pass


def _paid_mark(rid, filename: str = "", initial_uri: str = ""):
    """The marker for this rid, only if it is really OUR push (uri matches the file)."""
    try:
        v = rds.get("jukebox:paid_rid:%s" % rid)
        if not v:
            return None
        m = json.loads(v)
        uri = m.get("uri") or ""
        if not uri:
            return None
        if filename == uri or (initial_uri and initial_uri.endswith(uri)):
            return m
        return None
    except Exception:
        return None


def _note_hidden(req_id) -> bool:
    try:
        return bool(req_id) and bool(rds.sismember("jukebox:note_hidden", str(req_id)))
    except Exception:
        return False


def _push_slow(local_path, rights_class="", source="", artist="", title=""):
    """push_track with a socket timeout that outlives liquidsoap's resolve (20 s).
    "#{" is neutralised in every value: liquidsoap parses annotate values with its
    string-interpolation preprocessor and would drop the whole request."""
    meta = {"rights_class": rights_class, "source": source, "artist": artist, "title": title,
            "replay_gain": _gain_note(local_path)}
    meta = {k: str(v or "").replace("#{", "# {") for k, v in meta.items()}
    uri = "%s%s" % (_lc._annotate(meta), local_path)
    return _lc._send(_lc.DEFAULT_SOCKET, "jukebox.push " + uri, timeout=_PUSH_TIMEOUT_S)


def _rid_resolved(rid_raw, wait_s=8.0):
    """True = ready/playing (queued). False = DEFINITIVELY dropped. None = unknown."""
    txt = (rid_raw or "").strip()
    if not txt:
        return None                                   # push reply timed out: unknown
    rid = txt.splitlines()[0].strip()
    if not rid.isdigit():
        return False                                  # liquidsoap answered, but not with a rid
    deadline = time.time() + wait_s
    while True:
        try:
            raw = _lc._send(_lc.DEFAULT_SOCKET, "request.metadata " + rid)
        except Exception:
            return None
        lines = [l.strip() for l in raw.splitlines()]
        if "END" not in lines:
            return None                               # incomplete reply: unknown
        body = [l for l in lines[:lines.index("END")] if l]
        if body and body[0] == "No such request.":
            return False                              # exact match on the answer body only
        status = _lc._parse_metadata(raw).get("status", "")
        if status in ("ready", "playing"):
            return True
        if status == "destroyed":
            return False
        if time.time() > deadline:
            return None
        time.sleep(0.5)


def _try_queue(payment_hash, pending):
    """Queue a paid track. Returns (ok, message, wait_seconds).

    wait_seconds > 0 means the global anti-spam cooldown is still running and
    the caller should come back -- it is NOT a failure."""
    global _last_play_at
    with _queue_lock:
        # re-read under the lock: another caller may have queued it already
        raw = rds.get(_pending_play_key(payment_hash))
        if raw is None:
            return False, "expired", 0
        fresh = json.loads(raw)
        if fresh.get("queued"):
            return True, fresh.get("message", "Already queued"), 0

        now = time.time()
        # nr_rid_check: after a definitive drop, space the retries and cap them.
        attempts = int(fresh.get("drop_attempts") or 0)
        if attempts >= _DROP_RETRY_CAP:
            return False, "liquidsoap dropped this request %d times -- NOT queued, not retrying" % attempts, 0
        if attempts:
            since = now - float(fresh.get("last_drop_at") or 0)
            if since < _DROP_RETRY_GAP_S:
                return False, "", max(1, int(_DROP_RETRY_GAP_S - since) + 1)
        if now - _last_play_at < PLAY_COOLDOWN_S:
            return False, "", max(1, int(PLAY_COOLDOWN_S - (now - _last_play_at)) + 1)

        spooled = ""
        try:
            if fresh.get("wavlake"):
                spooled = _spool_wavlake(fresh)
                rid_raw = _push_slow(spooled, rights_class="v4v", source="wavlake",
                                     artist=fresh.get("artist", ""),
                                     title=fresh.get("title", ""))
            else:
                # ghost guard at claim-time too: the file was there at mint,
                # but a move can happen in the 15-minute payment window. A
                # loud failure here reaches the bot's PAID-BUT-NOT-QUEUED
                # alarm; a silent liquidsoap drop reaches nobody (2026-08-14).
                if not os.path.exists(fresh.get("local_path") or ""):
                    return False, "file missing from the library disks", 0
                # nr_names (2026-09-08): hand the player the CATALOG's names, as the Wavlake push does; a file
                # with an artist tag but no title tag otherwise airs, logs and posts as "Unknown track".
                _a = str(fresh.get("artist") or "")
                _t = str(fresh.get("title") or "")
                rid_raw = _push_slow(fresh["local_path"],
                                     rights_class=fresh.get("rights_class", ""),
                                     source=fresh.get("source", ""),
                                     artist="" if _a.casefold() in ("unknown artist", "unknown") else _a,
                                     title="" if _t.casefold() in ("unknown track", "unknown") else _t)
        except Exception as e:
            return False, str(e), 0

        # nr_rid_check: a push the socket accepted is not yet a track that will air.
        if _rid_resolved(rid_raw) is False:
            fresh["drop_attempts"] = attempts + 1
            fresh["last_drop_at"] = now
            rds.setex(_pending_play_key(payment_hash), _record_ttl(payment_hash),
                      json.dumps(fresh))
            logging.error("nr_rid_check: paid track NOT queued (attempt %d): %s - %s rid_raw=%r",
                          attempts + 1, fresh.get("artist", ""), fresh.get("title", ""), (rid_raw or "")[:40])
            return False, "liquidsoap could not open the file (request dropped, attempt %d) -- NOT queued" % (attempts + 1), 0
        # only a confirmed queue spends the anti-spam slot and triggers the V4V side effects
        _full = bool(fresh.get("full"))   # nr_full: settled at the mint, on proof of caller, not now
        _mark_paid_rid(rid_raw, spooled if fresh.get("wavlake") else fresh.get("local_path", ""),
                       note=(_read_pending_note(payment_hash, _full) or fresh.get("note") or "") if _notes_on() else "",
                       lane="v4v" if fresh.get("wavlake") else "paid",   # nr_announcer (2026-09-08): Wavlake plays are told apart; the site and the bot only test "gift"
                       who=(_read_pending_name(payment_hash) or fresh.get("who") or "") if _notes_on() else "",
                       via=fresh.get("via") or "", full=_full)
        _last_play_at = now
        if fresh.get("wavlake"):
            _set_wavlake_notice(fresh)
            threading.Thread(target=_forward_wavlake_artist,
                             args=(dict(fresh), payment_hash),
                             daemon=True).start()

        message = f"Queued '{fresh['artist']} - {fresh['title']}' -- should play shortly"
        fresh["queued"] = True
        fresh["message"] = message
        rds.setex(_pending_play_key(payment_hash), _record_ttl(payment_hash),
                  json.dumps(fresh))
        return True, message, 0


def _sweep_paid_plays():
    """Queue paid tracks nobody is watching any more.

    This is the safety net: whatever happens to the listener's browser or to the
    Telegram bot, a paid track still airs. Runs until the record's own 900s TTL
    expires it."""
    import random as _random

    def _mark_unqueueable(ph, message):
        """Under the queue lock, re-read the record and flag it -- never over a concurrent 'queued'."""
        with _queue_lock:
            k = _pending_play_key(ph)
            raw = rds.get(k)
            if raw is None:
                return
            rec = json.loads(raw)
            if rec.get("queued") or rec.get("unqueueable"):
                return
            rec["unqueueable"] = message
            rds.setex(k, _record_ttl(ph), json.dumps(rec))

    _err_count: dict = {}       # payment_hash -> transient failures in a row (capped, then terminal)
    _ERR_CAP = 10               # ~5 min at the 30-s ladder: a dead media link must not loop for a day
    while True:
        seen_this_pass = set()
        try:
            for key in rds.scan_iter(match=_pending_play_key("*"), count=100):
                try:
                    raw = rds.get(key)
                    if raw is None:
                        continue
                    pending = json.loads(raw)
                    if pending.get("queued") or pending.get("unqueueable"):
                        continue
                    ph = key.split(":", 1)[1] if isinstance(key, str) \
                        else key.decode().split(":", 1)[1]
                    seen_this_pass.add(ph)
                    now_ts = time.time()
                    gate = _sweep_next.get(ph)
                    if gate is not None and gate > now_ts:
                        continue                      # a retry gap or the 5-min late gate is running
                    # 2026-09-03: a record now outlives its invoice by a day (late payments from the
                    # captain's own wallet). Young records are checked every pass as before; once the
                    # invoice's 15 minutes are over, LNbits is asked about the record only every
                    # _SWEEP_LATE_GAP_S, staggered so a restart does not fire them all at once.
                    # Records minted before this code (no minted_at) count as young.
                    minted = float(pending.get("minted_at") or 0)
                    late = bool(minted) and (now_ts - minted) > PENDING_PLAY_TTL_S
                    if late:
                        if gate is None:
                            _sweep_next[ph] = now_ts + _random.uniform(0, _SWEEP_LATE_GAP_S)
                            continue
                        _sweep_next[ph] = now_ts + _SWEEP_LATE_GAP_S
                    if not _is_play_invoice_paid(
                            ph, key=WAVLAKE_INVOICE_KEY if pending.get("wavlake") else ""):
                        continue
                    ok, message, wait = _try_queue(ph, pending)
                    if ok:
                        _sweep_next.pop(ph, None)
                        logging.info("sweeper queued an unwatched paid play: %s",
                                     message)
                    elif wait:
                        _sweep_next[ph] = now_ts + wait + 1      # paid: retry the moment the wait ends
                    elif not _is_terminal(message) and _err_count.get(ph, 0) + 1 < _ERR_CAP:
                        # transient (a first drop, a refused socket, a CDN hiccup): the ladder as before
                        _err_count[ph] = _err_count.get(ph, 0) + 1
                        _sweep_next[ph] = now_ts + _DROP_RETRY_GAP_S
                        logging.warning("sweeper: paid track not queued yet (%s, try %d): %s -- retrying",
                                        ph[:16], _err_count[ph], message)
                    else:
                        if not _is_terminal(message):
                            message = "push failed %d times (%s) -- NOT queued, not retrying" % (_ERR_CAP, message)
                        # paid, and it will never queue by itself. Say it ONCE: mark the record
                        # (the sweeper skips it from now on) and push one fact-list entry that the
                        # bot relays to the admin chat. Nothing is retried, credited or refunded here.
                        logging.error("SWEEPER: paid but cannot queue (%s): %s",
                                      ph[:16], message)
                        _sweep_next.pop(ph, None)
                        try:
                            _mark_unqueueable(ph, message)
                        except Exception as e:
                            logging.warning("sweeper: could not mark %s: %s", ph[:16], e)
                        if ph in _unqueued_seen:
                            continue
                        _unqueued_seen.add(ph)
                        try:
                            rds.rpush("play:unqueued", json.dumps(
                                {"payment_hash": ph, "error": message,
                                 "artist": pending.get("artist", ""),
                                 "title": pending.get("title", ""),
                                 "ts": int(time.time())}))
                        except Exception:
                            pass
                except Exception as e:
                    logging.warning("sweeper item failed: %s", e)
            for k in [k for k in _sweep_next if k not in seen_this_pass]:
                _sweep_next.pop(k, None)
            for k in [k for k in _err_count if k not in seen_this_pass]:
                _err_count.pop(k, None)
        except Exception as e:
            logging.warning("sweeper pass failed: %s", e)
        time.sleep(5)


# --- captain's playlists -----------------------------------------------------
# SF's 36 Spotify exports, resolved to local files by nr-resolve-playlists.py.
# Loading one writes it straight into the rotation file liquidsoap watches, and
# drops an override so the shuffler leaves it alone until told otherwise.
_PLAYLIST_DIR = os.environ.get("NR_PLAYLIST_DIR", "/var/lib/jukebox/playlists")
_ROTATION = os.environ.get("NR_ROTATION", "/var/lib/jukebox/rotation.m3u")
_OVERRIDE = os.environ.get("NR_ROTATION_OVERRIDE", "/var/lib/jukebox/rotation.override")


def _playlist_list():
    """Every resolved playlist, with its track count. Newest name first."""
    out = []
    try:
        for f in sorted(os.listdir(_PLAYLIST_DIR)):
            if not f.endswith(".m3u"):
                continue
            p = os.path.join(_PLAYLIST_DIR, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    n = sum(1 for line in fh if line.strip())
            except OSError:
                n = 0
            out.append({"name": f[:-4], "file": f, "tracks": n})
    except OSError:
        pass
    return out


def _playlist_current():
    """Which playlist is on air, or None if the deck is running."""
    try:
        with open(_OVERRIDE, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _playlist_load(name):
    """Put a playlist on air. Returns (ok, message).

    Never deletes anything: the rotation file is replaced atomically, and the
    deck's own cursor is left untouched so the shuffle resumes where it was."""
    safe = os.path.basename(name).replace("..", "")
    if not safe.endswith(".m3u"):
        safe += ".m3u"
    src = os.path.join(_PLAYLIST_DIR, safe)
    if not os.path.isfile(src):
        return False, "no such playlist"
    try:
        with open(src, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
    except OSError as e:
        return False, str(e)
    if not lines:
        return False, "that playlist resolved to nothing playable"
    tmp = _ROTATION + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, _ROTATION)          # atomic; liquidsoap never sees a half file
    with open(_OVERRIDE, "w", encoding="utf-8") as fh:
        fh.write(safe[:-4])
    os.chmod(_OVERRIDE, 0o644)
    return True, f"{safe[:-4]} is on air -- {len(lines)} tracks"


def _playlist_release():
    """Hand the deck back to the shuffler."""
    try:
        os.remove(_OVERRIDE)
    except OSError:
        pass
    return True, "back to the shuffle -- the deck resumes where it left off"



# ---- nr_mood (2026-09-05): the captain's mood queue -----------------------------------------------
# A chosen playlist plays ABOVE the background rotation and BELOW paid requests (liquidsoap:
# fallback(track_sensitive=true, [requests, mood, radio])). The list lives in redis; a feeder thread
# hands liquidsoap ONE track at a time, ~25 s before the current mood track ends, so "Off" stops
# almost at once and a paid request never waits behind a stack of mood tracks. The rotation window
# and the deck cursor are never touched. Songs aired (by anyone but a paid request) in the last 24 h
# are skipped -- SF's ruling 2026-09-04. The feeder pauses during a live session.
# redis (db 2): mood:on "1"/"0" | mood:name | mood:list (json items, playlist order) | mood:skipped
#   (last 20 json) | mood:played (handed-over count) | mood:pushed (json) | mood:onair:<rid> (epoch)
_MOOD_TICK_S = 3
_MOOD_LOOKAHEAD_S = 25
_MOOD_RECENT_H = 24
_MOOD_MAX_SKIPS = 50
_MOOD_PAID = ("jukebox", "wavlake", "local", "gift")
_mood_lock = threading.Lock()
_mood_cache = {"recent_t": 0.0, "recent": set(), "lane_t": 0.0, "lane": None, "dur": {}}


def _mood_send(cmd, timeout=5.0):
    return _lc._send(_lc.DEFAULT_SOCKET, cmd, timeout=timeout)


def _mood_ask(cmd, timeout=5.0):
    """Like _mood_send, but a reply without its END line (timeout, partial) raises: unknown is not empty."""
    raw = _mood_send(cmd, timeout=timeout) or ""
    lines = [l.strip() for l in raw.splitlines()]
    if "END" not in lines:
        raise RuntimeError("incomplete reply to %s" % cmd)
    body = [l for l in lines if l and l not in ("END", "Bye!")]
    if body and body[0].startswith("ERROR"):
        raise RuntimeError(body[0])
    return body


def _mood_on_air_rids():
    """Every rid liquidsoap reports on air (two during a crossfade), as a list."""
    body = _mood_ask("request.on_air")
    return " ".join(body).split()


def _mood_lane_ok():
    """True when the RUNNING liquidsoap has the mood queue (a config without it answers ERROR)."""
    now = time.time()
    if _mood_cache["lane"] is not None and now - _mood_cache["lane_t"] < 60:
        return _mood_cache["lane"]
    try:
        reply = (_mood_send("mood.queue", timeout=3.0) or "").strip()
        ok = bool(reply) and not reply.upper().startswith("ERROR")
    except Exception:
        ok = False
    _mood_cache["lane_t"] = now
    _mood_cache["lane"] = ok
    return ok


def _mood_queue_rids():
    """rids waiting in the mood source (prefetched + pending; never the one on air). Raises when unknown."""
    return " ".join(_mood_ask("mood.queue")).split()


def _mood_on_air():
    """(rid, metadata) of the mood track on air, else (None, None). Checks every on-air rid
    (two during a crossfade) and also recognises our push by rid+path when a file tag beat the annotate."""
    try:
        rids = _mood_on_air_rids()
    except Exception:
        return None, None
    try:
        p = json.loads(rds.get("mood:pushed") or b"{}")
    except Exception:
        p = {}
    for rid in reversed(rids):                   # the newest track last: prefer it
        try:
            m = request_metadata(rid)
        except Exception:
            continue
        if m.get("source") == "mood" or (p.get("rid") == rid and p.get("path") and p.get("path") == m.get("filename")):
            return rid, m
    return None, None


def _mood_key(artist, title):
    return ((artist or "").casefold().strip(), (title or "").casefold().strip())


def _mood_recent_keys():
    """(artist, title) of every non-paid airing in the last 24 h, from the play-log tail; cached 60 s."""
    import datetime as _dt
    now = time.time()
    if now - _mood_cache["recent_t"] < 60:
        return _mood_cache["recent"]
    keys = set()
    try:
        cutoff = now - _MOOD_RECENT_H * 3600
        with open(PLAYLOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            chunk = f.read().replace(b"\x00", b"").decode("utf-8", "replace")
        for line in chunk.split("\n")[1:]:
            parts = line.rstrip("\r").split("\t")
            if len(parts) != 6 or parts[1] != "play" or parts[2] in _MOOD_PAID:
                continue
            try:
                ts = _dt.datetime.fromisoformat(parts[0]).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                keys.add(_mood_key(parts[4], parts[5]))
    except Exception:
        pass
    _mood_cache["recent_t"] = now
    _mood_cache["recent"] = keys
    return keys


def _mood_duration(path):
    d = _mood_cache["dur"].get(path)
    if d is not None:
        return d
    d = 0
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT duration_s FROM tracks WHERE path=?", (path,)).fetchone()
        finally:
            con.close()
        d = int(row[0]) if row and row[0] else 0
    except Exception:
        d = 0
    if len(_mood_cache["dur"]) > 2000:
        _mood_cache["dur"].clear()
    _mood_cache["dur"][path] = d
    return d


def _mood_items_for(paths):
    """Library rows for the playlist paths, in playlist order. Unknown paths keep the file name."""
    meta = {}
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            for i in range(0, len(paths), 400):
                chunk = paths[i:i + 400]
                q = "SELECT path, artist, title, duration_s, rights_class FROM tracks WHERE path IN (%s)" % ",".join("?" * len(chunk))
                for p, a, t, d, r in con.execute(q, chunk):
                    meta[p] = (a or "", t or "", int(d or 0), r or "collecting_society")
        finally:
            con.close()
    except Exception:
        pass
    items = []
    for p in paths:
        a, t, d, r = meta.get(p, ("", os.path.splitext(os.path.basename(p))[0], 0, "collecting_society"))
        items.append({"path": p, "artist": a, "title": t, "dur": d, "rights": r})
    return items


def _mood_pop_next():
    """The next playable item off the list, skipping recent airings. None when the list is empty."""
    recent = _mood_recent_keys()
    for _ in range(_MOOD_MAX_SKIPS):
        raw = rds.lpop("mood:list")
        if raw is None:
            return None
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if _mood_key(item.get("artist"), item.get("title")) in recent:
            item["why"] = "aired in the last 24 h"
            rds.lpush("mood:skipped", json.dumps(item))
            rds.ltrim("mood:skipped", 0, 19)
            continue
        return item
    return None


def _mood_park(item, why):
    item["why"] = why
    rds.lpush("mood:skipped", json.dumps(item))
    rds.ltrim("mood:skipped", 0, 19)


def _mood_push(item):
    """Hand ONE track to liquidsoap's mood queue. Returns "ok", "retry" (no answer) or "dead" (the
    request died at once: missing or unreadable file)."""
    meta = {"rights_class": item.get("rights") or "collecting_society", "source": "mood",
            "artist": item.get("artist") or "", "title": item.get("title") or "",
            "replay_gain": _gain_note(item["path"])}
    meta = {k: str(v or "").replace("#{", "# {") for k, v in meta.items()}
    uri = "%s%s" % (_lc._annotate(meta), item["path"])
    try:
        lines = (_mood_send("mood.push " + uri, timeout=_PUSH_TIMEOUT_S) or "").strip().splitlines()
    except Exception:
        return "retry"
    rid = lines[0].strip() if lines else ""
    if not rid.isdigit():
        return "retry"
    try:
        m = request_metadata(rid)                # mood.push resolves synchronously: a dead file is dead now
    except Exception:
        m = {}
    if not m or m.get("status") in ("destroyed", "failed"):
        return "dead"
    rds.set("mood:pushed", json.dumps({"rid": rid, "path": item["path"], "artist": item.get("artist", ""),
                                       "title": item.get("title", ""), "ts": time.time()}))
    rds.incr("mood:played")
    return "ok"


def _mood_hand_over():
    """Pop the next playable item and push it. Caller holds _mood_lock. Returns a reason."""
    item = _mood_pop_next()
    if item is None:
        return "empty"
    r = _mood_push(item)
    if r == "ok":
        return "pushed"
    if r == "dead":
        _mood_park(item, "file could not be played")
        return "dead"
    item["tries"] = int(item.get("tries", 0)) + 1
    if item["tries"] >= 2:
        _mood_park(item, "could not be handed over")
        return "parked"
    rds.lpush("mood:list", json.dumps(item))
    return "pushfail"


def _mood_feed_once():
    """One feeder tick. Returns a short reason (also handy for tests)."""
    on = rds.get("mood:on") == b"1"
    if not on and not rds.exists("mood:pushed"):
        return "off"
    if not _mood_lane_ok():
        return "nolane"
    rid, m = _mood_on_air()
    if rid:                                      # clock the on-air mood track from first sight, even while paused
        key = ("mood:onair:%s:%s" % (rid, m.get("filename") or ""))[:240]
        rds.set(key, str(time.time()), nx=True, ex=7200)
    if not on:
        return "off"
    if _stage_live():
        return "live"
    if rds.llen("mood:list") == 0:
        return "empty"
    with _mood_lock:
        if rds.get("mood:on") != b"1":
            return "off"                         # Off/Pause landed while we were looking
        try:
            waiting = [r for r in _mood_queue_rids() if r != rid]
        except Exception:
            return "unknown"                     # a partial socket answer is not "nothing queued"
        if waiting:
            return "queued"                      # one is already handed to the deck
        if rid:
            since = float(rds.get(key) or time.time())
            dur = _mood_duration(m.get("filename") or "")
            if dur and (dur - (time.time() - since)) > _MOOD_LOOKAHEAD_S:
                return "playing"                 # hand the next one over ~25 s before the end
        return _mood_hand_over()


def _mood_feeder():
    while True:
        time.sleep(_MOOD_TICK_S)
        try:
            _mood_feed_once()
        except Exception as e:
            try:
                print("mood feeder: %s" % e, flush=True)
            except Exception:
                pass


def _mood_state():
    on = rds.get("mood:on") == b"1"
    name = (rds.get("mood:name") or b"").decode("utf-8", "replace")
    nxt, skipped = [], []
    for raw in rds.lrange("mood:list", 0, 14):
        try:
            it = json.loads(raw)
            nxt.append({"artist": it.get("artist", ""), "title": it.get("title", ""), "dur": it.get("dur", 0), "path": it.get("path", "")})
        except Exception:
            pass
    for raw in rds.lrange("mood:skipped", 0, 9):
        try:
            it = json.loads(raw)
            skipped.append({"artist": it.get("artist", ""), "title": it.get("title", "")})
        except Exception:
            pass
    rid, m = _mood_on_air()
    queued = False
    try:
        queued = bool([r for r in _mood_queue_rids() if r != rid])
    except Exception:
        pass
    return {"on": on, "name": name, "left": rds.llen("mood:list"), "played": int(rds.get("mood:played") or 0),
            "next": nxt, "skipped": skipped,
            "now": ({"artist": m.get("artist", ""), "title": m.get("title", "")} if rid else None),
            "queued": queued, "lane_ok": _mood_lane_ok(), "live": _stage_live()}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _xml(self, code, body_str, content_type="application/rss+xml; charset=utf-8", filename=None):
        body = body_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if filename:
            # Without this a browser opens the playlist in its own media viewer,
            # which cannot play a playlist -- SF got a dead video tab (2026-07-29).
            # As an attachment it is handed to whatever the machine uses for .m3u.
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/x/code":           # nr_xlane: public read of one code
            return self._handle_x_code_get(parsed)
        if parsed.path == "/api/search":
            return self._handle_search(parsed)
        if parsed.path in ("/api/rss", "/api/rss.xml"):
            return self._handle_rss(parsed)
        if parsed.path == "/api/overlay/layout":
            # per-venue overlay positions (SF 2026-07-30: drag & drop editor).
            # Public read -- the OBS overlay polls this every 10 s.
            oqs = parse_qs(parsed.query)
            venue = "".join(c for c in (oqs.get("venue") or ["default"])[0].lower()
                            if c.isalnum() or c in "_-")[:24] or "default"
            raw = rds.get("overlay:layout:" + venue)
            try:
                return self._json(200, json.loads(raw) if raw else {})
            except Exception:
                return self._json(200, {})

        if parsed.path == "/api/sample":
            return self._handle_sample(parsed)
        if parsed.path == "/api/stats":
            return self._handle_stats()
        if parsed.path == "/api/admin/music/search":
            return self._handle_admin_music_search(parsed)
        if parsed.path == "/api/admin/rotation/info":
            return self._handle_admin_rotation_info(parsed)
        if parsed.path == "/api/admin/chat/recent":
            return self._handle_admin_chat_recent(parsed)
        if parsed.path == "/api/icecast-status":
            return self._handle_icecast_status()
        if parsed.path in ("/api/listen.m3u", "/api/listen.pls"):
            return self._handle_playlist(parsed)
        if parsed.path in ("/api/admin/mood", "/api/admin/mood/playlists"):
            return self._handle_mood_get(parsed)
        if parsed.path == "/api/admin/playlists":
            if not self._admin_authed((parse_qs(parsed.query).get("token") or [""])[0]):
                return self._json(403, {"error": "invalid or missing token"})
            return self._json(200, {"playlists": _playlist_list(),
                                    "current": _playlist_current()})
        if parsed.path == "/api/plays.rss":
            return self._handle_plays_rss(parsed)
        if parsed.path == "/api/admin/buma/status":
            return self._handle_buma_status(parsed)
        if parsed.path == "/api/admin/buma/report":
            return self._handle_buma_report(parsed)
        if parsed.path == "/api/admin/status":
            return self._handle_admin_status(parsed)
        if parsed.path == "/api/chat/recent":
            return self._handle_chat_recent()
        if parsed.path == "/api/chat/media":
            return self._handle_chat_media(parsed)
        if parsed.path == "/api/play/status":
            return self._handle_play_status(parsed)
        if parsed.path == "/api/donate/status":
            return self._handle_donate_status(parsed)
        if parsed.path == "/api/donate/total":
            return self._handle_donate_total()
        if parsed.path == "/api/nowplaying":
            return self._handle_nowplaying()
        if parsed.path == "/api/history":
            return self._handle_history(parsed)
        if parsed.path == "/api/livesplit":
            return self._handle_livesplit_public()
        if parsed.path == "/api/admin/livesplit":
            return self._handle_livesplit_admin_status(parsed)
        if parsed.path == "/api/session":
            return self._handle_session_check()
        if parsed.path == "/api/admin/stage/paylink":
            return self._handle_stage_paylink(parsed)
        if parsed.path == "/api/donate/recent":
            return self._json(200, {"donations": _donate_recent()})
        if parsed.path == "/api/admin/notes":
            return self._handle_notes(parsed)
        if parsed.path == "/api/admin/unqueued":
            return self._handle_unqueued(parsed)
        return self._json(404, {"error": "not found"})

    def _handle_nowplaying(self):
        """Public, read-only: what's on air + what's queued. No token (it's just
        what listeners already hear). Distinct from /api/admin/status, which also
        exposes control and needs the admin token."""
        try:
            on_air = on_air_rid()
            now_playing = _track_summary(on_air) if on_air else None
            queue = [_track_summary(rid) for rid in queue_rids()]
        except Exception as e:
            return self._json(502, {"error": f"Could not reach liquidsoap: {e}"})
        # Bare captain-pushes lose their socket metadata the moment they start
        # playing ("Unknown track" on the site, 2026-08-14). The play-log hook
        # demonstrably records the true name at track start, so when the socket
        # answer is nameless, trust the log's freshest line instead.
        if (now_playing and not (now_playing.get("artist") or "").strip()
                and now_playing.get("title") in ("", "Unknown track", None)):
            last = _read_plays(1)
            if last:
                now_playing = dict(now_playing)
                now_playing["artist"] = last[0].get("artist") or ""
                now_playing["title"] = last[0].get("title") or "Unknown track"
        # public shape: artist/title/album only (drop rid/status internals)
        def pub(t):
            if t is None:
                return None
            out = {"artist": t.get("artist"), "title": t.get("title"), "album": t.get("album"),
                   "requested": False, "lane": "", "req_id": "", "note": "", "who": "",
                   "note_via": ""}
            mk = _paid_mark(t.get("rid"), t.get("filename") or "", t.get("initial_uri") or "")
            if mk:
                out["requested"] = True
                out["lane"] = mk.get("lane") or "paid"
                out["req_id"] = mk.get("req_id") or ""
                if _notes_on() and not _note_hidden(out["req_id"]):
                    out["note"] = _clean_note(mk.get("note") or "", bool(mk.get("full")))
                    out["who"] = _clean_who(mk.get("who") or "")
                    # nr_at: a surface shows the @ only if this says the line was typed there. An old
                    # record has no "via" and reads as "web", which every surface treats as not-mine.
                    out["note_via"] = _clean_via(mk.get("via") or "")
            return out
        out = {"now_playing": pub(now_playing), "queue": [pub(t) for t in queue]}
        raw_wl = rds.get("wavlake:notice")
        if raw_wl is not None:
            try:
                out["wavlake"] = json.loads(raw_wl)
            except Exception:
                pass
        self._json(200, out)

    def _handle_history(self, parsed):
        """Public, read-only: the last N aired tracks from the play log. Exposes
        only ts/artist/title -- NOT rights_class/source (that royalty attribution
        stays internal)."""
        qs = parse_qs(parsed.query)
        try:
            n = max(1, min(int((qs.get("n") or ["25"])[0]), 100))
        except ValueError:
            n = 25
        self._json(200, {"history": _read_plays(n)})

    def _handle_admin_music_search(self, parsed):
        """Console MUSIC tab: like the public search but WITH paths and rotation flags."""
        qs = parse_qs(parsed.query)
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(200, {"results": []})
        import sqlite3 as _sq
        toks = [t for t in q.lower().split() if t][:6]
        where = " AND ".join("lower(artist || ' ' || title || ' ' || COALESCE(album,'')) LIKE ?" for _ in toks)
        con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT path, artist, title, album, duration_s FROM tracks WHERE %s ORDER BY artist, title LIMIT 25" % where,
            ["%%%s%%" % t for t in toks]).fetchall()
        con.close()
        pool = _rot_set()
        return self._json(200, {"results": [
            {"path": p, "artist": a or "", "title": t or "", "album": al or "",
             "duration_s": d, "in_rotation": p in pool}
            for p, a, t, al, d in rows]})

    def _handle_admin_rotation_info(self, parsed):
        qs = parse_qs(parsed.query)
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        q = (qs.get("q") or [""])[0].strip().lower()
        pool = sorted(_rot_set())
        hits = [p for p in pool if q in p.lower()] if q else pool
        return self._json(200, {"total": len(pool), "matched": len(hits),
                                "results": [{"path": p, "name": os.path.basename(p)} for p in hits[:100]]})

    def _handle_admin_chat_recent(self, parsed):
        qs = parse_qs(parsed.query)
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        try:
            msgs = telegram_chat.recent(50) if telegram_chat is not None else []
        except Exception:
            msgs = []
        def _names(key):
            try:
                return sorted(b.decode() if isinstance(b, bytes) else str(b) for b in rds.smembers(key))
            except Exception:
                return []
        banned = _names("chat:banned")
        for m in msgs:
            m["banned"] = (m.get("username") or "") in banned
        return self._json(200, {"messages": msgs, "banned": banned})

    def _handle_admin_console_post(self, parsed):
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        if parsed.path.startswith("/api/admin/rotation/"):
            path = (qs.get("path") or [""])[0]
            if not path.startswith("/mnt/"):
                return self._json(400, {"error": "bad path"})
            try:
                ok, msg = (_rot_add if parsed.path.endswith("/add") else _rot_remove)(path)
            except Exception as e:
                return self._json(502, {"error": "rotation edit failed: %s" % e})
            return self._json(200 if ok else 400, {"ok": ok, "message": msg})
        name = (qs.get("name") or [""])[0].strip()
        if not name or len(name) > 128:
            return self._json(400, {"error": "bad name (empty or over 128 chars)"})
        try:
            (rds.sadd if parsed.path.endswith("/ban") else rds.srem)("chat:banned", name)
        except Exception as e:
            return self._json(502, {"error": str(e)})
        verb = "hidden" if parsed.path.endswith("/ban") else "visible again"
        return self._json(200, {"ok": True, "message": "%s is now %s -- site, feed and bot at once" % (name, verb)})

    def _handle_stats(self):
        """Public: how many tracks the jukebox lists (SF, 2026-08-20: put the number on the
        homepage -- search finds more than the background plays). Cached 1 h."""
        now = time.time()
        if now - _STATS["t"] > 3600 or not _STATS["n"]:
            try:
                import sqlite3
                con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
                _STATS["n"] = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
                con.close()
                _STATS["t"] = now
            except Exception as e:
                if not _STATS["n"]:
                    return self._json(502, {"error": f"index unavailable: {e}"})
        self._json(200, {"tracks": _STATS["n"]})

    def _handle_sample(self, parsed):
        """A random handful of what is actually aboard.

        The search box is a blank stare if you do not already know what to ask
        for -- SF's point. This gives newcomers something to pick from, and it
        reads from the same index the search does, so it can never surface
        anything the search would not.

        src=wavlake (2026-08-19): a handful out of the widest deck Wavlake's PUBLIC api
        exposes -- the union of their sats rankings over 1/7/30/90 days, deduped, cached
        15 min. They publish no catalog-dump endpoint, so this plus search IS everything
        reachable. Same row shape as a search hit, so the play button takes the normal
        V4V road (210 sats, 206 forwarded).
        """
        qs = parse_qs(parsed.query)
        try:
            n = max(1, min(int((qs.get("n") or ["12"])[0]), 40))
        except ValueError:
            n = 12
        src = (qs.get("src") or [""])[0].strip().lower()
        if src == "wavlake":
            rows = _wavlake_top(n)
            if rows is None:
                return self._json(502, {"error": "Wavlake is not answering right now", "results": []})
            pool = len(_WAV_TOP.get("rows") or [])
            return self._json(200, {"label": ("Wavlake \u00b7 shuffling %d tracks" % pool) if pool else "Wavlake",
                                    "pool": pool, "results": rows})
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT artist, title, album FROM tracks "
                "WHERE title IS NOT NULL AND title != '' ORDER BY RANDOM() LIMIT ?",
                (n,),
            ).fetchall()
            con.close()
        except Exception as e:
            return self._json(502, {"error": f"library index unavailable: {e}"})
        self._json(200, {"label": "A taste of what's aboard", "results": [
            {"artist": a or "", "title": t or "", "album": al or ""} for a, t, al in rows
        ]})

    def _handle_icecast_status(self):
        """Icecast's own status JSON, served from the site's own origin.

        The public stream host sends no CORS header, so a browser could never
        read it directly -- every fetch failed and the audio player therefore
        reported "off air" while the stream was, in fact, up (found 2026-07-28).
        Proxying it here fixes that without touching Icecast or Caddy.
        """
        try:
            r = requests.get(ICECAST_STATUS_URL, timeout=6)
            r.raise_for_status()
            return self._json(200, r.json())
        except Exception as e:
            return self._json(502, {"error": f"icecast unreachable: {e}"})

    def _handle_playlist(self, parsed):
        """A plain playlist file, so anyone can listen in the player they already
        use -- VLC, foobar, a phone, a kitchen radio. Served from here rather than
        as a static file because the content type is what makes a player open it
        instead of a browser showing it as text.
        """
        url = os.environ.get("ICECAST_PUBLIC_URL", "https://stream.noderunnersradio.com/stream")
        if parsed.path.endswith(".pls"):
            body = ("[playlist]\nNumberOfEntries=1\n"
                    f"File1={url}\nTitle1=Noderunners Radio\nLength1=-1\nVersion=2\n")
            return self._xml(200, body, "audio/x-scpls", filename="noderunners-radio.pls")
        body = f"#EXTM3U\n#EXTINF:-1,Noderunners Radio\n{url}\n"
        self._xml(200, body, "audio/x-mpegurl", filename="noderunners-radio.m3u")

    def _handle_rss(self, parsed):
        """Recently played, as a feed. Same public data as /api/history, in the
        shape a feed reader understands -- no royalty attribution, no internals."""
        qs = parse_qs(parsed.query)
        try:
            n = max(1, min(int((qs.get("n") or ["50"])[0]), 200))
        except ValueError:
            n = 50
        site = os.environ.get("SITE_BASE_URL", "https://noderunnersradio.com").rstrip("/")

        def esc(s):
            return (str(s if s is not None else "").replace("&", "&amp;")
                    .replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

        items = []
        for p in _read_plays(n):
            when = p["ts"]
            try:
                when = format_datetime(datetime.fromisoformat(p["ts"]))
            except Exception:
                pass                                  # an odd timestamp is not worth a 500
            line = " - ".join(x for x in (p.get("artist"), p.get("title")) if x)
            guid = hashlib.sha1((p["ts"] + line).encode("utf-8")).hexdigest()
            items.append(
                "<item>"
                f"<title>{esc(line)}</title>"
                f"<link>{esc(site)}/</link>"
                f"<description>{esc(line)} -- aired on Noderunners Radio</description>"
                f"<pubDate>{esc(when)}</pubDate>"
                f'<guid isPermaLink="false">{guid}</guid>'
                "</item>"
            )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"><channel>'
            "<title>Noderunners Radio -- recently played</title>"
            f"<link>{esc(site)}/</link>"
            "<description>Every track the ship airs, newest first. "
            "Broadcasting from international waters.</description>"
            "<language>en</language>"
            f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>"
            + "".join(items)
            + "</channel></rss>"
        )
        self._xml(200, xml)

    def _handle_chat_recent(self):
        if telegram_chat is None:
            return self._json(200, {"messages": []})
        # feed_hidden mirrors the captain's chat brake (the bridge's flag
        # file): the OVERLAY chat strip obeys it, the site chat tab ignores it
        msgs = telegram_chat.recent(50)
        # Ban list (Solana-spam raid, 2026-08-14): Telegram never tells bots
        # about deletions, so a banned raider's messages would replay from the
        # relay's memory for hours. Names in the redis set chat:banned are
        # dropped at READ time -- site, feed and bot all clean at once.
        #   ban:   redis-cli -n 2 sadd chat:banned <username>
        #   unban: redis-cli -n 2 srem chat:banned <username>
        try:
            banned = {b.decode() if isinstance(b, bytes) else str(b)
                      for b in rds.smembers("chat:banned")}
        except Exception:
            banned = set()
        if banned:
            msgs = [m for m in msgs if (m.get("username") or "") not in banned]
        self._json(200, {"messages": msgs,
                         "feed_hidden": os.path.exists("/tmp/nr-chat-off")})

    def _handle_chat_media(self, parsed):
        if telegram_chat is None:
            return self._json(404, {"error": "chat relay not configured"})
        qs = parse_qs(parsed.query)
        file_id = (qs.get("file_id") or [""])[0]
        if not file_id:
            return self._json(400, {"error": "missing file_id"})

        path = telegram_chat.file_path(file_id)
        if not path:
            return self._json(404, {"error": "file not found"})

        upstream = telegram_chat.file_bytes(path)
        if upstream.status_code != 200:
            return self._json(502, {"error": "upstream fetch failed"})

        # Telegram serves GIFs (.mp4) and photos with a useless octet-stream
        # type, and browsers refuse to PLAY that -- name the type by extension
        # so the overlay can show the crew's GIFs (SF's ask 2026-08-01).
        ctype = upstream.headers.get("Content-Type", "") or ""
        if not ctype or ctype == "application/octet-stream":
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            ctype = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
                     "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "png": "image/png", "webp": "image/webp"}.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        for chunk in upstream.iter_content(chunk_size=65536):
            self.wfile.write(chunk)

    def _handle_search(self, parsed):
        qs = parse_qs(parsed.query)
        q = (qs.get("q") or [""])[0].strip()
        # src filter (SF 2026-07-30, the chips beside Shuffle): "" = both
        # lanes, "wavlake" = Wavlake only (wider window), "library" = ship
        # library only. Unknown values fall back to both lanes.
        src = (qs.get("src") or [""])[0].strip().lower()
        if src not in ("", "wavlake", "library"):
            src = ""
        # nr_rank: how many library rows to return. Clamped like every other n in this file. THE
        # DEFAULT IS UNCHANGED AT 10, deliberately: the website's own jukebox panel renders every row
        # it is handed, on every keystroke, so a raised default would change the public page unasked.
        # Only a caller that asks gets more -- which is the Telegram bot, when it pages.
        try:
            lim = max(1, min(int((qs.get("limit") or ["10"])[0]), SEARCH_LIMIT_MAX))
        except ValueError:
            lim = 10
        # nr_offset (2026-09-10): how many rows to SKIP, so a caller can walk past the first page.
        # Clamped like everything else here; anything unparseable is 0, which is exactly today.
        try:
            off = max(0, min(int((qs.get("offset") or ["0"])[0]), 2000))
        except Exception:
            off = 0
        if not q:
            return self._json(200, {"results": []})

        results, more = [], False
        if src != "wavlake":
            local = chain.by_name["local"]
            # nr_offset: ask for ONE more than the page needs, so "more" is knowledge rather than a
            # guess -- a Next button should only appear when there is somewhere to go.
            tracks = asyncio.run(local.search(q, limit=lim + 1, offset=off))
            more = len(tracks) > lim
            tracks = tracks[:lim]
            results = [{"artist": t.artist, "title": t.title, "album": t.album,
                        "source": "library", "sats": _play_price(t.duration_s),
                        "duration_s": t.duration_s} for t in tracks]

        # Wavlake lane (SF's rulings 2026-07-30): every row shows its source.
        # When a song exists in BOTH lanes, the Wavlake version wins --
        # "artists remain more value in the end." Results are cached 120 s so
        # live typing does not hammer their API. A lane failure never breaks
        # library search. The filtered view gets a wider window (21) than the
        # mixed view (8).
        wrows = []
        wlimit = 21 if src == "wavlake" else 8
        # nr_offset (review finding): this lane knows nothing about offset -- it would re-run the
        # identical query and prepend the SAME rows to page 2, page 3 and page 40. Past page one its
        # rows have already been shown, so it sits the rest out. src="wavlake" never pages (nothing
        # sends an offset with it), and src="" pages only from the library side.
        if src != "library" and len(q) >= 3 and off == 0:
            ck = f"wavlake:search:{wlimit}:" + q.lower()
            cached = rds.get(ck)
            if cached is not None:
                wrows = json.loads(cached)
            else:
                try:
                    wtracks = asyncio.run(wavlake.search(q, limit=wlimit))
                    # nr_wavphrase (2026-09-12): Wavlake matches a NAME, not a phrase. 'michilis digital silence'
                    # finds nothing there while 'michilis' finds the artist -- so a phrase that misses is asked
                    # again by its first word, and only the tracks that carry the other words stay.
                    words = q.lower().split()
                    if not wtracks and len(words) >= 2:
                        rest = words[1:]
                        again = asyncio.run(wavlake.search(words[0], limit=wlimit))
                        wtracks = [t for t in again
                                   if all(w in ((t.artist or "") + " " + (t.title or "") + " " + (t.album or "")).lower() for w in rest)]
                    wrows = [{"artist": t.artist, "title": t.title, "album": t.album,
                              "source": "wavlake", "guid": t.source_uri,
                              "sats": WAVLAKE_PLAY_PRICE_SATS} for t in wtracks]
                except Exception:
                    wrows = []
                rds.setex(ck, 120, json.dumps(wrows))
        wkeys = {(w["artist"].strip().lower(), w["title"].strip().lower()) for w in wrows}
        results = [r for r in results
                   if (r["artist"].strip().lower(), r["title"].strip().lower()) not in wkeys]
        # nr_offset: the caller is told where it is and whether there is a next page. Older callers
        # simply ignore the two extra keys.
        self._json(200, {"results": wrows + results, "offset": off, "more": bool(more)})

    def _session_cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _admin_authed(self, token):
        if bool(ADMIN_TOKEN) and token == ADMIN_TOKEN:
            return True
        return _session_valid(self._session_cookie())

    def _handle_chatfeed(self):
        """Show or hide the chat strip on the video feed.

        Whatever anyone types in the Telegram group goes out on the picture, so
        the captain needs a brake that does not involve a terminal. The bridge
        checks for this flag file every 5 seconds and blanks the strip while it
        exists. No sudo, no restart -- a file the bridge already watches.
        """
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "unauthorized"})
        action = (qs.get("action") or [""])[0]
        if action not in ("show", "hide", "status"):
            return self._json(400, {"error": "action must be show, hide or status"})
        flag = "/tmp/nr-chat-off"
        try:
            if action == "hide":
                open(flag, "w").close()
            elif action == "show":
                try:
                    os.remove(flag)
                except FileNotFoundError:
                    pass
            showing = not os.path.exists(flag)
            return self._json(200, {
                "showing": showing,
                "message": ("Chat is on the feed." if showing
                            else "Chat is hidden -- it clears the picture within 5 seconds."),
            })
        except Exception as e:
            return self._json(500, {"error": f"could not switch the chat strip: {e}"})

    def _handle_bridge(self):
        """Start/stop the Owncast bridge -- the station's own audio+image feed.

        Owncast has ONE ingest slot. The bridge holds it while nobody is
        streaming; a guest DJ needs it free. The console calls this when the
        lever moves, so the captain never has to open a terminal mid-session.
        Deliberately separate from the settlement path: this touches video
        only, never money.
        """
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "unauthorized"})
        action = (qs.get("action") or [""])[0]
        if action not in ("start", "stop", "status"):
            return self._json(400, {"error": "action must be start, stop or status"})
        try:
            if action == "status":
                r = subprocess.run(["systemctl", "is-active", "owncast-bridge"],
                                   capture_output=True, text=True, timeout=10)
                return self._json(200, {"running": r.stdout.strip() == "active"})
            subprocess.run(["sudo", "-n", "systemctl", action, "owncast-bridge"],
                           capture_output=True, text=True, timeout=20, check=True)
            time.sleep(1.5)
            r = subprocess.run(["systemctl", "is-active", "owncast-bridge"],
                               capture_output=True, text=True, timeout=10)
            running = r.stdout.strip() == "active"
            return self._json(200, {
                "running": running,
                "message": ("Station feed is back on air." if running
                            else "Station feed stopped -- the ingest slot is free for a DJ."),
            })
        except subprocess.CalledProcessError as e:
            return self._json(500, {"error": "bridge command refused (sudoers rule missing?)",
                                    "detail": (e.stderr or "")[:200]})
        except Exception as e:
            return self._json(500, {"error": "bridge command failed", "detail": str(e)[:200]})

    def _handle_session_check(self):
        self._json(200, {"authed": _session_valid(self._session_cookie()),
                         "configured": bool(ADMIN_USER and ADMIN_PASS_HASH and ADMIN_SESSION_SECRET)})

    def _handle_login(self):
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        if not (ADMIN_USER and ADMIN_PASS_HASH and ADMIN_SESSION_SECRET):
            return self._json(500, {"error": "Login not configured yet (ADMIN_USER / ADMIN_PASS_HASH / ADMIN_SESSION_SECRET)"})
        user = (qs.get("username") or [""])[0]
        pw = (qs.get("password") or [""])[0]
        time.sleep(0.5)  # blunt brute-force a little
        if not (hmac.compare_digest(user, ADMIN_USER) and _check_password(pw)):
            return self._json(403, {"error": "Wrong username or password"})
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}={_make_session()}; Path=/; Max-Age={SESSION_TTL_S}; "
                         f"HttpOnly; Secure; SameSite=Strict")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_logout(self):
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}=gone; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- BUMA/STEMRA opgave (SF's ruling 2026-08-13: monthly, unasked) ----
    def _handle_plays_rss(self, parsed):
        """PUBLIC live feed of aired tracks -- the link SF can hand a
        collecting society so they can log plays as they happen."""
        try:
            import buma_reports
            return self._xml(200, buma_reports.rss(50).decode("utf-8"))
        except Exception as e:
            return self._json(500, {"error": f"feed failed: {e}"})

    def _handle_buma_status(self, parsed):
        qs = parse_qs(parsed.query)
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "invalid or missing token"})
        try:
            import buma_reports
            return self._json(200, buma_reports.status())
        except Exception as e:
            return self._json(500, {"error": f"status failed: {e}"})

    def _handle_buma_report(self, parsed):
        """One month as a downloadable CSV, generated fresh from the play-log
        at click time -- there is never a stale report to accidentally mail."""
        qs = parse_qs(parsed.query)
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "invalid or missing token"})
        month = (qs.get("month") or [""])[0]
        kind = (qs.get("kind") or ["speellijst"])[0]
        if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
            return self._json(400, {"error": "month must be YYYY-MM"})
        if kind not in ("speellijst", "overzicht"):
            return self._json(400, {"error": "kind must be speellijst or overzicht"})
        try:
            import buma_reports
            make = (buma_reports.csv_speellijst if kind == "speellijst"
                    else buma_reports.csv_overzicht)
            # decode keeps the BOM as ﻿; _xml re-encodes it intact.
            # SF 2026-08-13: ONE file per month goes in the mail, named so the
            # receiver knows what it is without opening it.
            fname = ("buma-opgave-%s.csv" % month if kind == "speellijst"
                     else "overzicht-%s.csv" % month)
            return self._xml(200, make(month).decode("utf-8"),
                             content_type="text/csv; charset=utf-8",
                             filename=fname)
        except Exception as e:
            return self._json(500, {"error": f"report failed: {e}"})

    # ---- nr_mood (2026-09-05): console endpoints for the captain's mood queue ----
    def _handle_mood_get(self, parsed):
        if not self._admin_authed((parse_qs(parsed.query).get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        if parsed.path == "/api/admin/mood/playlists":
            out = []
            for p in _playlist_list():
                miss = 0
                try:
                    with open(os.path.join(_PLAYLIST_DIR, p["name"] + ".missing.tsv"), encoding="utf-8") as fh:
                        miss = max(0, sum(1 for _ in fh) - 1)
                except OSError:
                    pass
                out.append({"name": p["name"], "tracks": p["tracks"], "missing": miss})
            return self._json(200, {"playlists": out})
        try:
            return self._json(200, _mood_state())
        except Exception as e:
            return self._json(502, {"error": "mood state: %s" % e})

    def _handle_mood_post(self, parsed):
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        act = parsed.path.rsplit("/", 1)[-1]
        try:
            if act == "load":
                name = os.path.basename((qs.get("name") or [""])[0]).replace("..", "")
                if not name.endswith(".m3u"):
                    name += ".m3u"
                src = os.path.join(_PLAYLIST_DIR, name)
                if not name[:-4] or not os.path.isfile(src):
                    return self._json(400, {"error": "no such playlist"})
                with open(src, encoding="utf-8") as fh:
                    paths = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
                if not paths:
                    return self._json(400, {"error": "that playlist is empty on the server"})
                shuffle = (qs.get("shuffle") or ["0"])[0] == "1"
                items = _mood_items_for(paths)
                if shuffle:
                    import random as _rnd
                    _rnd.shuffle(items)
                with _mood_lock:
                    pipe = rds.pipeline()
                    pipe.delete("mood:list", "mood:skipped", "mood:pushed")
                    for it in items:
                        pipe.rpush("mood:list", json.dumps(it))
                    pipe.set("mood:name", name[:-4])
                    pipe.set("mood:played", 0)
                    pipe.set("mood:on", "1")
                    pipe.execute()
                    _mood_cache["recent_t"] = 0.0
                note = "" if _mood_lane_ok() else "; NOTE: liquidsoap does not have the mood lane yet -- nothing plays until that restart"
                return self._json(200, {"ok": True, "message": "%s: %d songs queued%s -- starts after the current song%s" % (
                    name[:-4], len(items), " (shuffled)" if shuffle else "", note)})
            if act == "pause":
                rds.set("mood:on", "0")
                return self._json(200, {"ok": True, "message": "paused -- the rotation takes over after the current song; Resume continues the list"})
            if act == "resume":
                if rds.llen("mood:list") == 0:
                    return self._json(400, {"error": "nothing to resume -- load a playlist"})
                rds.set("mood:on", "1")
                return self._json(200, {"ok": True, "message": "resumed -- continues after the current song"})
            if act == "off":
                with _mood_lock:
                    pipe = rds.pipeline()
                    pipe.set("mood:on", "0")
                    pipe.delete("mood:list", "mood:skipped")
                    pipe.execute()
                return self._json(200, {"ok": True, "message": "off -- list cleared; what was already handed to the deck (at most one song) still plays"})
            if act == "next":
                rid, m = _mood_on_air()
                with _mood_lock:
                    if not [r for r in _mood_queue_rids() if r != rid] and rds.get("mood:on") == b"1":
                        _mood_hand_over()
                if not rid:
                    return self._json(200, {"ok": True, "message": "no mood song on air -- the next one is handed to the deck and plays after the current song"})
                waiting = []
                for _ in range(8):
                    try:
                        waiting = [r for r in _mood_queue_rids() if r != rid]
                    except Exception:
                        waiting = []
                    if waiting:
                        break
                    time.sleep(0.5)
                if not waiting and rds.llen("mood:list") > 0:
                    return self._json(502, {"error": "could not hand the next song to the deck -- not skipping"})
                try:
                    if rid not in _mood_on_air_rids():
                        return self._json(200, {"ok": True, "message": "that song ended by itself"})
                except Exception:
                    pass
                _mood_send("mood.skip")
                return self._json(200, {"ok": True, "message": "skipped" + ("" if waiting else " -- the list is empty, the rotation follows")})
            if act == "remove":
                want = (qs.get("path") or [""])[0]
                if not want:
                    return self._json(400, {"error": "no path given"})
                with _mood_lock:
                    hit = None
                    for raw in rds.lrange("mood:list", 0, 60):     # the console shows the first 15; look a bit further
                        try:
                            if json.loads(raw).get("path") == want:
                                hit = raw
                                break
                        except Exception:
                            continue
                    if hit is None:
                        return self._json(400, {"error": "no such entry (the list moved on)"})
                    rds.lrem("mood:list", 1, hit)
                try:
                    it = json.loads(hit)
                    who = "%s - %s" % (it.get("artist", ""), it.get("title", ""))
                except Exception:
                    who = "entry"
                return self._json(200, {"ok": True, "message": "removed %s" % who})
            return self._json(404, {"error": "unknown mood action"})
        except Exception as e:
            return self._json(502, {"error": "mood %s: %s" % (act, e)})

    def _handle_admin_status(self, parsed):
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})

        try:
            on_air = on_air_rid()
            now_playing = _track_summary(on_air) if on_air else None
            queue = [_track_summary(rid) for rid in queue_rids()]
        except Exception as e:
            return self._json(500, {"error": f"Could not reach liquidsoap: {e}"})

        self._json(200, {"now_playing": now_playing, "queue": queue,
                          "duration_pricing": _duration_pricing_on()})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/request":
            return self._handle_request()
        if parsed.path == "/api/x/code":           # nr_xlane: the bot mints a code (localhost only)
            return self._handle_x_code_mint()
        if parsed.path == "/api/play":
            return self._handle_play()
        if parsed.path == "/api/admin/note":
            return self._handle_note_add()
        if parsed.path == "/api/admin/giftplay":
            return self._handle_giftplay()
        if parsed.path == "/api/donate":
            return self._handle_donate()
        if parsed.path in ("/api/admin/playlist/load", "/api/admin/playlist/shuffle"):
            # nr_mood (2026-09-05): RETIRED. These replaced the background rotation FILE with a playlist,
            # and the deal (nr-shuffle) silently undid that within 6 h. The captain's mood queue
            # (/api/admin/mood/*) plays a playlist ABOVE the rotation without touching it.
            return self._json(410, {"error": "retired -- use the captain's mood queue (/api/admin/mood/*)"})
        if parsed.path.startswith("/api/admin/mood/"):
            return self._handle_mood_post(parsed)
        if parsed.path in ("/api/admin/rotation/add", "/api/admin/rotation/remove",
                           "/api/admin/chat/ban", "/api/admin/chat/unban"):
            return self._handle_admin_console_post(parsed)
        if parsed.path == "/api/admin/pricing":
            length = int(self.headers.get("Content-Length", 0))
            pqs = parse_qs(self.rfile.read(length).decode())
            if not self._admin_authed((pqs.get("token") or [""])[0]):
                return self._json(403, {"error": "invalid or missing token"})
            on = (pqs.get("on") or [""])[0].strip().lower() in ("1", "on", "true", "yes")
            if on:
                rds.set("jukebox:duration_pricing", "on")
            else:
                rds.delete("jukebox:duration_pricing")
            _price_toggle["t"] = 0.0  # force the 15s cache to re-read now
            return self._json(200, {"ok": True, "duration_pricing": on})
        if parsed.path == "/api/admin/skip":
            return self._handle_admin_action(skip, "Skipped current track")
        if parsed.path == "/api/admin/flush":
            return self._handle_admin_action(flush_and_skip, "Flushed queue and skipped")
        if parsed.path == "/api/admin/livesplit/on":
            return self._handle_livesplit_on()
        if parsed.path == "/api/admin/overlay/layout":
            # captain-only write of a venue layout (drag & drop editor SAVE)
            length = int(self.headers.get("Content-Length", 0))
            oqs = parse_qs(self.rfile.read(length).decode())
            if not self._admin_authed((oqs.get("token") or [""])[0]):
                return self._json(403, {"error": "unauthorized -- log into the console in this browser first"})
            venue = "".join(c for c in (oqs.get("venue") or ["default"])[0].lower()
                            if c.isalnum() or c in "_-")[:24] or "default"
            try:
                layout = json.loads((oqs.get("layout") or ["{}"])[0])
                assert isinstance(layout, dict) and len(json.dumps(layout)) < 4000
            except Exception:
                return self._json(400, {"error": "bad layout"})
            rds.set("overlay:layout:" + venue, json.dumps(layout))
            return self._json(200, {"saved": venue})

        if parsed.path == "/api/admin/livesplit/join":
            # add DJs to an OPEN pool without settling and WITHOUT touching the
            # feed (SF's ruling mid-sesh 2026-07-30: a latecomer joins the
            # current pot; the lever cycle blinked the stream = bad UX).
            # Additions only -- removing someone mid-flight stays a settle.
            length = int(self.headers.get("Content-Length", 0))
            jqs = parse_qs(self.rfile.read(length).decode())
            if not self._admin_authed((jqs.get("token") or [""])[0]):
                return self._json(403, {"error": "invalid or missing token"})
            addrs = [w.strip() for w in (jqs.get("dj") or []) if w.strip()]
            aliases = [a.strip() for a in (jqs.get("alias") or [])]
            crew = [c.strip() for c in (jqs.get("crew") or []) if c.strip()]
            added = []

            def _grow(sess):
                del added[:]
                have = set()
                for d in sess.get("djs", []):
                    have.add((d.get("alias") or "").strip().lower())
                    have.add((d.get("address") or "").strip().lower())
                have.discard("")
                for i, a in enumerate(addrs):
                    if "@" not in a:
                        continue
                    alias = (aliases[i] if i < len(aliases) and aliases[i] else a)
                    if a.lower() in have or alias.lower() in have:
                        continue
                    sess["djs"].append({"alias": alias, "address": a})
                    added.append(alias)
                    have.add(a.lower()); have.add(alias.lower())
                for c in crew:
                    if c.lower() in have:
                        continue
                    sess["djs"].append({"alias": c, "address": "", "nopay": True})
                    added.append(c + " (free set)")
                    have.add(c.lower())
                return sess if added else None

            st, sess = _sess_update(_grow)
            if st == "none":
                return self._json(409, {"error": "No open session -- use the lever to start one"})
            if st == "closing":
                return self._json(409, {"error": "Session is settling -- the roster is frozen"})
            if st == "aborted":
                return self._json(200, {"message": "Nobody new to add -- everyone picked is already in the pool.", "added": []})
            if st != "ok":
                return self._json(502, {"error": "could not get a clean grip on the pool -- try again"})
            return self._json(200, {"message": "Added to the OPEN pool, feed untouched: " + ", ".join(added) +
                                    ". They share the current pot equally at settlement.", "added": added})

        if parsed.path == "/api/admin/livesplit/remove":
            return self._handle_livesplit_remove()
        if parsed.path == "/api/admin/livesplit/clear":
            return self._handle_livesplit_clear()
        if parsed.path == "/api/admin/livesplit/pool":
            return self._handle_livesplit_pool()
        if parsed.path == "/api/admin/livesplit/off":
            return self._handle_livesplit_off()
        if parsed.path == "/api/admin/livesplit/nowplaying":
            return self._handle_livesplit_nowplaying()
        if parsed.path == "/api/admin/chatfeed":
            return self._handle_chatfeed()
        if parsed.path == "/api/admin/bridge":
            return self._handle_bridge()
        if parsed.path == "/api/login":
            return self._handle_login()
        if parsed.path == "/api/logout":
            return self._handle_logout()
        return self._json(404, {"error": "not found"})

    # ---- live donation split (settlement model) -------------------------------
    # Lever ON = open a session (pot starts filling in the stage wallet).
    # Lever OFF = settle: whole-sat shares to each DJ, remainder stays radio.

    def _handle_livesplit_public(self):
        """Public, read-only. Exposes aliases + pot only -- never addresses."""
        try:
            sess = _load_session()
        except Exception:
            sess = None
        if not sess or not LNBITS_STAGE_ADMIN_KEY:
            return self._json(200, {"live": False})
        names = [d.get("alias") or "guest DJ" for d in sess.get("djs", [])]
        out = {"live": True, "dj": ", ".join(names), "djs": names,
               "dj_percent": sess.get("percent"), "count": len(names),
               "now_dj": sess.get("now_dj"), "now_djs": sess.get("now_djs") or []}
        try:
            out.update(_session_pot(sess))
        except Exception:
            pass
        self._json(200, out)

    def _handle_livesplit_admin_status(self, parsed):
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        if not LNBITS_STAGE_ADMIN_KEY:
            return self._json(500, {"error": "Live split not configured (LNBITS_STAGE_ADMIN_KEY missing)"})
        try:
            owed = rds.llen("wavlake:payouts:owed")
        except Exception:
            owed = -1  # -1 = could not read; the console says "check by hand"
        sess = _load_session()
        if not sess:
            return self._json(200, {"live": False, "wavlake_owed": owed})
        out = {"live": True, "percent": sess.get("percent"),
               "djs": [{"alias": d.get("alias"), "address": d.get("address"),
                        "nopay": bool(d.get("nopay"))} for d in sess.get("djs", [])],
               "started_at": sess.get("started_at"), "now_dj": sess.get("now_dj"),
               "now_djs": sess.get("now_djs") or [], "wavlake_owed": owed,
               "closing_age_s": (int(time.time()) - int(sess.get("closing") or 0)) if sess.get("closing") else None}
        try:
            out.update(_session_pot(sess))
        except Exception as e:
            out["pot_error"] = str(e)
        self._json(200, out)

    def _sesh_feed(self, opening):
        """Swap the feed plumbing when the session lever moves (SF's ruling
        2026-07-29): sesh OPEN = bridge off (ingest slot free) + reverse on
        (audio-only listeners follow the DJ); sesh CLOSED = the mirror image.
        Best-effort on purpose -- the money path must never fail on plumbing;
        anything that refused comes back as a note for the console to show."""
        steps = ([("stop", "owncast-bridge"), ("start", "owncast-reverse")] if opening
                 else [("stop", "owncast-reverse"), ("start", "owncast-bridge")])
        notes = []
        for action, unit in steps:
            try:
                subprocess.run(["sudo", "-n", "systemctl", action, unit],
                               capture_output=True, text=True, timeout=20, check=True)
            except Exception as e:
                notes.append(f"{unit} {action} refused: {str(e)[:120]}")
        return notes

    def _open_session(self, djs, percent):
        """Shared lever-ON path: sweep the dust-era targets, snapshot the stage
        balance as the pot baseline, store the session. djs = [{alias,address}]."""
        ghost = _load_session()
        if ghost is not None:
            who = ", ".join((d.get("alias") or d.get("address") or "?") for d in ghost.get("djs", [])) or "empty pool"
            age_h = (int(time.time()) - int(ghost.get("started_at", 0))) / 3600.0
            return self._json(409, {"error": "A session is already open (%s; started %.1f h ago). "
                                    "Settle it (lever OFF), or clear the ghost from the console -- "
                                    "opening over it would split tonight's pot with yesterday's crew (the 08-07 incident)." % (who, age_h)})
        _sweep_stage_targets()
        _sweep_jar_targets()
        try:
            baseline = _stage_balance_msat()
        except Exception as e:
            return self._json(502, {"error": f"Could not read the stage wallet: {e}"})
        sess = {"started_at": int(time.time()), "baseline_msat": baseline,
                "percent": percent, "djs": djs}
        if not rds.set(LIVESESSION_KEY, json.dumps(sess), nx=True):
            return self._json(409, {"error": "Two hands on the lever at once -- a session just opened; refresh and look."})
        feed_notes = self._sesh_feed(True)
        paying = _paying_djs(sess)
        names = ", ".join(d["alias"] for d in paying) or "nobody"
        crew_names = [d["alias"] for d in djs if d.get("nopay")]
        msg = f"Session OPEN -- pot is filling; {percent}% to {len(paying)} paid DJ{'s' if len(paying) != 1 else ''} at settlement: {names}."
        if crew_names:
            msg += f" On the lineup, no payout (their choice): {', '.join(crew_names)}."
        msg += " Feed handed over: bridge off, reverse on -- jukebox locked."
        if feed_notes:
            msg += " WARNING: " + "; ".join(feed_notes)
        self._json(200, {"message": msg, "live": True, "dj_percent": percent, "count": len(djs)})

    def _handle_livesplit_on(self):
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        if not LNBITS_STAGE_ADMIN_KEY:
            return self._json(500, {"error": "Live split not configured (LNBITS_STAGE_ADMIN_KEY missing)"})
        dj = (qs.get("dj") or [""])[0].strip()
        alias = (qs.get("alias") or [""])[0].strip()
        if "@" not in dj:
            return self._json(400, {"error": "dj must be a Lightning Address (name@domain)"})
        try:
            percent = int((qs.get("percent") or [str(DJ_SPLIT_PERCENT_DEFAULT)])[0])
        except ValueError:
            return self._json(400, {"error": "percent must be a whole number"})
        if not 1 <= percent <= 99:
            return self._json(400, {"error": "percent must be between 1 and 99"})
        return self._open_session([{"alias": alias or dj, "address": dj}], percent)

    def _handle_stage_paylink(self, parsed):
        """Get-or-create the STATIC pay link on the stage wallet -- the QR that
        goes in the video feed. Anonymous donations, split by the current pool."""
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        if not LNBITS_STAGE_ADMIN_KEY:
            return self._json(500, {"error": "Stage wallet not configured (LNBITS_STAGE_ADMIN_KEY missing)"})
        headers = {"X-Api-Key": LNBITS_STAGE_ADMIN_KEY, "Content-Type": "application/json"}
        try:
            resp = requests.get(f"{LNBITS_BASE_URL}/lnurlp/api/v1/links", headers=headers, timeout=15)
            resp.raise_for_status()
            link = next((l for l in (resp.json() or []) if l.get("username") == "dreadnode"), None)
            if link is None:
                resp = requests.post(
                    f"{LNBITS_BASE_URL}/lnurlp/api/v1/links",
                    headers=headers,
                    json={"description": "The Dread Node -- live session donations",
                          "min": 1, "max": 1000000, "username": "dreadnode", "zaps": False},
                    timeout=15,
                )
                if resp.status_code >= 300:
                    return self._json(502, {"error": f"LNbits refused the pay link: {resp.status_code} {resp.text[:200]}"})
                link = resp.json()
        except Exception as e:
            return self._json(502, {"error": f"Could not reach LNbits: {e}"})
        self._json(200, {"lightning_address": f"dreadnode@{LN_ADDRESS_DOMAIN}",
                         "lnurl": link.get("lnurl") or ""})

    def _handle_livesplit_pool(self):
        """One pool, split evenly: percent is the WHOLE DJ share (e.g. 25),
        divided across every dj= entry. dj= and alias= repeat pairwise."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        if not LNBITS_STAGE_ADMIN_KEY:
            return self._json(500, {"error": "Live split not configured (LNBITS_STAGE_ADMIN_KEY missing)"})

        addrs = [w.strip() for w in (qs.get("dj") or []) if w.strip()]
        aliases = [a.strip() for a in (qs.get("alias") or [])]
        # crew= entries (repeated): name-only lineup members -- on the overlay
        # and the site, NOT in the payout (SF 2026-07-30: some DJs refuse pay,
        # their choice; avatars still fly).
        crew = [c.strip() for c in (qs.get("crew") or []) if c.strip()]
        if not addrs and not crew:
            return self._json(400, {"error": "no DJs picked for the pool"})
        bad = [a for a in addrs if "@" not in a]
        if bad:
            return self._json(400, {"error": f"not a Lightning Address (name@domain): {', '.join(bad[:3])}"})
        try:
            percent = int((qs.get("percent") or [str(DJ_SPLIT_PERCENT_DEFAULT)])[0])
        except ValueError:
            return self._json(400, {"error": "percent must be a whole number"})
        if not 1 <= percent <= 99:
            return self._json(400, {"error": "percent must be between 1 and 99"})

        djs, seen = [], set()
        for i, a in enumerate(addrs):
            alias = (aliases[i] if i < len(aliases) and aliases[i] else a)
            if a.lower() in seen or alias.lower() in seen:
                continue          # ticked AND typed = one share, not two (08-26 review)
            seen.add(a.lower()); seen.add(alias.lower())
            djs.append({"alias": alias, "address": a})
        for c in crew:
            if c.lower() in seen:
                continue
            seen.add(c.lower())
            djs.append({"alias": c, "address": "", "nopay": True})
        return self._open_session(djs, percent)

    def _handle_livesplit_remove(self):
        """Take one DJ out of the OPEN pool. Both roster incidents (2026-08-07 and
        2026-08-25) were 'someone is wrongly in and there is no way to take them
        out'. Money-safe: nothing is paid here; the pot splits among who remains."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "invalid or missing token"})
        who = (qs.get("dj") or [""])[0].strip()
        if not who:
            return self._json(400, {"error": "say who: dj=<alias or address>"})
        out = []

        def _drop(sess):
            del out[:]
            keep = []
            for d in sess.get("djs", []):
                if (d.get("alias") or "").lower() == who.lower() or (d.get("address") or "").lower() == who.lower():
                    out.append(d.get("alias") or d.get("address") or "?")
                else:
                    keep.append(d)
            if not out:
                return None
            sess["djs"] = keep
            aliases = [d.get("alias") for d in keep]
            sess["now_djs"] = [a for a in (sess.get("now_djs") or []) if a in aliases]
            sess["now_dj"] = (sess["now_djs"][0] if sess["now_djs"] else None)
            return sess

        st, sess = _sess_update(_drop)
        if st == "none":
            return self._json(409, {"error": "no open session"})
        if st == "closing":
            return self._json(409, {"error": "session is settling right now -- the roster is frozen"})
        if st == "aborted":
            return self._json(404, {"error": "%r is not in the pool" % who})
        if st != "ok":
            return self._json(502, {"error": "could not get a clean grip on the pool -- try again"})
        left = ", ".join((d.get("alias") or d.get("address") or "?") for d in sess.get("djs", [])) or "NOBODY -- an empty pool settles everything to the radio"
        return self._json(200, {"message": "Removed %s from the pool -- nothing was paid. Splitting now: %s." % (", ".join(out), left)})

    def _handle_livesplit_clear(self):
        """Delete a ghost or crashed session WITHOUT paying anyone: the pot stays
        with the radio wallet. Atomic (WATCH): what is backed up is exactly what
        is deleted. Refuses while a settlement is actually RUNNING (fresh closing
        flag, under 10 min); an older flag means the settle crashed and clear is
        the way out -- the owed roster survives in the backup and the journal."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "invalid or missing token"})
        stamp = time.strftime("%Y%m%d-%H%M%S")
        sess = None
        closing = 0
        for _ in range(8):
            pipe = rds.pipeline()
            try:
                pipe.watch(LIVESESSION_KEY)
                raw = pipe.get(LIVESESSION_KEY)
                if raw is None:
                    return self._json(200, {"message": "No session to clear.", "live": False})
                sess = json.loads(raw)
                closing = int(sess.get("closing") or 0)
                if closing and time.time() - closing < 600:
                    return self._json(409, {"error": "A settlement is RUNNING right now (started %ds ago). "
                                            "Wait for it to finish; clear only if it never does." % int(time.time() - closing)})
                pipe.multi()
                pipe.set(LIVESESSION_KEY + ".bak-" + stamp, raw, ex=60 * 86400)
                pipe.delete(LIVESESSION_KEY)
                pipe.execute()
                break
            except redis.WatchError:
                continue
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass
        else:
            return self._json(502, {"error": "could not get a clean grip on the session -- try again"})
        try:
            rds.rpush("livesession:settlements", json.dumps({
                "ts": int(time.time()), "cleared": True, "was_closing": bool(closing),
                "roster": [(d.get("alias") or d.get("address") or "?") for d in sess.get("djs", [])]}))
        except Exception:
            pass
        feed_notes = self._sesh_feed(False)
        msg = ("Session cleared -- NOBODY was paid, the pot stays with the radio. "
               "Backup: %s.bak-%s (60 days). Feed back to radio." % (LIVESESSION_KEY, stamp))
        if closing:
            msg += " NOTE: this session had a crashed settlement -- who was owed is in the backup; tell Claude to reconcile."
        if feed_notes:
            msg += " WARNING: " + "; ".join(feed_notes)
        return self._json(200, {"message": msg, "live": False})

    def _handle_livesplit_nowplaying(self):
        """Mark which pool DJ is on deck right now (purely cosmetic -- drives
        the overlay highlight; settlement math is untouched). Manual for now,
        slot-schedule automation later."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        alias = (qs.get("dj") or [""])[0].strip()
        unknown = []

        def _mark(sess):
            del unknown[:]
            aliases = [d.get("alias") for d in sess.get("djs", [])]
            now = [a for a in (sess.get("now_djs") or []) if a in aliases]
            if not alias or alias.lower() in ("none", "nobody"):
                now = []
            elif alias in aliases:
                # tap toggles -- multiple DJs can be on deck for back-to-back sets
                if alias in now:
                    now.remove(alias)
                else:
                    now.append(alias)
            else:
                unknown.append(alias)
                return None
            sess["now_djs"] = now
            sess["now_dj"] = now[0] if now else None   # legacy single-DJ field
            return sess

        st, sess = _sess_update(_mark)
        if st == "none":
            return self._json(400, {"error": "no session open"})
        if st == "closing":
            return self._json(409, {"error": "session is settling -- the set is over"})
        if st == "aborted":
            return self._json(400, {"error": "unknown DJ: " + alias})
        if st != "ok":
            return self._json(502, {"error": "could not write -- tap again"})
        now = sess.get("now_djs") or []
        msg = ("On deck: " + ", ".join(now)) if now else "Nobody marked on deck"
        self._json(200, {"message": msg, "now_djs": now})

    def _handle_livesplit_off(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        if not LNBITS_STAGE_ADMIN_KEY:
            return self._json(500, {"error": "Live split not configured (LNBITS_STAGE_ADMIN_KEY missing)"})
        # always kill any stray dust-era instant-split targets, session or not
        _sweep_stage_targets()
        _sweep_jar_targets()

        # FREEZE the roster first (2026-08-25 review): from this instant every
        # join/remove/on-deck tap refuses, so the roster paid below is exactly the
        # roster that exists -- no joiner can vanish inside the LNbits call, and
        # no late write can resurrect the session after the delete.
        st, sess = _sess_update(lambda s: dict(s, closing=int(time.time())))
        if st == "none":
            # self-heal: lever OFF always forces radio plumbing, session or not
            self._sesh_feed(False)
            return self._json(200, {"message": "No session was open -- swept stray split targets, feed forced back to radio; donations stay 100% with the radio",
                                    "live": False})
        if st == "closing":
            age = int(time.time()) - int((sess or {}).get("closing") or 0)
            if age > 600:
                return self._json(409, {"error": "A settlement STARTED %d minutes ago and never finished -- it probably "
                                        "crashed. The roster is frozen and SAFE. Press 'Clear session' on the console "
                                        "(it pays NOBODY, keeps a backup), then tell Claude to reconcile who was owed." % (age // 60)})
            return self._json(409, {"error": "Settlement is already running -- one lever throw is enough."})
        if st != "ok":
            return self._json(502, {"error": "Could not freeze the session -- throw the lever again."})

        try:
            pot_msat = max(0, _stage_balance_msat() - int(sess.get("baseline_msat", 0)))
        except Exception as e:
            # unfreeze ONLY if the key still holds our frozen copy -- a clear (or a
            # clear + brand-new open) during the wallet call must never be undone
            thawed = dict(sess)
            thawed.pop("closing", None)
            for _ in range(4):
                pipe = rds.pipeline()
                try:
                    pipe.watch(LIVESESSION_KEY)
                    cur = pipe.get(LIVESESSION_KEY)
                    if cur is None or json.loads(cur) != sess:
                        break                  # cleared or replaced meanwhile: leave it be
                    pipe.multi()
                    pipe.set(LIVESESSION_KEY, json.dumps(thawed))
                    pipe.execute()
                    break
                except redis.WatchError:
                    continue
                finally:
                    try:
                        pipe.reset()
                    except Exception:
                        pass
            return self._json(502, {"error": f"Could not read the stage wallet -- session left open: {e}"})

        # atomic claim: redis DELETE returns 1 for exactly ONE caller -- a second
        # concurrent or repeated lever-off loses the claim and pays NOTHING
        # (fix for the 2026-07-23 double-settlement)
        if rds.delete(LIVESESSION_KEY) != 1:
            return self._json(200, {"message": "Session already settled -- nothing paid twice.", "live": False})

        djs = _paying_djs(sess)
        percent = int(sess.get("percent", DJ_SPLIT_PERCENT_DEFAULT))
        n = max(1, len(djs))
        per_dj_sats = int(pot_msat * percent / 100 / n) // 1000  # whole sats, floored

        # the OWED list hits the journal IMMEDIATELY after the claim -- before the
        # feed handover's slow systemctl calls and before any payment leaves. A
        # crash from here on always leaves a record of who was due (08-26 review).
        try:
            rds.rpush("livesession:settlements", json.dumps({
                "ts": int(time.time()), "intent": True, "pot_sats": pot_msat // 1000,
                "percent": percent, "per_dj_sats": per_dj_sats,
                "owed": [{"alias": d.get("alias"), "address": d.get("address")} for d in djs]}))
        except Exception:
            pass

        # session claimed: hand the feed back to radio before settling, so the
        # plumbing flips even if a payout below fails
        feed_notes = self._sesh_feed(False)

        paid, failed = [], []
        if per_dj_sats >= 1:
            for d in djs:
                try:
                    _lnurlp_pay_address(d["address"], per_dj_sats)
                    paid.append(d.get("alias") or d["address"])
                except Exception as e:
                    failed.append(f"{d.get('alias') or d['address']} ({str(e)[:80]})")

        # session was already closed by the atomic claim above; anything unpaid stays with the radio

        # settlement history: feeds the future Active-DJs stats page
        try:
            rds.rpush("livesession:settlements", json.dumps({
                "ts": int(time.time()), "pot_sats": pot_msat // 1000, "percent": percent,
                "per_dj_sats": per_dj_sats, "paid": paid, "failed": failed}))
        except Exception:
            pass

        pot_sats = pot_msat // 1000
        radio_keep = pot_sats - per_dj_sats * len(paid)
        msg = f"Settled: pot {pot_sats} sats"
        if per_dj_sats >= 1 and paid:
            msg += f" -- {per_dj_sats} sats to each of {len(paid)} DJ{'s' if len(paid) > 1 else ''} ({', '.join(paid)})"
        elif per_dj_sats < 1 and djs:
            msg += " -- shares under 1 sat, nothing paid out"
        msg += f"; {radio_keep} sats stay with the radio."
        msg += " Feed back to radio -- jukebox open."
        if feed_notes:
            msg += " WARNING: " + "; ".join(feed_notes)
        if failed:
            msg += f" FAILED (sats stay with the radio, sort manually): {'; '.join(failed)}"
        self._json(200, {"message": msg, "live": False, "pot_sats": pot_sats,
                         "per_dj_sats": per_dj_sats, "paid": paid, "failed": failed})

    def _handle_play_status(self, parsed):
        qs = parse_qs(parsed.query)
        payment_hash = (qs.get("payment_hash") or [""])[0].strip()
        if not payment_hash:
            return self._json(400, {"error": "missing payment_hash"})

        raw = rds.get(_pending_play_key(payment_hash))
        if raw is None:
            return self._json(404, {"error": "unknown or expired payment_hash"})
        pending = json.loads(raw)

        # nr_notes v2: the site sends note= (+ the mint-time note_token) with every poll until
        # queued. Written to its OWN key with the pending record's remaining TTL -- the pending
        # record itself is never rewritten here (that is _try_queue's job, under its lock).
        nqs = parse_qs(parsed.query, keep_blank_values=True)
        if "note" in nqs and not pending.get("queued") and _notes_on():
            tok = (nqs.get("nt") or [""])[0]
            if tok and pending.get("note_token") and hmac.compare_digest(tok, pending["note_token"]):
                note = _clean_note((nqs.get("note") or [""])[0], bool(pending.get("full")))
                ttl = rds.ttl(_pending_play_key(payment_hash))
                if ttl and ttl > 0:
                    if note:
                        rds.setex(_pending_note_key(payment_hash), ttl, note)
                    else:
                        rds.delete(_pending_note_key(payment_hash))
                    who = _clean_who((nqs.get("who") or [""])[0])
                    if who:
                        rds.setex(_pending_name_key(payment_hash), ttl, who)
                    else:
                        rds.delete(_pending_name_key(payment_hash))

        if pending.get("queued"):
            return self._json(200, {"paid": True, "queued": True, "message": pending.get("message", "Already queued")})

        try:
            paid = _is_play_invoice_paid(
                payment_hash,
                key=WAVLAKE_INVOICE_KEY if pending.get("wavlake") else "")
        except Exception as e:
            return self._json(502, {"error": f"Could not reach LNbits: {e}"})

        if not paid:
            return self._json(200, {"paid": False, "queued": False})

        ok, message, wait = _try_queue(payment_hash, pending)
        if wait:
            return self._json(200, {"paid": True, "queued": False,
                                    "message": f"Zap seen! Queuing in {wait}s (anti-spam)"})
        if not ok:
            return self._json(500, {"error": f"Paid but could not queue track: {message}"})
        self._json(200, {"paid": True, "queued": True, "message": message})

    # ---- nr_notes_channel (2026-09-08): localhost-only ADMIN NOTES -> the bot relays them to SF's Telegram ----
    # Any local service (the X announcer today, the repeat watchdog later) can hand SF one line without owning
    # a Telegram token. Same shape as play:unqueued: a redis list, peek + ack, unreadable entries set aside.
    def _local_only(self):
        return not (self.client_address[0] not in ("127.0.0.1", "::1")
                    or self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded"))

    def _handle_note_add(self):
        if not self._local_only():
            return self._json(403, {"error": "local callers only"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 4000:
            return self._json(400, {"error": "bad length"})
        try:
            d = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            src = str(d.get("src") or "")[:32].strip() or "note"
            text = str(d.get("text") or "")[:600].strip()
        except Exception:
            return self._json(400, {"error": "bad json"})
        if not text:
            return self._json(400, {"error": "empty"})
        rec = {"id": os.urandom(8).hex(), "ts": time.time(), "src": src, "text": text,
               "group": bool(d.get("group"))}   # nr_groupping: the relay also tells the public group
        try:
            rds.rpush("admin:notes", json.dumps(rec))
            rds.ltrim("admin:notes", -200, -1)
        except Exception as e:
            return self._json(502, {"error": "redis: %s" % e})
        return self._json(200, {"ok": True, "id": rec["id"]})

    def _handle_notes(self, parsed):
        if not self._local_only():
            return self._json(403, {"error": "local callers only"})
        qs = parse_qs(parsed.query)
        acks = [h for h in (qs.get("ack") or [""])[0].split(",") if h]
        try:
            if acks:
                removed = 0
                for raw in rds.lrange("admin:notes", 0, 199):
                    try:
                        if json.loads(raw).get("id") in acks:
                            removed += rds.lrem("admin:notes", 0, raw)
                    except Exception:
                        pass
                return self._json(200, {"removed": removed})
            items = []
            for raw in rds.lrange("admin:notes", 0, 39):
                try:
                    it = json.loads(raw)
                    if not isinstance(it, dict) or not it.get("id") or not it.get("text"):
                        raise ValueError("not a note")
                    items.append(it)
                except Exception:
                    try:
                        rds.rpush("admin:notes.bad", raw)
                        rds.lrem("admin:notes", 1, raw)
                    except Exception:
                        pass
                if len(items) >= 20:
                    break
            return self._json(200, {"items": items, "total": int(rds.llen("admin:notes") or 0)})
        except Exception as e:
            return self._json(502, {"error": "redis: %s" % e})

    def _handle_unqueued(self, parsed):
        """Localhost-only (the bot): the 'paid but could not queue' fact list.
        GET (any args but ack): the first 20 readable entries, nothing removed; unreadable entries
        are moved aside to play:unqueued.bad (kept, never deleted) so they cannot clog the window.
        GET ...?ack=<hash>,<hash>: remove only those entries -- the bot acks what it has posted.
        Nothing is re-queued or credited here, ever."""
        if (self.client_address[0] not in ("127.0.0.1", "::1")
                or self.headers.get("X-Forwarded-For")
                or self.headers.get("Forwarded")):
            return self._json(403, {"error": "local callers only"})
        qs = parse_qs(parsed.query)
        acks = [h for h in (qs.get("ack") or [""])[0].split(",") if h]
        try:
            if acks:
                removed = 0
                for raw in rds.lrange("play:unqueued", 0, 199):
                    try:
                        if json.loads(raw).get("payment_hash") in acks:
                            removed += rds.lrem("play:unqueued", 0, raw)
                    except Exception:
                        pass
                return self._json(200, {"removed": removed})
            items = []
            for raw in rds.lrange("play:unqueued", 0, 39):
                try:
                    it = json.loads(raw)
                    if not isinstance(it, dict) or not it.get("payment_hash"):
                        raise ValueError("not a record")
                    items.append(it)
                except Exception:
                    try:
                        rds.rpush("play:unqueued.bad", raw)
                        rds.lrem("play:unqueued", 1, raw)
                    except Exception:
                        pass
                if len(items) >= 20:
                    break
            return self._json(200, {"items": items, "total": int(rds.llen("play:unqueued") or 0)})
        except Exception as e:
            return self._json(502, {"error": "redis: %s" % e})

    def _from_the_box(self) -> bool:
        """True only for a call made ON this machine that Caddy never touched. The public /api/* is
        proxied and its requests ALSO arrive from 127.0.0.1 -- but the proxy always stamps
        X-Forwarded-For, and the bot's direct localhost calls never do. Exactly the test
        _handle_giftplay has used since 2026-08-14, and the same trade: worst case if a local process is
        compromised, someone puts a link in a shoutout. No money moves on this path, ever."""
        return (self.client_address[0] in ("127.0.0.1", "::1")
                and not self.headers.get("X-Forwarded-For")
                and not self.headers.get("Forwarded"))

    def _handle_giftplay(self):
        """Captain's gift: queue a library track free of charge, settled the
        moment it queues -- deliberately NO voucher/balance/ledger, per SF's
        no-credit law (2026-08-04; gift design 2026-08-14).

        LOCALHOST-ONLY on purpose: the Telegram bot lives on this same box and
        holds zero station secrets by design. Worst case if the bot is fully
        compromised: someone queues songs. No money moves on this path, ever.
        """
        # Truly-local only: Caddy proxies the public /api/* here and its
        # requests ALSO arrive from 127.0.0.1 -- but the proxy always stamps
        # X-Forwarded-For, and the bot's direct localhost calls never do.
        if (self.client_address[0] not in ("127.0.0.1", "::1")
                or self.headers.get("X-Forwarded-For")
                or self.headers.get("Forwarded")):
            return self._json(403, {"error": "local callers only"})
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        artist = (qs.get("artist") or [""])[0].strip()
        title = (qs.get("title") or [""])[0].strip()
        if not artist or not title:
            return self._json(400, {"error": "missing artist/title"})
        if _load_session() is not None:
            return self._json(409, {"error": "live sesh on -- the DJ has the deck"})

        # modest brake so a runaway caller cannot flood the airwaves
        try:
            n = rds.incr("giftplay:hour")
            if n == 1:
                rds.expire("giftplay:hour", 3600)
            if int(n) > 6:
                return self._json(429, {"error": "gift limit reached this hour"})
        except Exception:
            pass

        local = chain.by_name["local"]
        # nr_rank: the SAME ceiling search uses. At 5 this could not find a track that search had just
        # shown, and answered 404 "Track not found in library" on a row the person could see.
        matches = asyncio.run(local.search(f"{artist} {title}", limit=SEARCH_LIMIT_MAX))
        track = next(
            (t for t in matches
             if t.artist.strip().lower() == artist.lower()
             and t.title.strip().lower() == title.lower()), None)
        if not track:
            return self._json(404, {"error": "track not found in library"})
        if not track.local_path or not os.path.exists(track.local_path):
            return self._json(404, {"error": "file missing from the library disks"})
        try:
            _mark_paid_rid(push_track(track.local_path,
                       rights_class=track.rights_class.value,
                       source="gift",
                       artist=track.artist, title=track.title), track.local_path, "", "gift")
        except Exception as e:
            return self._json(500, {"error": f"could not queue: {e}"})
        return self._json(200, {
            "message": f"Queued '{track.artist} - {track.title}' as a gift"})

    def _handle_admin_action(self, fn, success_message):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})

        try:
            fn()
        except Exception as e:
            return self._json(500, {"error": f"Command failed: {e}"})

        self._json(200, {"message": success_message})

    def _handle_request(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(400, {"error": "missing q"})
        if not WANTED_LIST_OPEN:
            return self._json(403, {
                "added": False, "votes": 0,
                "message": "Requests for tracks we don't own yet are paused while we tighten the hatches. The jukebox itself still works -- search and play anything already aboard.",
            })
        # belt and braces for when it reopens: bound the length and refuse
        # control characters outright, so nothing odd can ever reach storage
        if len(q) > 120:
            return self._json(400, {"error": "that is too long for a song title"})
        if any(ord(c) < 32 or ord(c) == 127 for c in q):
            return self._json(400, {"error": "that request contains characters we do not accept"})

        # Only a MusicBrainz-confirmed real song gets added -- keeps the
        # wanted list a genuine demand signal instead of a typo bin.
        hits = asyncio.run(musicbrainz.search(q, limit=1))
        if not hits:
            return self._json(200, {"votes": 0, "message": "No results found", "added": False})

        votes = asyncio.run(wanted.add(q))
        self._json(200, {"votes": votes, "message": "Added to the wanted list", "added": True})

    def _handle_x_code_mint(self):
        """nr_xlane: POST q=&sats=[&note=] from the X bot on this box -> {code, sats, wl_sats, url, expires_in}.
        Never from the web: the price is the caller's word here, and only a local process may give it."""
        if not self._from_the_box():
            return self._json(403, {"error": "local callers only"})
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        q = _clean_query((qs.get("q") or [""])[0])
        if not q:
            return self._json(400, {"error": "missing q"})
        try:
            sats = int((qs.get("sats") or ["0"])[0])
        except Exception:
            sats = 0
        if sats < PLAY_PRICE_SATS or sats > XCODE_SATS_MAX:
            return self._json(400, {"error": "sats out of range (floor %d)" % PLAY_PRICE_SATS})
        if _load_session() is not None:
            return self._json(409, {"error": "A live sesh is on -- the DJ has the deck. The jukebox reopens when the sesh ends."})
        note = _clean_note((qs.get("note") or [""])[0]) if _notes_on() else ""
        try:
            code = _xcode_new()
            rec = {"q": q, "sats": sats, "note": note, "created": time.time()}
            rds.setex(_xcode_key(code), XCODE_TTL_S, json.dumps(rec))
        except Exception as e:
            return self._json(502, {"error": "Could not store the code: %s" % str(e)[:120]})
        return self._json(200, {"code": code, "sats": sats, "wl_sats": sats + WAVLAKE_PLAY_PRICE_SATS,
                                "expires_in": XCODE_TTL_S, "url": SITE_URL + "/?jukebox&p=" + code})

    def _handle_x_code_get(self, parsed):
        """nr_xlane: GET ?c=<code> -> what the page needs to open on that search. Public; a code is unguessable."""
        code = (parse_qs(parsed.query).get("c") or [""])[0].strip().lower()
        rec = _xcode_load(code)
        if rec == XCODE_ERR:
            return self._json(503, {"error": "The station is busy. Try again in a moment."})
        if rec is None:
            return self._json(404, {"error": "This link has expired. Ask again on X."})
        try:
            ttl = rds.ttl(_xcode_key(code))
        except Exception:
            ttl = 0
        sats = int(rec.get("sats") or 0)
        out = {"q": rec.get("q", ""), "sats": sats, "wl_sats": sats + WAVLAKE_PLAY_PRICE_SATS,
               "status": "minted" if rec.get("payment_hash") else "open",
               "expires_in": max(0, int(ttl or 0))}
        if rec.get("payment_hash"):
            out["artist"], out["title"] = rec.get("artist", ""), rec.get("title", "")
        return self._json(200, out)

    def _handle_play(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        artist = (qs.get("artist") or [""])[0].strip()
        title = (qs.get("title") or [""])[0].strip()
        if not artist or not title:
            return self._json(400, {"error": "missing artist/title"})


        # SF's ruling 2026-07-29: while a live sesh is open the jukebox is shut --
        # a DJ has the deck, and a paid queue would fight the harbor for nothing.
        if _load_session() is not None:
            return self._json(409, {"error": "A live sesh is on -- the DJ has the deck. The jukebox reopens when the sesh ends."})

        if not LNBITS_PLAY_INVOICE_KEY:
            return self._json(500, {"error": "Payments not configured (LNBITS_PLAY_INVOICE_KEY missing)"})

        # nr_xlane: a one-time pay code from the X lane. It sets the price, the name, the message and the
        # door, and it binds to the first song it mints for. No code = exactly the path below, unchanged.
        xcode = (qs.get("code") or [""])[0].strip().lower()
        xrec = None
        if xcode:
            xrec = _xcode_load(xcode)
            if xrec == XCODE_ERR:
                return self._json(503, {"error": "The station is busy. Try again in a moment."})
            if xrec is None:
                return self._json(410, {"error": "This link has expired. Ask again on X."})
            if xrec.get("payment_hash"):
                posted_guid = "".join(c for c in (qs.get("guid") or [""])[0].strip() if c.isalnum() or c in "-_")[:64]
                same = ((xrec.get("artist") or "").strip().lower() == artist.lower()
                        and (xrec.get("title") or "").strip().lower() == title.lower())
                if xrec.get("source") == "wavlake":
                    # Wavlake rewrites names on resolve; the guid is the song's real identity there
                    same = bool(posted_guid) and posted_guid == (xrec.get("guid") or "")
                if same and isinstance(xrec.get("reply"), dict):
                    return self._json(200, xrec["reply"])
                # nr_xlane2 (SF 2026-09-12): another song on a used link is a NEW invoice at the same X price --
                # a second person on X may tap the same link and pay for their own pick. The link keeps its
                # first binding; the extra invoice is remembered on the record.
                xrec["_extra"] = True
            if not _xcode_claim(xcode):
                return self._json(409, {"error": "One moment: an invoice for this link is being made. Tap again in a few seconds."})

        # ---- Wavlake lane: 210 sats, spooled from their CDN, never stored in
        # the library. The guid comes from our own search results, but we
        # re-resolve it against Wavlake before we take a single sat.
        if (qs.get("source") or [""])[0].strip().lower() == "wavlake":
            guid = (qs.get("guid") or [""])[0].strip()
            guid = "".join(c for c in guid if c.isalnum() or c in "-_")[:64]
            if not guid:
                return self._json(400, {"error": "missing guid for the Wavlake track"})
            probe = _WTrack(title=title, artist=artist, source="wavlake",
                            source_uri=guid, rights_class=_WRights.V4V,
                            availability=_WAvail.PLAYABLE)
            resolved = wavlake._resolve_sync(probe)
            media = resolved.extra.get("media_url") if resolved else None
            if not media:
                return self._json(404, {"error": "Track not found on Wavlake"})
            try:
                # nr_xlane: on the X lane the artist's 210 stays whole and the X share rides on top
                wl_price = WAVLAKE_PLAY_PRICE_SATS + (int(xrec.get("sats") or 0) if xrec else 0)
                invoice = _create_play_invoice(
                    memo=f"Zap (V4V): {resolved.artist} - {resolved.title}"[:120],
                    sats=wl_price, key=WAVLAKE_INVOICE_KEY)
            except Exception as e:
                return self._json(502, {"error": f"Could not create invoice: {e}"})
            if not invoice.get("payment_hash") or not invoice.get("bolt11"):
                return self._json(502, {"error": "LNbits returned an unexpected invoice response"})
            # nr_full: granted here, once, on proof of caller. A website visitor is always proxied.
            full_note = bool(self._from_the_box()
                             and (qs.get("full") or [""])[0].strip().lower() in ("1", "true", "yes", "on"))
            pending = {
                "artist": resolved.artist, "title": resolved.title,
                "wavlake": True, "guid": guid, "media_url": media,
                "rights_class": "v4v", "source": "wavlake", "queued": False,
                "note": _clean_note((qs.get("note") or [""])[0], full_note) if _notes_on() else "",
                "who": _clean_who((qs.get("who") or [""])[0]) if _notes_on() else "",
                # nr_at: the DOOR is proof-of-caller too, never the caller's word. /api/play is public --
                # the website posts to it -- so without this gate anyone could send via=tg and buy a live
                # Telegram mention of a stranger for 21 sats, which is the exact harm this whole change
                # exists to prevent. Both real callers are on loopback and keep their stamp.
                "via": (_clean_via((qs.get("via") or [""])[0]) if self._from_the_box() else "web"),
                "full": full_note,
                "note_token": _secrets.token_hex(16),
            }
            pending["minted_at"] = time.time()
            if xrec:
                _xlane_stamp(pending, xrec, xcode)          # nr_xlane
            rds.setex(_pending_play_key(invoice["payment_hash"]), PENDING_RECORD_TTL_S, json.dumps(pending))
            reply = {
                "payment_hash": invoice["payment_hash"],
                "bolt11": invoice["bolt11"],
                "sats": wl_price,
                "notes_enabled": _notes_on(), "note_token": pending["note_token"], "note_max": NOTE_MAX,
                "message": f"Zap {wl_price} sats to queue '{resolved.artist} - {resolved.title}' (Wavlake, V4V)",
            }
            if xrec:
                reply = _xlane_reply(reply)
                try:
                    (_xcode_extra if xrec.get("_extra") else _xcode_bind)(xcode, xrec, pending, invoice, reply)
                except Exception:
                    pass                                   # the invoice and its record exist; the reply must still go out
                _xcode_release(xcode)
            return self._json(200, reply)

        # Never trust a client-supplied path -- re-look-up the track ourselves so
        # only files actually in the indexed library can ever be queued.
        local = chain.by_name["local"]
        # nr_rank: the SAME ceiling search uses. At 5 this could not find a track that search had just
        # shown, and answered 404 "Track not found in library" on a row the person could see.
        matches = asyncio.run(local.search(f"{artist} {title}", limit=SEARCH_LIMIT_MAX))
        named = [t for t in matches
                 if t.artist.strip().lower() == artist.lower()
                 and t.title.strip().lower() == title.lower()]
        # nr_version (2026-09-10, SF's own four "Walk Of Life" rows): artist + title is NOT a unique
        # key. The library holds one song on several albums at several lengths, and taking the FIRST
        # name match meant tapping the 5:06 live cut could queue the 4:08 compilation cut -- and
        # price it by THAT one's length. A caller may now say which one it means.
        # Both are OPTIONAL HINTS and they only ever NARROW a list this station just built from its
        # own index: a caller still cannot name a file, reach outside the library, or widen anything.
        # Send neither and the behaviour is exactly what it was -- first exact name match.
        want_album = (qs.get("album") or [""])[0].strip().lower()[:200]
        # except Exception, not (TypeError, ValueError): int(float("inf")) raises OverflowError, and
        # /api/play is PUBLIC -- an uncaught one there drops the connection with no HTTP answer at all
        # and writes a traceback to the journal. A hint parser has no business being fussy.
        try:
            want_dur = int(float((qs.get("duration_s") or ["0"])[0]))
        except Exception:
            want_dur = 0
        if want_dur < 0 or want_dur > 86400:
            want_dur = 0
        track = None
        def _fits(t):
            if want_album and (t.album or "").strip().lower() != want_album:
                return False
            # two seconds of slack: the catalogue rounds, the decoder does not
            if want_dur > 0 and abs(_int0(t.duration_s) - want_dur) > 2:
                return False
            return True
        if named and (want_album or want_dur > 0):
            track = next((t for t in named if _fits(t)), None)
            if track is None:
                # nr_reach: the hints named a row that is not in this 24-row window. Since the bot
                # can page past row 24, that is a row somebody is LOOKING AT -- so go and find it in
                # the index rather than queue whatever came first and charge for that.
                track = _exact_hit(local, artist, title, _fits)
        if track is None:
            # no hints, or the hints matched nothing anywhere: the old behaviour, and never a 404
            # we did not have before
            track = named[0] if named else None
        if not track:
            return self._json(404, {"error": "Track not found in library"})

        # GHOST GUARD (2026-08-14): the index can point at files a dedupe/move
        # has taken away (measured ~14-17% on ml4/ml5, 100% on ml3 that day).
        # Two listeners paid for silence before this existed. NEVER mint an
        # invoice for a path that is not on disk at this exact moment. The
        # search dedupe collapses same-name twins, so hunt a LIVING twin in
        # the full index before giving up.
        if not track.local_path or not os.path.exists(track.local_path):
            twin = None
            try:
                _con = sqlite3.connect(chain.by_name["local"].db_path)
                rows = _con.execute(
                    "SELECT path, COALESCE(album, ''), COALESCE(duration_s, 0) FROM tracks"
                    " WHERE lower(artist)=? AND lower(title)=?",
                    (artist.lower(), title.lower())).fetchall()
                _con.close()
                # nr_version (2026-09-10): hunting a twin by NAME ALONE can hand back a different
                # RECORDING. Prefer one that matches the row we actually resolved -- same album,
                # and a length within the same 2 seconds the picker above uses. Falling back to the
                # NEAREST length keeps the old "never refuse if something lives" behaviour while
                # making the substitute the closest recording still on the disks.
                _living = [r for r in rows if r[0] and os.path.exists(r[0])]
                _ta = (getattr(track, "album", "") or "").strip().lower()
                _td = _int0(getattr(track, "duration_s", 0))
                twin = next((r[0] for r in _living
                             if (not _ta or (r[1] or "").strip().lower() == _ta)
                             and (_td <= 0 or abs(_int0(r[2]) - _td) <= 2)), None)
                if twin is None and _living:
                    # "nearest length" is meaningless when we do not KNOW the length: sorting by
                    # abs(len - 0) hands back the SHORTEST file on the shelf -- a 12-second fragment
                    # instead of the song. With no length, keep the old first-living behaviour.
                    twin = (sorted(_living, key=lambda r: abs(_int0(r[2]) - _td))[0][0]
                            if _td > 0 else _living[0][0])
            except Exception:
                twin = None
            if twin:
                track.local_path = twin
            else:
                return self._json(404, {
                    "error": "That one has slipped off the shelf -- the file is "
                             "missing from the library disks. You have not been "
                             "charged."})

        price = max(int(xrec.get("sats") or 0), _play_price(track.duration_s)) if xrec else _play_price(track.duration_s)   # nr_xlane: never below the site price
        try:
            invoice = _create_play_invoice(memo=f"Zap: {track.display()}", sats=price)
        except Exception as e:
            return self._json(502, {"error": f"Could not create invoice: {e}"})

        if not invoice.get("payment_hash") or not invoice.get("bolt11"):
            return self._json(502, {"error": "LNbits returned an unexpected invoice response"})

        # nr_full: granted here, once, on proof of caller. A website visitor is always proxied.
        full_note = bool(self._from_the_box()
                         and (qs.get("full") or [""])[0].strip().lower() in ("1", "true", "yes", "on"))
        pending = {
            "artist": track.artist,
            "title": track.title,
            "local_path": track.local_path,
            "rights_class": track.rights_class.value,
            "source": track.source,
            "queued": False,
            "note": _clean_note((qs.get("note") or [""])[0], full_note) if _notes_on() else "",
            "who": _clean_who((qs.get("who") or [""])[0]) if _notes_on() else "",
            # nr_at: the DOOR is proof-of-caller too, never the caller's word. /api/play is public -- the
            # website posts to it -- so without this gate anyone could send via=tg and buy a live Telegram
            # mention of a stranger for 21 sats, which is the exact harm this whole change exists to
            # prevent. Both real callers are on loopback and keep their stamp.
            "via": (_clean_via((qs.get("via") or [""])[0]) if self._from_the_box() else "web"),
            "full": full_note,
            "note_token": _secrets.token_hex(16),
        }
        pending["minted_at"] = time.time()
        if xrec:
            _xlane_stamp(pending, xrec, xcode)              # nr_xlane
        rds.setex(_pending_play_key(invoice["payment_hash"]), PENDING_RECORD_TTL_S, json.dumps(pending))

        reply = {
            "payment_hash": invoice["payment_hash"],
            "bolt11": invoice["bolt11"],
            "sats": price,
            "notes_enabled": _notes_on(), "note_token": pending["note_token"], "note_max": NOTE_MAX,
            "duration_s": track.duration_s,
            "message": f"Zap {price} sats to queue '{track.display()}'",
        }
        if xrec:
            reply = _xlane_reply(reply)
            try:
                (_xcode_extra if xrec.get("_extra") else _xcode_bind)(xcode, xrec, pending, invoice, reply)
            except Exception:
                pass                                       # the invoice and its record exist; the reply must still go out
            _xcode_release(xcode)
        self._json(200, reply)

    def _handle_donate(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        try:
            amount = int((qs.get("amount") or ["0"])[0])
        except ValueError:
            amount = 0
        if amount < 1:
            return self._json(400, {"error": "Enter an amount of at least 1 sat"})
        name = (qs.get("name") or [""])[0].strip().replace("\n", " ").replace("\r", " ")[:40]
        message = (qs.get("message") or [""])[0].strip().replace("\n", " ").replace("\r", " ")[:100]
        target = (qs.get("target") or [""])[0].strip().lower()
        if target not in ("", "live", "station"):
            return self._json(400, {"error": "target must be 'live' or 'station'"})
        if target == "live":
            if not LNBITS_STAGE_ADMIN_KEY:
                return self._json(500, {"error": "Live split not configured (LNBITS_STAGE_ADMIN_KEY missing)"})
            if not _stage_live():
                # the site greys the button out, but never trust the button
                return self._json(409, {"error": "No live session right now -- that pot is closed. Support the ship instead?"})
        if not LNBITS_DONATE_INVOICE_KEY:
            return self._json(500, {"error": "Donations not configured (LNBITS_DONATE_INVOICE_KEY missing)"})
        try:
            invoice = _create_donate_invoice(amount, name, message, target)
        except Exception as e:
            return self._json(502, {"error": f"Could not create invoice: {e}"})
        if not invoice.get("payment_hash") or not invoice.get("bolt11"):
            return self._json(502, {"error": "LNbits returned an unexpected invoice response"})
        self._json(200, {"payment_hash": invoice["payment_hash"], "bolt11": invoice["bolt11"], "sats": amount})

    def _handle_donate_status(self, parsed):
        qs = parse_qs(parsed.query)
        payment_hash = (qs.get("payment_hash") or [""])[0].strip()
        if not payment_hash:
            return self._json(400, {"error": "missing payment_hash"})
        try:
            paid = _is_donate_invoice_paid(payment_hash)
        except Exception as e:
            return self._json(502, {"error": f"Could not reach LNbits: {e}"})
        self._json(200, {"paid": paid})

    def _handle_donate_total(self):
        try:
            total = _donate_total_sats()
        except Exception as e:
            return self._json(502, {"error": f"Could not reach LNbits: {e}"})
        # lifetime paid straight to DJs: pre-logging history (SF-confirmed seed)
        # + every settlement recorded since 2026-07-24
        dj_paid = DJ_PAID_SEED_SATS
        try:
            for raw in rds.lrange("livesession:settlements", 0, -1):
                rec = json.loads(raw)
                dj_paid += int(rec.get("per_dj_sats", 0)) * len(rec.get("paid", []))
        except Exception:
            pass
        # lifetime sats forwarded straight to OG artists (the Wavlake lane):
        # seed + every SENT / SETTLED-BY-HAND row in the payout ledger. The
        # ledger is the royalty record -- this number is measured, never typed.
        og_sats, og_plays = WAVLAKE_OG_SEED_SATS, WAVLAKE_OG_SEED_PLAYS
        try:
            with open(WAVLAKE_PAYOUT_LOG, encoding="utf-8") as fh:
                for line in fh:
                    cols = line.rstrip("\n").split("\t")
                    if len(cols) >= 3 and cols[1] in ("SENT", "SETTLED-BY-HAND"):
                        og_sats += int(cols[2])
                        og_plays += 1
        except Exception:
            pass
        self._json(200, {"total_sats": total, "dj_paid_sats": dj_paid,
                         "og_paid_sats": og_sats, "og_plays": og_plays})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=_sweep_paid_plays, daemon=True).start()
    threading.Thread(target=_mood_feeder, daemon=True).start()     # nr_mood (2026-09-05)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"library_api listening on 127.0.0.1:{PORT}")
    server.serve_forever()
