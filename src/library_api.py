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

import functools
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
    # nr_bestcopy (2026-10-14, SF: "update to best quality files"): the first row that fits names the
    # RECORDING; among its copies (same rights class, length within 3 s, still fitting the hints) the
    # best FILE is handed out -- lossless first, then the higher bitrate. Same song, same price.
    first = None
    for r in rows:
        try:
            t = local._row_to_track(r)
        except Exception:
            continue
        if fits(t):
            first = (r, t)
            break
    if first is None:
        return None
    try:
        from resolvers.local import _quality, _rights, _dur, DUR_TOL_S
        (best_r, best_t), d0 = first, _dur(first[0])
        for r in rows:
            d = _dur(r)
            if _rights(r) != _rights(first[0]) or (d is None) != (d0 is None):
                continue
            if d is not None and abs(d - d0) > DUR_TOL_S:
                continue
            if _quality(r) > _quality(best_r):
                t = local._row_to_track(r)
                if fits(t):
                    best_r, best_t = r, t
        return best_t
    except Exception:
        return first[1]        # the money path must not die because a ranking failed


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
# 210 sats for now, NEVER into the rotation. nr_wavcache (2026-10-24, SF: "keep that copy and store it so we don't have
# to re-download the same songs all the time"): a paid song is kept in the cache below and reused for the next paid
# play -- fetched on demand, never bulk-mirrored (Wavlake's one request). Unplayed for 90 days -> gone; gone on Wavlake
# (asked before every invoice) -> our copy goes at once. Never in the library, never in the rotation.
WAVLAKE_PLAY_PRICE_SATS = int(os.environ.get("WAVLAKE_PLAY_PRICE_SATS", "210"))
WAVLAKE_SPOOL_DIR = "/var/lib/jukebox/wavlake-cache"
WAVLAKE_CACHE_KEEP_S = 90 * 86400
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
                                    "/srv/jukebox/app/wavlake-payouts.tsv")
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
# nr_freelane (2026-10-23, SF: the "CC & public domain" lane): the free songs live in their OWN index, so no library
# search, no BUMA tool and no nightly scan ever sees them, and a library request can never land on a free copy (or the
# other way round). A row is looked up in free.db ONLY when it says source=free. Nothing here touches the rotation.
FREE_DB = "/var/lib/jukebox/free.db"
free_local = LocalResolver(FREE_DB)
def _lane_local(source):
    return free_local if str(source or "").strip().lower() == "free" else chain.by_name["local"]

# nr_catalog (2026-10-24, a listener's request in the Telegram group, SF: "a catalog, for the entire library, so folks can
# peruse more easily"): "Browse the ship". Read-only: GET /api/browse/{artists,artist,recent,album}?lane=library|free.
# The artist list is FOLDED on collaboration markers only: "Joachim Garraud feat. Poet Name Life" joins "Joachim Garraud"
# when that name also exists on its own. Never on "&" or "and": tested on the library, "Simon & Garfunkel" folded under an
# artist called "SIMON" -- "&" is usually part of a band's name. Every credit stays as it is in the rows.
# Rows of one recording (same artist, title and album, length within 3 s: the jukebox's own rule) show once.
_CAT = {}
_CAT_LOCK = threading.Lock()
_CAT_SEP = re.compile(r"\s*[,(\[]?\s*\b(?:feat\.?|ft\.?|featuring|vs\.?)\s+|\s+(?:x|with)\s+|\s*;\s*|\s+\(", re.I)   # nr_catfold: + ";" and " ("
_CAT_SEP_SMALL = re.compile(r"\s*[/,]\s*")   # nr_catfold: "/" and "," only for names with 3 songs or fewer (never AC/DC)
_CAT_TTL = 600
_CAT_MAX_TRACKS = 800


def _cat_db(lane):
    return FREE_DB if lane == "free" else DB_PATH


def _cat_key(s):
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _cat_fold(s):
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def _cat_find(idx, q):
    """nr_catfind (2026-10-31, SF: the catalog filter and the jukebox search gave different results): the artists whose
    name (or one of its spellings) holds the typed text -- as before, these come first -- then the names that hold every
    typed WORD in any order with spaces and punctuation ignored: "maiden iron" finds Iron Maiden, "ac dc" finds AC/DC."""
    first = [k for k in idx["order"] if q in k or any(q in _cat_key(v) for v in idx["groups"][k]["variants"])]
    toks = [_cat_fold(t) for t in q.split() if _cat_fold(t)]
    whole = _cat_fold(q)
    if not toks:
        return first
    fold = idx.get("_fold")
    if fold is None:                              # once per index build: every name and spelling, folded
        fold = idx["_fold"] = {k: "|".join([_cat_fold(k)] + [_cat_fold(v) for v in idx["groups"][k]["variants"]])
                               for k in idx["order"]}
    seen = set(first)
    more = [k for k in idx["order"] if k not in seen and (whole in fold[k] or all(t in fold[k] for t in toks))]
    return first + more


# nr_catclean (2026-10-24): placeholder artist tags fold into ONE "Unknown artist", compilation spellings into ONE
# "Various artists" (SF: "some weirdness" -- artists called "Track 1" .. "Track 9"). Keys are already lower-cased.
_CAT_UNKNOWN = re.compile(r"^(?:unknown(?: artist)?|<unknown>|\[unknown\]|\(unknown\)|artist|no artist|n/?a|none|-+|\?+|_+|\.+|"
                          r"track ?\d*|track ?\d+ ?- ?.*|cd ?\d+|disc ?\d+|\d{1,2}|untitled|audiotrack|unknown artist \d+|"
                          r"(?:https?://|www\.)\S+|\S+\.(?:com|org|net|ru|nl|info|biz)|a|b|aa|"
                          r"[\[(][^\])]{0,24}\d[^\])]{0,24}[\])]\s*(?:[a-d]?\d{0,2})?)$")   # nr_catfold: "(01)", "[wonka 1012] b2"
_CAT_VARIOUS = re.compile(r"^(?:va|v/a|v\\a|v\.a\.?|various|various artists?|diverse|div\.?|compilation|varios|vários|verschiedene)$")


def _cat_norm(a):
    """(key, display name) of an artist tag: placeholders and compilations fold into one entry each"""
    k = _cat_key(a)
    if _CAT_UNKNOWN.match(k):
        return "unknown artist", "Unknown artist"
    if _CAT_VARIOUS.match(k):
        return "various artists", "Various artists"
    return k, (a or "").strip()


def _cat_own(idx, a):
    """nr_catthe: the group key a tag belongs to as the artist's OWN ("Specials" is The Specials' own, not a guest)"""
    nk = _cat_norm(a)[0]
    return idx.get("same", {}).get(nk, nk)


def _cat_letter(name):
    c = (name or "").strip()[:1].upper()
    return c if "A" <= c <= "Z" else "#"


_CAT_BUILDING = set()   # nr_catcount: lanes whose index is being rebuilt in the background


def _cat_index(lane):
    """The folded artist list of one lane, cached 10 minutes.
    nr_catcount (2026-10-29): an expired list is rebuilt in the background while visitors keep the old one."""
    now = time.time()
    with _CAT_LOCK:
        c = _CAT.get(("idx", lane))
        if c and now - c["t"] < _CAT_TTL:
            return c
        if c:
            if lane not in _CAT_BUILDING:
                _CAT_BUILDING.add(lane)
                threading.Thread(target=_cat_index_build, args=(lane,), daemon=True).start()
            return c
    return _cat_index_build(lane)


def _cat_counts(lane):
    """nr_catcount (2026-10-29, SF: list "U Roy 216", pane "199 songs"): songs per artist name, counted the way the
    pane shows them -- one per recording (_cat_dedupe: same artist + title + album, lengths within 3 s)."""
    con = sqlite3.connect(f"file:{_cat_db(lane)}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT artist, title, COALESCE(album, ''), COALESCE(duration_s, 0) FROM tracks "
                          "WHERE artist IS NOT NULL AND trim(artist) != '' AND title IS NOT NULL AND trim(title) != ''")
        seen, n = {}, {}
        for ar, ti, al, d in cur:
            k = (_cat_key(ar), _cat_key(ti), _cat_key(al))
            d = int(d or 0)
            ds = seen.setdefault(k, [])
            if any(abs(x - d) <= 3 for x in ds):
                continue
            ds.append(d)
            n[ar] = n.get(ar, 0) + 1
    finally:
        con.close()
    return list(n.items())


def _cat_index_build(lane):
    now = time.time()
    try:
        rows = _cat_counts(lane)
    except Exception:
        with _CAT_LOCK:
            _CAT_BUILDING.discard(lane)
        raise
    exact = {}
    for a, n in rows:
        nk, nm = _cat_norm(a)                                  # nr_catclean
        e = exact.setdefault(nk, {"name": nm, "n": 0, "variants": []})
        e["n"] += n
        e["variants"].append(a)
    # nr_catthe: "The Specials" = "Specials" = "Specials, The" (plain names only): the spelling with the most songs
    # leads, the others fold into it silently -- and so does everything that folds under them ("Beatles With ...")
    bases = {}
    for k, e in exact.items():
        if len(_CAT_SEP.split(e["name"], maxsplit=1)) < 2:
            b = k[4:] if k.startswith("the ") else (k[:-5] if k.endswith(", the") else k)
            bases.setdefault(b, []).append(k)
    same = {}
    for b, ks in bases.items():
        if len(ks) > 1:
            lead = max(ks, key=lambda x: (exact[x]["n"], x.startswith("the "), x))
            same.update((x, lead) for x in ks if x != lead)
    groups = {}
    for k, e in exact.items():
        parts = _CAT_SEP.split(e["name"], maxsplit=1)
        if len(parts) < 2 and e["n"] <= 3:                     # nr_catfold
            parts = _CAT_SEP_SMALL.split(e["name"], maxsplit=1)
        main = _cat_key(parts[0]) if len(parts) > 1 else k
        target = main if (main != k and main in exact) else k
        target = same.get(target, target)                    # nr_catthe
        g = groups.setdefault(target, {"name": exact[target]["name"], "n": 0, "variants": [], "others": 0})
        g["n"] += e["n"]
        g["variants"] += e["variants"]
        if target != k and same.get(k) != target:
            g["others"] += 1
    order = sorted(groups, key=lambda k: (groups[k]["name"].casefold(), k))
    letters = {}
    for k in order:
        letters[_cat_letter(groups[k]["name"])] = letters.get(_cat_letter(groups[k]["name"]), 0) + 1
    out = {"t": now, "groups": groups, "order": order, "letters": letters, "same": same}   # nr_catthe
    with _CAT_LOCK:
        _CAT[("idx", lane)] = out
        _CAT_BUILDING.discard(lane)
    return out


def _cat_row(lane, a, t, al, d, path=""):
    return {"artist": a or "", "title": t or "", "album": al or "", "duration_s": int(d or 0),
            "sats": _play_price(d), "source": "free" if lane == "free" else "library", "genre": _cat_genre(path),
            "label": _cat_label(path), "disc": _disc_url(path), "cover": _cover_url(path)}   # nr_catgenre; nr_labels; nr_covers


# nr_labels (2026-10-29, SF: "built it all"): a Great 78 record shows its OWN label. The Great 78 Project photographed
# every record it transferred; archive.org keeps <id>_thumb.jpg (small) and <id>_itemimage.jpg (the whole record).
# Rule: Great 78 records dated <= 1955 (label designs past 70 years; a straight photo of a flat label adds no new right in
# the EU). Photos are fetched ONCE, only for identifiers in our own manifest, and kept on the music disk; visitors never
# talk to archive.org.
_DISC_DIR = "/srv/music/free_music/labels"
_DISC_MANIFEST = "/srv/music/free_music/originals/great78/manifest.tsv"
_DISC_PREFIX = "/srv/music/free_music/station/great78/"
_DISC_MAX_YEAR = 1955
_DISC_ID = re.compile(r"^78_[A-Za-z0-9._-]{1,200}$")
_DISC = {"map": None, "ids": frozenset(), "t": 0.0}
_DISC_LOCK = threading.Lock()
_DISC_GATE = threading.BoundedSemaphore(3)          # at most 3 fetches from archive.org at a time


def _disc_map():
    """station file name -> Great 78 identifier, for records dated <= 1955 (read once; a missing manifest retries in 10 min)"""
    m = _DISC["map"]
    if m is not None and (m or time.time() - _DISC["t"] < 600):
        return m
    with _DISC_LOCK:
        m = _DISC["map"]
        if m is not None and (m or time.time() - _DISC["t"] < 600):
            return m
        files = {}
        try:
            with open(_DISC_MANIFEST, encoding="utf-8", errors="replace") as fh:
                next(fh, None)
                for line in fh:
                    c = line.rstrip("\n").split("\t")
                    if len(c) < 5:
                        continue
                    y = c[1][:4]
                    if y.isdigit() and 1880 <= int(y) <= _DISC_MAX_YEAR and _DISC_ID.match(c[0]) and c[4]:
                        files[c[4]] = c[0]
        except OSError as e:
            logging.warning("nr_labels: no Great 78 manifest (%s)", e)
        _DISC.update(map=files, ids=frozenset(files.values()), t=time.time())
        return files


def _disc_url(path):
    """the label photo's address for a song, or "" (only Great 78 records <= 1955 have one)"""
    if not path or not path.startswith(_DISC_PREFIX):
        return ""
    ident = _disc_map().get(path[len(_DISC_PREFIX):])
    return ("/api/label/" + ident) if ident else ""


def _disc_fetch(ident, size, fn):
    """fetch one label photo from archive.org, shrink it, keep it. False = not now (a failure waits a day)."""
    bad = fn[:-4] + ".bad"
    try:
        if time.time() - os.path.getmtime(bad) < 86400:
            return False
    except OSError:
        pass
    if not _DISC_GATE.acquire(timeout=20):
        return False
    try:
        if os.path.exists(fn):
            return True
        import io
        from PIL import Image
        name = ident + ("_thumb.jpg" if size == "t" else "_itemimage.jpg")
        r = requests.get("https://archive.org/download/%s/%s" % (ident, name), timeout=(8, 25), stream=True,
                         headers={"User-Agent": "NoderunnersRadio/1.0 (record-label cache; https://noderunnersradio.com)"})
        if r.status_code != 200:
            raise ValueError("archive.org answered %s" % r.status_code)
        data = bytearray()
        for ch in r.iter_content(65536):
            data += ch
            if len(data) > 8000000:
                raise ValueError("picture too big")
        with Image.open(io.BytesIO(bytes(data))) as im:
            im = im.convert("RGB")
            px = 96 if size == "t" else 400
            im.thumbnail((px, px), Image.LANCZOS)
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            tmp = fn + ".part"
            im.save(tmp, "JPEG", quality=82, optimize=True)
        os.replace(tmp, fn)
        return True
    except Exception as e:
        logging.warning("nr_labels: %s %s: %s", ident, size, str(e)[:160])
        try:
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            open(bad, "w").close()
        except OSError:
            pass
        return False
    finally:
        _DISC_GATE.release()


# nr_covers (2026-10-29, SF: "show it anyway, if we get takedown requests we'll take it down.. but I think most visual
# artist would be glad to see their work"): cover art. Wavlake's album art, and the ship library's own covers (the album
# folder's cover file, else the picture inside the song file, else the folder's likeliest front). Cut ONCE into a cache
# on the music disk; visitors never talk to Wavlake's image host. Great 78 records keep their round label (nr_labels).
# Removal (never on the site): touch <_COVER_DIR>/m/<shard>/<key>.blocked -> never served again.
_COVER_DIR = "/srv/music/free_music/covers"
_COVER_ROOT = tuple("/srv/music_library%s/" % n for n in ("", "_2", "_3", "_4", "_5", "_6"))
_COVER_KEY = re.compile(r"^(w[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|l[0-9a-f]{24})$")
_WL_TRACK = re.compile(r"cloudfront\.net/track/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.")
_WL_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_WL_ART = re.compile(r"^https://d12wklypp119aj\.cloudfront\.net/image/[0-9a-fA-F-]{36}\.(?:jpe?g|png|webp)$")
_COVER_IMG = (".jpg", ".jpeg", ".png", ".webp")
_COVER_FRONT = re.compile(r"(?i)front|cover|^folder|^albumart")
_COVER_SKIP = re.compile(r"(?i)(?<![a-z])(back|inlay|inside|tray|proof|booklet|matrix|rear|spine|disc|label|side|cd ?\d)(?![a-z])")
_COVER_UA = {"User-Agent": "NoderunnersRadio/1.0 (cover cache; https://noderunnersradio.com)"}
_COVER_KEYS = {}                                     # key -> path, only for addresses this API handed out
_COVER_LOCK = threading.Lock()
_COVER_GATE = threading.BoundedSemaphore(3)          # at most 3 covers cut at a time


def _cover_url(path):
    """the cover's address for a song, or "" (Great 78 records have their round label instead)"""
    if not path:
        return ""
    m = _WL_TRACK.search(path)
    if m:
        return "/api/art/w" + m.group(1).lower()
    if not path.startswith(_COVER_ROOT):
        return ""
    k = "l" + hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:24]
    with _COVER_LOCK:
        if k not in _COVER_KEYS:
            if len(_COVER_KEYS) >= 300000:
                for old in list(_COVER_KEYS)[:50000]:
                    del _COVER_KEYS[old]
            _COVER_KEYS[k] = path
    return "/api/art/" + k


def _cover_files(key):
    return (os.path.join(_COVER_DIR, "t", key[1:3], key + ".jpg"), os.path.join(_COVER_DIR, "m", key[1:3], key + ".jpg"))


def _cover_download(url, limit=12000000):
    r = requests.get(url, timeout=(6, 20), stream=True, headers=_COVER_UA)
    if r.status_code != 200:
        raise ValueError("%s answered %s" % (url.split("/")[2], r.status_code))
    data = bytearray()
    for ch in r.iter_content(65536):
        data += ch
        if len(data) > limit:
            raise ValueError("picture too big")
    return bytes(data)


def _cover_wavlake(tid):
    r = requests.get("https://wavlake.com/api/v1/content/track/" + tid, timeout=(6, 12), headers=_COVER_UA)
    if r.status_code != 200:
        raise ValueError("wavlake answered %s" % r.status_code)
    d = r.json()
    if isinstance(d, list):
        d = d[0] if d else {}
    u = (d.get("albumArtUrl") or "") if isinstance(d, dict) else ""
    return _cover_download(u) if _WL_ART.match(u) else None


def _cover_embedded(path):
    try:
        r = subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0", "-frames:v", "1",
                            "-f", "image2pipe", "-c:v", "png", "-"], stdin=subprocess.DEVNULL, capture_output=True, timeout=25)
        return r.stdout if r.returncode == 0 and r.stdout[:4] == b"\x89PNG" else None
    except Exception:
        return None


def _cover_local(path):
    """the best picture for a ship-library song, or None. Only songs the library knows."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if con.execute("SELECT 1 FROM tracks WHERE path = ?", (path,)).fetchone() is None:
            return None
    finally:
        con.close()
    d = os.path.dirname(path)
    imgs = []
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.lower().endswith(_COVER_IMG) and e.is_file():
                    sz = e.stat().st_size
                    if 0 < sz <= 20000000:
                        imgs.append((e.name, sz))
    except OSError:
        pass
    def read(n):
        with open(os.path.join(d, n), "rb") as fh:
            return fh.read()
    stem = lambda n: os.path.splitext(n)[0]
    front = sorted((x for x in imgs if _COVER_FRONT.search(stem(x[0])) and not _COVER_SKIP.search(stem(x[0]))), key=lambda x: -x[1])
    if front:
        return read(front[0][0])
    emb = _cover_embedded(path)
    if emb:
        return emb
    rest = sorted((x for x in imgs if not _COVER_SKIP.search(stem(x[0]))), key=lambda x: (not x[0].startswith("00"), -x[1]))
    return read(rest[0][0]) if rest else None


def _cover_make(key):
    """cut both sizes of one cover. True = there; False = none (or not now)."""
    t, m = _cover_files(key)
    none = m[:-4] + ".none"
    try:
        if time.time() - os.path.getmtime(none) < (86400 if key[0] == "w" else 7 * 86400):
            return False
    except OSError:
        pass
    path = _COVER_KEYS.get(key) if key[0] == "l" else ""
    if key[0] == "l" and not path:
        return False                                  # an address this API never handed out (or one from before a restart)
    if not _COVER_GATE.acquire(timeout=25):
        return False
    try:
        if os.path.exists(t) and os.path.exists(m):
            return True
        data = _cover_wavlake(key[1:]) if key[0] == "w" else _cover_local(path)
        if not data:
            raise LookupError("no picture")
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
        for fn, px in ((m, 400), (t, 96)):
            c = im.copy()
            c.thumbnail((px, px), Image.LANCZOS)
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            c.save(fn + ".part", "JPEG", quality=82, optimize=True)
            os.replace(fn + ".part", fn)
        return True
    except Exception as e:
        if not isinstance(e, LookupError):
            logging.warning("nr_covers: %s: %s", key, str(e)[:160])
        try:
            os.makedirs(os.path.dirname(none), exist_ok=True)
            open(none, "w").close()
        except OSError:
            pass
        return False
    finally:
        _COVER_GATE.release()


# nr_catgenre (2026-10-25, SF: "Can we add genre hints"): the folder a person sorted a song into names its genre.
_GENRE_TOP = {'Retro House': 'Retro house · Trance', 'Reggae and Dub': 'Reggae · Ska · Dub'}   # EXAMPLE -- your own top-level genre folders, mapped to a short genre hint
_GENRE_OWN = {'electronic': 'Electronic', 'rock': 'Rock · Metal · Indie'}   # EXAMPLE -- the genre folders inside your own collection
_GENRE_MIXED = ['Unsorted', 'Compilations']   # EXAMPLE -- folders that hold mixed genres
_GENRE_KEYS = [(re.compile(k, re.I), v) for k, v in [('happy hardcore', 'Happy hardcore'), ('hardstyle', 'Hardstyle'), ('apres ski|schlager|limburgs', 'Après-ski · Schlager'), ('italo disco', 'Italo disco'), ('dubstep', 'Dubstep'), ('drum ?(and|&|n) ?bass|jungle', 'Drum & bass'), ('deep ?house', 'Deep house'), ('house', 'House'), ('techno', 'Techno'), ('trance', 'Trance'), ('hardcore', 'Hardcore'), ('reggae|dancehall', 'Reggae'), ('hip ?hop|rap\\b', 'Hip-hop'), ('jazz', 'Jazz'), ('blues', 'Blues'), ('punk', 'Punk'), ('metal', 'Metal'), ('top 40|hitzone|hit explosion|\\bpop\\b', 'Pop'), ('\\brock\\b', 'Rock'), ('dance', 'Dance'), ('electronic', 'Electronic'), ('ambient|chill', 'Chillout'), ("50's|60's|70's|80's", 'Oldies')]]


@functools.lru_cache(maxsize=200000)
def _cat_genre(path):
    """a short genre hint from the folder a song sits in, or "" (better none than a wrong one)"""
    parts = (path or "").split("/")
    if len(parts) < 5:
        return ""
    if parts[2] == "music":                                     # the free lane: /srv/music/free_music/station/<set>/...
        s = parts[5] if len(parts) > 6 else ""
        return {"78s": "78 rpm", "great78": "78 rpm", "cylinders": "Cylinder"}.get(s, "")
    top = parts[3]
    if top in _GENRE_TOP:
        return _GENRE_TOP[top]
    if top == "Own Collection digitized" and len(parts) > 5:
        return _GENRE_OWN.get(parts[4], "")
    if top in _GENRE_MIXED and len(parts) > 5:
        for lvl in parts[4:6]:
            if lvl == parts[-1]:
                break
            for rx, g in _GENRE_KEYS:
                if rx.search(lvl):
                    return g
    return ""


# nr_catgenre (SF: "We can do the same for label info where available"): the folder right after a "Labels" folder
_LABEL_DIR = re.compile(r'(?i)\blabels?\b')   # EXAMPLE -- a folder that holds one folder per record label
_LABEL_TOP = {'Warp Records': 'Warp Records'}   # EXAMPLE -- top-level folders that ARE a label
_LABEL_TRIM = re.compile('(?i)==+|=-+|-=+|\\[!\\]|\\[\\+\\]|\\[label\\]|\\(incomplete\\)|\\[flac\\]|\\+ ?all sublabels|\\[320\\]|\\(complete\\)')
_LABEL_JUNK = re.compile('(?i)label|discography|^test$|net-releases|^r$|unsorted|various|^va\\b|^(cd|disc) ?\\d|\\b(19|20)\\d\\d\\b| - |\\([^)]*$|\\]')


@functools.lru_cache(maxsize=200000)
def _cat_label(path):
    """the record label from the folder a song sits in, or an empty string"""
    parts = (path or "").split("/")
    if len(parts) < 5:
        return ""
    if parts[3] in _LABEL_TOP:
        return _LABEL_TOP[parts[3]]
    for i in range(3, min(len(parts) - 2, 6)):
        # a shelf OF labels ends in "Label(s)" (for example "Labels" or "Record labels");
        # "Trojan Label - Bob Marley - 1973" is a release, not a shelf
        shelf = re.sub(r"(?i)\([^)]*\)|\bincompl?ete\b|\bincoplete\b", "", parts[i]).strip(" ]=-[")
        if _LABEL_DIR.search(shelf) and re.search(r"(?i)labels?$", shelf):
            name = _LABEL_TRIM.sub(" ", parts[i + 1])
            name = re.sub(r"\s+", " ", name).strip(" -=+[]()!")
            if len(name) < 2 or _LABEL_JUNK.search(name):
                return ""
            return name
    return ""


def _cat_dedupe(rows):
    """one row per recording: same artist + title + album, lengths within 3 s"""
    seen, out = {}, []
    for r in rows:                                           # nr_catgenre: rows may carry the path as a 5th field
        a, t, al, d = r[:4]
        k = (_cat_key(a), _cat_key(t), _cat_key(al))
        d = int(d or 0)
        if any(abs(x - d) <= 3 for x in seen.get(k, [])):
            continue
        seen.setdefault(k, []).append(d)
        out.append((a, t, al, d) + tuple(r[4:5]))
    return out


def _cat_index_all(lanes):
    """nr_allcat: one artist list over several lanes (library + the free lane), merged by name, cached 10 minutes."""
    lanes = tuple(l for l in lanes if l in ("library", "free")) or ("library",)
    now = time.time()
    with _CAT_LOCK:
        c = _CAT.get(("all", lanes))
        if c and now - c["t"] < _CAT_TTL:
            return c
    groups = {}
    for lane in lanes:
        for k, g in _cat_index(lane)["groups"].items():
            m = groups.setdefault(k, {"name": g["name"], "n": 0, "variants": [], "others": 0})
            m["n"] += g["n"]
            m["variants"] += g["variants"]
            m["others"] += g["others"]
    order = sorted(groups, key=lambda k: (groups[k]["name"].casefold(), k))
    letters = {}
    for k in order:
        letters[_cat_letter(groups[k]["name"])] = letters.get(_cat_letter(groups[k]["name"]), 0) + 1
    out = {"t": now, "groups": groups, "order": order, "letters": letters}
    with _CAT_LOCK:
        _CAT[("all", lanes)] = out
    return out


def _cat_artist_rows(lane, g):
    """one lane's rows for a (folded) artist group, one row per recording"""
    names = list(dict.fromkeys(g["variants"]))[:900]
    con = sqlite3.connect(f"file:{_cat_db(lane)}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT artist, title, COALESCE(album, ''), COALESCE(duration_s, 0), path FROM tracks "
                           "WHERE artist IN (%s) AND title IS NOT NULL AND trim(title) != '' "
                           "ORDER BY COALESCE(album, ''), path LIMIT %d" % (",".join("?" * len(names)), _CAT_MAX_TRACKS * 3),
                           names).fetchall()
    finally:
        con.close()
    return _cat_dedupe(rows)[:_CAT_MAX_TRACKS]


def _cat_loose(idx, lanes, letter, off):
    """nr_catbox2: the songs of one letter's one-song names (its "needs sorting" box), 200 names a page, A-Z by artist"""
    keys = [k for k in idx["order"] if idx["groups"][k]["n"] <= 1 and _cat_letter(idx["groups"][k]["name"]) == letter]
    page = keys[off:off + 200]
    tracks = []
    for lane in lanes:
        li = _cat_index(lane)["groups"]
        names = list(dict.fromkeys(v for k in page if k in li for v in li[k]["variants"]))
        if not names:
            continue
        con = sqlite3.connect(f"file:{_cat_db(lane)}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT artist, title, COALESCE(album, ''), COALESCE(duration_s, 0), path FROM tracks "
                               "WHERE artist IN (%s) AND title IS NOT NULL AND trim(title) != ''" % ",".join("?" * len(names)),
                               names).fetchall()
        finally:
            con.close()
        tracks += [_cat_row(lane, *r) for r in _cat_dedupe(rows)]
    tracks.sort(key=lambda t: (t["artist"].casefold(), t["title"].casefold()))
    return {"total": len(keys), "offset": off, "next": off + len(page), "tracks": tracks}


def _cat_recent(lane):
    """the newest albums (newest row first), cached 10 minutes: the GROUP BY over albums takes ~1.2 s"""
    now = time.time()
    with _CAT_LOCK:
        c = _CAT.get(("recent", lane))
        if c and now - c["t"] < _CAT_TTL:
            return c["rows"]
    con = sqlite3.connect(f"file:{_cat_db(lane)}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT artist, COALESCE(album, ''), MAX(rowid) r, COUNT(*) FROM tracks "
                           "WHERE artist IS NOT NULL AND trim(artist) != '' GROUP BY artist, COALESCE(album, '') "
                           "ORDER BY r DESC LIMIT 300").fetchall()
    finally:
        con.close()
    rows = [{"artist": a, "album": al, "n": n} for a, al, _r, n in rows]
    with _CAT_LOCK:
        _CAT[("recent", lane)] = {"t": now, "rows": rows}
    return rows
musicbrainz = MusicBrainzResolver()

TELEGRAM_CHAT_BOT_TOKEN = os.environ.get("TELEGRAM_CHAT_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # e.g. -100…; optional filter
telegram_chat = None
if TELEGRAM_CHAT_BOT_TOKEN:
    telegram_chat = TelegramChatRelay(
        token=TELEGRAM_CHAT_BOT_TOKEN,
        chat_id=int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None,
        rds=rds,
    )
    telegram_chat.start()
_last_play_at = 0.0


_NOSTRCHAT = {"mtime": 0.0, "rows": []}


def _nostr_chat_recent(n=50):
    """nr_nostrchat (2026-10-03): the newest Nostr live-chat rows from nr-nostrchat's file, re-read only when the file
    changes. Never raises: no file = no Nostr rows."""
    p = "/var/lib/jukebox/nostrchat.json"
    try:
        mt = os.path.getmtime(p)
        if mt != _NOSTRCHAT["mtime"]:
            with open(p, encoding="utf-8") as fh:
                rows = json.load(fh).get("messages") or []
            _NOSTRCHAT["rows"] = [r for r in rows if isinstance(r, dict) and r.get("src") == "nostr"]
            _NOSTRCHAT["mtime"] = mt
    except Exception:
        pass
    return _NOSTRCHAT["rows"][-n:]


# ---- nr_nostrpost (2026-10-03): the site's chat bar -- a listener's OWN signed Nostr message, checked, then relayed ----
_NOSTRPOST_A = '30311:358225a7c2f03c1a0f096f15f5c48cd080e8e328b14389db01516d226838352f:noderunners-radio'
_NOSTRPOST_RELAYS = ["wss://nos.lol", "wss://relay.primal.net", "wss://relay.snort.social", "wss://relay.zap.stream", "wss://relay.damus.io"]
_NOSTRPOST = {"lib": None, "ann": None, "hits": {}, "lock": threading.Lock()}


def _nostrpost_libs():
    """nr-nostr's BIP-340 + relay code and nr-announcer's blocklist, loaded once (one set of rules for every bot)."""
    if _NOSTRPOST["lib"] is None:
        import importlib.util, importlib.machinery, sys as _sys
        def load(name, path):
            loader = importlib.machinery.SourceFileLoader(name, path)
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
        was, _sys.dont_write_bytecode = _sys.dont_write_bytecode, True     # never a .pyc beside /usr/local/bin files
        try:
            _NOSTRPOST["ann"] = load("nr_announcer_post", "/usr/local/bin/nr-announcer")
            _NOSTRPOST["lib"] = load("nr_nostr_post", "/usr/local/bin/nr-nostr")
        finally:
            _sys.dont_write_bytecode = was
    return _NOSTRPOST["lib"], _NOSTRPOST["ann"]


def _nostrpost_rate(key, limit, window=60):
    now = time.time()
    with _NOSTRPOST["lock"]:
        hits = [t for t in _NOSTRPOST["hits"].get(key, []) if now - t < window]
        ok = len(hits) < limit
        if ok:
            hits.append(now)
        _NOSTRPOST["hits"][key] = hits
        if len(_NOSTRPOST["hits"]) > 5000:                                # keep the table small
            for k in list(_NOSTRPOST["hits"])[:2500]:
                _NOSTRPOST["hits"].pop(k, None)
        return ok


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
        "source": (m.get("source") or ""),   # nr_captline: paid / gift / mood, for the console
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
            if now - os.path.getmtime(p) > WAVLAKE_CACHE_KEEP_S:   # nr_wavcache: unplayed for 90 days
                os.remove(p)
        except OSError:
            pass
    path = os.path.join(WAVLAKE_SPOOL_DIR, pending["guid"] + ".mp3")
    if os.path.exists(path):
        try:
            os.utime(path, None)          # nr_wavcache: played again -> kept another 90 days
        except OSError:
            pass
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
_ZAPNAMES = {"mtime": 0.0, "names": {}}


def _zap_name(pk):
    """nr_videocard (2026-10-15): a zapper's name -- from nr-nostrchat's profile cache when it has one, else a short
    npub. Never raises, never asks a relay (the API stays fast)."""
    p = "/var/lib/jukebox/nostrchat-names.json"
    try:
        mt = os.path.getmtime(p)
        if mt != _ZAPNAMES["mtime"]:
            with open(p, encoding="utf-8") as fh:
                _ZAPNAMES["names"] = json.load(fh) or {}
            _ZAPNAMES["mtime"] = mt
        n = (_ZAPNAMES["names"].get(pk) or [""])[0]
        if n:
            return str(n)[:40]
    except Exception:
        pass
    try:
        lib, _ann = _nostrpost_libs()
        npub = lib.bech32_encode("npub", bytes.fromhex(pk))
        return npub[:10] + "…" + npub[-4:]
    except Exception:
        return ""


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


DONATE_MSG_MIN_SATS = 21      # nr_tinyzap (2026-10-22): a message needs at least the friction line behind it


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
                # nr_videocard (2026-10-15): a Nostr zap carries its message and its sender in the zap request
                via = "zap" if extra.get("nostr") else ""
                if via:
                    try:
                        zr = extra["nostr"]
                        zr = json.loads(zr) if isinstance(zr, str) else zr
                        if not message:
                            message = str(zr.get("content") or "")
                        zpk = str(zr.get("pubkey") or "")
                        if not name and re.fullmatch(r"[0-9a-f]{64}", zpk):
                            name = _zap_name(zpk)
                    except Exception:
                        pass
                # nr_tinyzap (2026-10-22, SF: an advert rode a 10-sat zap onto the video band): a payment under
                # the friction line that carries words is left out everywhere; without words it still shows.
                if amount // 1000 < DONATE_MSG_MIN_SATS and str(message).strip():
                    continue
                # nr_linkallow: filtered HERE, at the source, so the overlay and the site's own
                # donation popups are covered by one rule that cannot drift apart.
                items.append({"ts": p.get("time") or 0, "sats": amount // 1000,
                              "name": _strip_links(name.strip())[:40],
                              "message": _strip_links(str(message).strip())[:255], "via": via})
        except Exception:
            pass
    items.sort(key=lambda x: x["ts"], reverse=True)
    items = items[:limit]
    _donate_recent_cache["ts"] = now
    _donate_recent_cache["items"] = items
    return items


# nr_cal (2026-10-22, SF: "calendar upgrade plz"): the block mined at a moment, for the calendar pill's Block-height mode.
# From the station's own mempool (127.0.0.1:8999: no keys, read-only), cached per minute; plus the tip for estimates.
_BLK = {"tip": None, "tip_at": 0.0, "by_min": {}}


def _blocks_at(tss):
    now = time.time()
    if not _BLK["tip"] or now - _BLK["tip_at"] > 60:
        try:
            b = requests.get("http://127.0.0.1:8999/api/v1/blocks", timeout=5).json()[0]
            _BLK["tip"], _BLK["tip_at"] = {"height": int(b["height"]), "time": int(b["timestamp"])}, now
        except Exception:
            pass
    out = {}
    for ts in tss[:60]:
        m = ts // 60 * 60
        if m > now or m < 1231006505:                          # the future is an estimate in the page; nothing before block 0
            continue
        h = _BLK["by_min"].get(m)
        if h is None:
            try:
                h = int(requests.get("http://127.0.0.1:8999/api/v1/mining/blocks/timestamp/%d" % m, timeout=5).json()["height"])
            except Exception:
                continue
            if len(_BLK["by_min"]) > 50000:
                _BLK["by_min"].clear()
            _BLK["by_min"][m] = h
        out[str(ts)] = h
    return {"tip": _BLK["tip"], "blocks": out}


_TICKER_CTA = "Keep the ship afloat: donate or play a song at noderunnersradio.com"


def _ticker_ts(ts):
    try:
        return float(ts)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(ts)).timestamp()
        except Exception:
            return 0.0


def _donate_ticker():
    """nr_cardband (2026-10-15): what the site card's gift band shows -- the video band's rules (render_ticker.py):
    Satograms out; the announcer's word list on names and messages (a hit: that part goes); the list unavailable =
    amounts only. 'new' = under 10 minutes old: those first, oldest first, then the last five, then the call to donate.
    The captain's brake /tmp/nr-ticker-off empties it."""
    if os.path.exists("/tmp/nr-ticker-off"):
        return {"items": [], "cta": "", "off": True}
    try:
        _lib, ann = _nostrpost_libs()
        bl = ann.load_blocklist()
    except Exception:
        ann, bl = None, None
    now, fresh, rest = time.time(), [], []
    for d in _donate_recent(10):
        msg = str(d.get("message") or "")
        sats = int(d.get("sats") or 0)
        if sats <= 0 or "satogram" in msg.lower() or "\U0001F4E8" in msg:
            continue
        name = ""
        if ann is not None:
            try:
                name, msg = ann.clean(d.get("name"), 40), ann.clean(msg, 280)
                if name and ann.blocked(name, bl):
                    name = ""
                if msg and ann.blocked(msg, bl):
                    msg = ""
            except Exception:
                name, msg = "", ""
        else:
            msg = ""
        new = now - _ticker_ts(d.get("ts")) < 600
        (fresh if new else rest).append({"sats": sats, "name": name, "message": msg, "new": new})
    return {"items": fresh[::-1] + rest[:5][::-1], "cta": _TICKER_CTA}


# --- nr_charts (2026-10-17, SF: "a top 10, a top 21, a top 42, and a top 100 folks can scroll through on all fronts"):
# the top requests, counted from the play log. Requests only (the lanes below, the same set the mood queue treats as
# paid), never who asked: the log does not know. One song = one line, whatever the file's tags: a leading track number
# goes, case and spacing fold, a "Remastered" tail is ignored. The windows roll (the last 7 / 30 / 365 days), so a chart
# is never empty on the 1st of a month. A window opens only once the log is that old: year 21 July 2027, decade 21 July 2036 (ten years with three leap days = 3653 days).
# Ties go to the song asked for most recently. Up/down marks compare with the window before; "all time" compares with
# all time as it stood a week ago. Brakes, no restart needed (SF: "I can always disable it, later"):
#   /var/lib/jukebox/charts.off        -- the charts rest: the site hides the Top tab, /top says so
#   /var/lib/jukebox/charts-again.off  -- the "play it again" buttons go away
_CHART_LANES = {"local": "library", "jukebox": "library", "gift": "library", "wavlake": "wavlake"}
_CHART_PERIODS = (("week", 7), ("month", 30), ("all", None), ("year", 365), ("decade", 3653))
_CHART_SIZES = (10, 21, 42, 100, 210, 420)
_CHART_OFF = "/var/lib/jukebox/charts.off"
_CHART_AGAIN_OFF = "/var/lib/jukebox/charts-again.off"
_CHART_TTL_S = 600
_CHART = {"lock": threading.Lock(), "t": 0.0, "rows": [], "first": None, "out": {}}
_CHART_NUM_SEP = re.compile(r"^\s*\d{1,4}\s*[.\-_]\s+")     # "01. ", "02 - ", "216. ", "06- "  (not "3.14")
_CHART_NUM_ZERO = re.compile(r"^\s*0\d{1,3}\s+")            # "0028 Dire Straits", "01 Dead And Bloated"
_CHART_NUM_LIST = re.compile(r"^\s*\d{3,4}\s+(?=\S)")       # artist only: "1020 Kenny Rogers" (a list rank)
_CHART_NUM_SIDE = re.compile(r"^\s*\d{1,2}[abAB]\s+")       # title only: "6a Let Me Be" (a record side)
_CHART_REMASTER = re.compile(r"\s*[-–(\[][^-–()\[\]]*remaster[^-–()\[\]]*[)\]]?\s*$", re.I)


def _chart_strip(s, pats):
    """the first pattern that matches comes off -- and never strips a name to nothing ("99 Luftballons" stays)"""
    s = (s or "").replace("\x00", "").strip()
    for p in pats:
        n = p.sub("", s, count=1).strip()
        if n != s:
            return n or s
    return s


def _chart_load():
    """the play log as [(epoch, lane, key, artist, title)], oldest first; read again every 10 minutes"""
    now = time.time()
    if _CHART["first"] is not None and now - _CHART["t"] < _CHART_TTL_S:
        return
    rows, first = [], None
    try:
        with open(PLAYLOG_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) != 6 or p[1] != "play":
                    continue
                try:
                    ts = datetime.fromisoformat(p[0]).timestamp()
                except Exception:
                    continue
                if first is None:
                    first = ts
                lane = _CHART_LANES.get(p[2].replace("\x00", ""))
                if not lane or not (p[4].strip() or p[5].strip()):
                    continue
                a = _chart_strip(p[4], (_CHART_NUM_SEP, _CHART_NUM_ZERO, _CHART_NUM_LIST))
                t = _chart_strip(p[5], (_CHART_NUM_SEP, _CHART_NUM_ZERO, _CHART_NUM_SIDE))
                key = (" ".join(a.casefold().split()), " ".join((_CHART_REMASTER.sub("", t) or t).casefold().split()))
                rows.append((ts, lane, key, a, t))
    except FileNotFoundError:
        pass
    _CHART.update(t=now, rows=rows, first=first if first is not None else now, out={})


def _chart_count(since, until, lane):
    n, last, shown = {}, {}, {}
    for ts, ln, key, a, t in _CHART["rows"]:
        if ts < since or ts >= until or (lane and ln != lane):
            continue
        n[key] = n.get(key, 0) + 1
        last[key] = ts
        shown[key] = (a, t, ln)                     # the newest tags win the display
    return sorted(n, key=lambda k: (-n[k], -last[k])), n, shown


def _chart_day(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def _chart_build(period, size, lane):
    now, first = time.time(), _CHART["first"]
    periods = {}
    for name, days in _CHART_PERIODS:
        opens = first + days * 86400 if days else first
        periods[name] = {"open": now >= opens, "opens": _chart_day(opens)}
    days = dict(_CHART_PERIODS)[period]
    out = {"period": period, "n": size, "lane": lane or "all", "since": _chart_day(first), "periods": periods,
           "open": periods[period]["open"], "updated": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
           "requests": 0, "songs": 0, "moves": False, "top": [], "wavlake_top": []}
    if not out["open"]:
        return out
    if days:
        since, prev_since, prev_until = now - days * 86400, now - 2 * days * 86400, now - days * 86400
    else:
        since, prev_since, prev_until = 0.0, 0.0, now - 7 * 86400
    order, n, shown = _chart_count(since, now + 1, lane)
    out["requests"], out["songs"] = sum(n.values()), len(order)
    out["moves"] = (prev_since >= first) if days else (prev_until > first)   # the window before is fully in the log
    prank = {}
    if out["moves"]:
        porder, _pn, _ps = _chart_count(prev_since, prev_until, lane)
        prank = {k: i + 1 for i, k in enumerate(porder)}
    for i, k in enumerate(order[:size]):
        a, t, ln = shown[k]
        out["top"].append({"rank": i + 1, "artist": a, "title": t, "lane": ln, "n": n[k],
                           "prev": prank.get(k), "new": bool(out["moves"]) and k not in prank,
                           "q": (a + " " + t).strip()[:120]})
    if not lane:
        worder, wn, wshown = _chart_count(since, now + 1, "wavlake")
        out["wavlake_top"] = [{"rank": i + 1, "artist": wshown[k][0], "title": wshown[k][1], "n": wn[k],
                               "q": (wshown[k][0] + " " + wshown[k][1]).strip()[:120]} for i, k in enumerate(worder[:5])]
    return out


def _charts(period, size, lane):
    """GET /api/charts?period=week|month|all|year|decade&n=10|21|42|100|210|420&lane=library|wavlake (lane empty = all)"""
    if os.path.exists(_CHART_OFF):
        return {"off": True}
    period = period if period in dict(_CHART_PERIODS) else "month"
    size = size if size in _CHART_SIZES else 10
    lane = lane if lane in ("library", "wavlake") else ""
    with _CHART["lock"]:
        _chart_load()
        hit = _CHART["out"].get((period, size, lane))
        if not hit:
            hit = _CHART["out"][(period, size, lane)] = _chart_build(period, size, lane)
    out = dict(hit)
    out["again"] = not os.path.exists(_CHART_AGAIN_OFF)
    return out


# The radio raised ~3M sats across its pre-LNbits life (Geyser + years of
# donations -- what actually sits in the real jar). The payment-history sum
# below only sees the new plumbing, so the public "sats raised" adds this
# baseline. Override with env DONATE_BASELINE_SATS when SF refines the figure.
DONATE_BASELINE_SATS = int(os.environ.get("DONATE_BASELINE_SATS", "3000000"))
# lifetime sats already paid to DJs before settlement logging existed (2026-07-24, SF-confirmed)
DJ_PAID_SEED_SATS = int(os.environ.get("DJ_PAID_SEED_SATS", "13516"))

_donate_total_cache = {"ts": 0.0, "sats": None}

# nr_pageedit (2026-09-14): the captain edits page TEXT in place, in English; the other five languages follow by DeepL
# (key present) or wait in a queue for a hand translation. Only these pages, only these languages, only keys that exist.
_PAGE_EDIT_DIR = "/srv/jukebox/site"
_PAGE_EDIT_PAGES = ("dj-handbook", "funding", "manifesto", "obs-guide", "play", "progress", "roadmap", "why-sats")
_PAGE_EDIT_LANGS = ("en", "nl", "de", "fr", "es", "it", "pt", "ru", "zh", "ja")
_PAGE_EDIT_SHARED = ("top", "playBadge", "englishCounts", "langLabel")
_PAGE_EDIT_TAGS = {"b", "strong", "i", "em", "u", "s", "br", "code", "a"}
_STALE_PATH = "/var/lib/jukebox/i18n-stale.json"
_DEEPL_KEY_FILE = "/srv/jukebox/config/nr-deepl/key"
_DEEPL_TARGET = {"nl": "NL", "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "pt": "PT-PT", "ru": "RU", "zh": "ZH-HANS", "ja": "JA"}
_page_edit_lock = threading.RLock()


def _page_sanitize(src):
    """Keep b/strong/i/em/u/s/br/code/a[href] and span (class only); drop every other tag but keep its text; script/style
    content vanishes. nr_editfix (2026-10-05): span used to be dropped too, and that broke the handbook's grid rows
    ("> text" = two spans) the moment the captain edited one. A span keeps only plain class names; spans are balanced."""
    import html as _html
    from html.parser import HTMLParser as _HP
    out = []; skip = [0]; spans = [0]
    class P(_HP):
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"): skip[0] += 1; return
            if skip[0]: return
            if tag == "br": out.append("<br>"); return
            if tag == "span":
                cls = " ".join(c for c in (next((v for k, v in attrs if k == "class"), "") or "").split()
                               if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,30}", c))[:80]
                spans[0] += 1
                return out.append('<span class="%s">' % cls if cls else "<span>")
            if tag not in _PAGE_EDIT_TAGS: return
            if tag == "a":
                href = next((v for k, v in attrs if k == "href"), "") or ""
                if not re.match(r"^(https?://|/|#|mailto:)", href.strip()): return out.append("<a>")
                extra = ' target="_blank" rel="noopener"' if href.strip().startswith("http") else ""
                return out.append('<a href="%s"%s>' % (_html.escape(href.strip(), quote=True), extra))
            out.append("<%s>" % tag)
        def handle_endtag(self, tag):
            if tag in ("script", "style"): skip[0] = max(0, skip[0] - 1); return
            if skip[0]: return
            if tag == "span":
                if spans[0]: spans[0] -= 1; out.append("</span>")
                return
            if tag == "br" or tag not in _PAGE_EDIT_TAGS: return
            out.append("</%s>" % tag)
        def handle_data(self, data):
            if not skip[0]: out.append(_html.escape(data, quote=False))
        def handle_entityref(self, name): out.append("&%s;" % name)
        def handle_charref(self, name): out.append("&#%s;" % name)
    p = P(convert_charrefs=False); p.feed(src or ""); p.close()
    return ("".join(out) + "</span>" * spans[0]).strip()


def _page_rewrap_row(val, tick):
    """nr_editfix: a grid row ("> text") gets its two spans back -- the mark in the first, the words in the second.
    A copy of the mark at the start of the words (also inside a leading <b>) is taken out. A value with spans is kept."""
    if "<span" in val: return val
    m = re.match(r"(\s*(?:<(?:b|strong|em|i|u)>)?)\s*" + re.escape(tick) + r"\s*", val)
    if m: val = m.group(1) + val[m.end():]
    return '<span class="tick">%s</span><span>%s</span>' % (tick, val.strip())


def _page_keep_rows(text, key, val):
    """nr_editfix: when key's block on the page is a grid row (<span class="tick">..</span><span>..), val keeps that shape."""
    rx = re.compile(r'<(\w+)\b[^>]*\sdata-i18n-html="%s"[^>]*>(.*?)</\1>' % re.escape(key), re.S)
    ms = list(rx.finditer(text))
    if len(ms) != 1: return val
    mt = re.match(r'\s*<span class="tick">(.*?)</span><span>', ms[0].group(2), re.S)
    return _page_rewrap_row(val, mt.group(1)) if mt else val


def _page_plain(src):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(src or "")).strip()


def _page_dict(text, lang):
    """(line index, dict) of the page's one-line dictionary for lang. Raises ValueError when not exactly one."""
    pat = re.compile(r'^(\s+)"%s": (\{.*\}),?\s*$' % lang)
    lines = text.split("\n")
    hits = [i for i, l in enumerate(lines) if pat.match(l)]
    if len(hits) != 1: raise ValueError("no single dictionary line for %s" % lang)
    return hits[0], json.loads(pat.match(lines[hits[0]]).group(2))


def _page_kind(text, key):
    if ('data-i18n-html="%s"' % key) in text: return "html"
    if ('data-i18n="%s"' % key) in text: return "text"
    return None


def _page_put(text, lang, values):
    """Pure: write or INSERT dictionary entries for lang (no markup change). Returns the new text."""
    idx, d = _page_dict(text, lang)
    lines = text.split("\n"); line = lines[idx]
    for key, val in values.items():
        js = json.dumps(val, ensure_ascii=False).replace("</", "<\\/")
        mm = re.search(r'"%s": "(?:[^"\\]|\\.)*"' % re.escape(key), line)
        if mm:
            line = line[:mm.start()] + '"%s": %s' % (key, js) + line[mm.end():]
        else:
            m2 = re.match(r"^(.*\})(,?)\s*$", line, re.S)
            body, tail = m2.group(1), m2.group(2)
            core = body[:-1].rstrip()
            line = core + ("" if core.endswith("{") else ", ") + '"%s": %s' % (key, js) + "}" + tail
    lines[idx] = line
    _page_dict("\n".join(lines), lang)                    # must still parse
    return "\n".join(lines)


def _page_set_values(text, lang, edits):
    """Pure: (new_text, saved_keys, skipped[{key, why}]). English edits also rewrite the markup block."""
    import html as _html
    _idx, d = _page_dict(text, lang)
    saved, skipped, values = [], [], {}
    for e in edits or []:
        key = str((e or {}).get("key") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", key) or key not in d:
            skipped.append({"key": key, "why": "unknown key"}); continue
        if key.startswith(_PAGE_EDIT_SHARED):
            skipped.append({"key": key, "why": "shared label, edit it in the template"}); continue
        kind = _page_kind(text, key)
        if not kind: skipped.append({"key": key, "why": "no element carries it"}); continue
        val = e.get("html") if kind == "html" else e.get("text")
        if not isinstance(val, str) or len(val) > 20000:
            skipped.append({"key": key, "why": "missing or too long"}); continue
        values[key] = (kind, _page_sanitize(val) if kind == "html" else _page_plain(val))
        if kind == "html": values[key] = (kind, _page_keep_rows(text, key, values[key][1]))   # nr_editfix
    new = _page_put(text, lang, {k: v for k, (kind, v) in values.items()}) if values else text
    saved = list(values)
    if lang == "en":
        for key, (kind, val) in values.items():
            attr = "data-i18n-html" if kind == "html" else "data-i18n"
            rx = re.compile(r'(<(\w+)\b[^>]*\s%s="%s"[^>]*>)(.*?)(</\2>)' % (attr, re.escape(key)), re.S)
            ms = list(rx.finditer(new))
            if len(ms) != 1 or re.search(r"<%s[\s>]" % ms[0].group(2), ms[0].group(3)):
                skipped.append({"key": key, "why": "markup block not single/simple; dictionary saved, page text not"}); continue
            m = ms[0]
            inner = val if kind == "html" else _html.escape(val, quote=False)
            new = new[:m.start(3)] + inner + new[m.end(3):]
    return new, saved, skipped


def _page_write(path, new_text):
    """Dated backup beside the file (kept), atomic replace, read-back. Caller holds the lock. Returns the backup name."""
    import shutil as _shutil, tempfile as _tempfile
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    bak = path + ".bak-edit-" + stamp
    if os.path.exists(bak): bak += "-%d" % int(time.time() * 1000 % 1000)
    _shutil.copy2(path, bak)
    st = os.stat(path)
    fd, tmp = _tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
    with os.fdopen(fd, "wb") as fh: fh.write(new_text.encode("utf-8"))
    os.chmod(tmp, st.st_mode & 0o7777); os.replace(tmp, path)
    if open(path, "rb").read() != new_text.encode("utf-8"):
        _shutil.copy2(bak, path); raise RuntimeError("read-back mismatch, restored the backup")
    return os.path.basename(bak)


def _stale_load():
    try:
        with open(_STALE_PATH, encoding="utf-8") as fh: v = json.load(fh)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _stale_save(items):
    import tempfile as _tempfile
    fd, tmp = _tempfile.mkstemp(prefix="i18n-stale.", dir=os.path.dirname(_STALE_PATH))
    with os.fdopen(fd, "w", encoding="utf-8") as fh: json.dump(items, fh, ensure_ascii=False, indent=0)
    os.chmod(tmp, 0o644); os.replace(tmp, _STALE_PATH)


def _stale_add(page, keys):
    items = _stale_load(); have = {(i.get("page"), i.get("key")) for i in items}
    for k in keys:
        if (page, k) not in have: items.append({"page": page, "key": k, "ts": int(time.time())})
    _stale_save(items)


def _stale_remove(page, keys):
    ks = set(keys)
    _stale_save([i for i in _stale_load() if not (i.get("page") == page and i.get("key") in ks)])


def _deepl_key():
    k = (os.environ.get("DEEPL_API_KEY") or "").strip()
    if k: return k
    try:
        with open(_DEEPL_KEY_FILE, encoding="utf-8") as fh: return fh.read().strip()
    except Exception:
        return ""


def _deepl_host(key):
    return "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"


def _deepl_post(path, payload, key):
    r = requests.post(_deepl_host(key) + path, json=payload, headers={"Authorization": "DeepL-Auth-Key " + key}, timeout=60)
    if r.status_code != 200: raise RuntimeError("DeepL %s: %s" % (r.status_code, r.text[:200]))
    return r.json()


def _deepl_usage(key):
    r = requests.get(_deepl_host(key) + "/v2/usage", headers={"Authorization": "DeepL-Auth-Key " + key}, timeout=10)
    return r.json() if r.status_code == 200 else None


def _deepl_translate(texts, lang, html):
    key = _deepl_key()
    payload = {"text": list(texts), "source_lang": "EN", "target_lang": _DEEPL_TARGET[lang], "formality": "prefer_less"}
    if html: payload.update({"tag_handling": "html", "ignore_tags": ["code"]})
    return [t["text"] for t in _deepl_post("/v2/translate", payload, key)["translations"]]


def _page_translate(page, keys=None, missing=False):
    """DeepL: the given EN keys of `page` (and/or every EN key missing from a language) into nl/de/fr/es/pt.
    Writes the page once (dated backup), clears those keys from the waiting queue. Returns a report."""
    if not _deepl_key(): raise ValueError("no DeepL key on the box -- as sf, run: nr-deepl-key")
    if page not in _PAGE_EDIT_PAGES: raise ValueError("unknown page")
    path = os.path.join(_PAGE_EDIT_DIR, page + ".html")
    report = {"page": page, "per_lang": {}, "chars": 0, "keys": []}
    with _page_edit_lock:
        text = open(path, "rb").read().decode("utf-8")
        _i, en = _page_dict(text, "en")
        done_keys = set()
        for lang in ("nl", "de", "fr", "es", "it", "pt", "ru", "zh", "ja"):
            _j, d = _page_dict(text, lang)
            want = []
            for k in (keys or []):
                if k in en and not k.startswith(_PAGE_EDIT_SHARED) and k not in want: want.append(k)
            if missing:
                for k in en:
                    if k not in d and not k.startswith(_PAGE_EDIT_SHARED) and k not in want: want.append(k)
            want = [k for k in want if _page_kind(text, k)]
            if not want: report["per_lang"][lang] = 0; continue
            values = {}
            for is_html in (True, False):
                group = [k for k in want if (_page_kind(text, k) == "html") == is_html]
                for i in range(0, len(group), 40):
                    chunk = group[i:i + 40]; src = [en[k] for k in chunk]
                    out = _deepl_translate(src, lang, is_html)
                    if len(out) != len(chunk): raise RuntimeError("DeepL returned %d texts for %d" % (len(out), len(chunk)))
                    for k, v in zip(chunk, out): values[k] = _page_keep_rows(text, k, _page_sanitize(v)) if is_html else _page_plain(v)   # nr_editfix
                    report["chars"] += sum(len(x) for x in src)
            text = _page_put(text, lang, values)
            report["per_lang"][lang] = len(values); done_keys |= set(values)
        if done_keys:
            report["backup"] = _page_write(path, text)
        report["keys"] = sorted(done_keys)
        _stale_remove(page, done_keys)
    return report


def _page_save(page, lang, edits):
    if page not in _PAGE_EDIT_PAGES: raise ValueError("unknown page")
    if lang not in _PAGE_EDIT_LANGS: raise ValueError("unknown language")
    if not isinstance(edits, list) or not edits or len(edits) > 200: raise ValueError("no edits")
    path = os.path.join(_PAGE_EDIT_DIR, page + ".html")
    with _page_edit_lock:
        raw = open(path, "rb").read()
        new, saved, skipped = _page_set_values(raw.decode("utf-8"), lang, edits)
        if not saved:
            return {"ok": False, "saved": [], "skipped": skipped, "error": "nothing could be saved"}
        bak = _page_write(path, new)
        out = {"ok": True, "saved": saved, "skipped": skipped, "backup": bak, "lang": lang, "page": page}
        if lang == "en":
            _stale_add(page, saved)
            if _deepl_key():
                try:
                    rep = _page_translate(page, keys=saved)
                    out["translated"] = rep["per_lang"]; out["chars"] = rep["chars"]
                except Exception as e:
                    out["translate_error"] = str(e)[:200]
            out["waiting"] = [i["key"] for i in _stale_load() if i.get("page") == page]
    return out


def _i18n_status():
    key = _deepl_key(); items = _stale_load()
    counts = {}
    for i in items: counts[i.get("page")] = counts.get(i.get("page"), 0) + 1
    missing = {}
    for page in _PAGE_EDIT_PAGES:
        try:
            text = open(os.path.join(_PAGE_EDIT_DIR, page + ".html"), "rb").read().decode("utf-8")
            _i, en = _page_dict(text, "en")
            per = {}
            for lang in ("nl", "de", "fr", "es", "it", "pt", "ru", "zh", "ja"):
                _j, d = _page_dict(text, lang)
                per[lang] = sum(1 for k in en if k not in d and not k.startswith(_PAGE_EDIT_SHARED) and _page_kind(text, k))
            missing[page] = per
        except Exception as e:
            missing[page] = {"error": str(e)[:80]}
    usage = None
    if key:
        try: usage = _deepl_usage(key)
        except Exception: usage = None
    return {"deepl": bool(key), "usage": usage, "stale": items, "stale_counts": counts, "missing": missing}



# nr_remaining (2026-09-14): what is actually LEFT of the sats -- the offline jar and LNbits together -- is a number only
# the captain knows, so he types it in the console. It never moves by itself. Empty = the site shows no "remaining" line.
def _remaining_sats():
    """(sats, set_at_unix) or (None, None). Read from redis db 2 each call; the site polls this every ~20 s."""
    try:
        raw = rds.get("jukebox:sats_remaining")
        if not raw:
            return None, None
        ts = rds.get("jukebox:sats_remaining_ts")
        return int(raw), (int(ts) if ts else None)
    except Exception:
        return None, None


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


def _rot_never(path, meta=None):
    """nr_rotbtn (2026-10-06): the reason a file may NEVER enter the steady rotation, or "". HARD RULE (2026-07-29):
    Wavlake / V4V files air only on a paid request. Station jingles have their own lane. Live sets are not files."""
    meta = meta or {}
    p = (path or "").lower()
    src = str(meta.get("source") or "").lower()
    rc = str(meta.get("rights_class") or "").lower()
    if src == "wavlake" or rc == "v4v" or "wavlake" in p:
        return "Wavlake / V4V song -- these never join the rotation"
    if src == "jingle" or rc == "station" or "/noderunners radio jingles/" in p:
        return "a station jingle"
    if not path:
        return "no file (a live set or a stream)"
    if not path.startswith("/srv/"):
        return "not a library file"
    return ""


def _rot_add(path):
    import sqlite3 as _sq, random as _rand
    never = _rot_never(path)                     # nr_rotbtn: the hard guard, whatever path is posted
    if never:
        return False, never
    con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    row = con.execute("SELECT artist, title FROM tracks WHERE path=?", (path,)).fetchone()
    con.close()
    if not row:   # nr_rotfree (2026-10-29, SF): the CC & Public Domain lane rotates too
        fcon = _sq.connect(f"file:{FREE_DB}?mode=ro", uri=True)
        row = fcon.execute("SELECT artist, title FROM tracks WHERE path=?", (path,)).fetchone()
        fcon.close()
    if not row:
        return False, "that path is in neither the library nor the CC & Public Domain index -- only indexed tracks rotate"
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


_STATS = {"t": 0.0, "n": 0, "lib": 0, "free": 0}   # /api/stats cache: the index count moves rarely, ask sqlite hourly


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
                # nr_histreq (2026-10-24): a request or not -- never who asked
                plays.append({"ts": ts, "artist": artist, "title": title,
                              "req": "gift" if _source == "gift" else ("paid" if _source in ("local", "wavlake") else "")})
    except FileNotFoundError:
        pass
    return plays[-n:][::-1]


def _stage_live() -> bool:
    """Live mode = a settlement session is currently open. nr_autoreturn: a session that ended by itself is not live
    (it only sits in the live key for the seconds the captain's lever needs to settle it)."""
    try:
        s = _load_session()
        return s is not None and not s.get("ended_at")
    except Exception:
        return False


# --- nr_autoreturn (2026-10-22, SF: "next time, this should happen automatically" / "important! This should work next
# live sesh"). The radio comes back by itself; the money waits for the captain's lever. See gen-autoreturn.py.
SESH_ENDED_KEY = "livesession:ended"
SESH_IDLE_END_S = 600            # the DJ streamed, then the stream has been gone this long
SESH_NOSTREAM_END_S = 1200       # nobody streamed this long after the lever went on
SESH_AUTOEND_OFF = "/srv/jukebox/config/nr-autoreturn.off"
OWNCAST_STATUS_URL = "http://127.0.0.1:8090/api/status"


def _iso_ts(s) -> float:
    """Owncast's '2026-10-21T06:03:26Z' (any fraction) -> epoch seconds; 0.0 when missing or unreadable."""
    if not s:
        return 0.0
    try:
        import calendar
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return 0.0


def _autoend_reason(sess, st, now):
    """Why this session should end by itself now, or None. Only on a clear OFFLINE from Owncast."""
    if not sess or sess.get("closing") or sess.get("ended_at"):
        return None
    if not isinstance(st, dict) or st.get("online") is not False:
        return None
    started = float(sess.get("started_at") or 0)
    conn, disc = _iso_ts(st.get("lastConnectTime")), _iso_ts(st.get("lastDisconnectTime"))
    if conn > started:                                   # a DJ streamed during this session
        if disc >= conn and now - disc >= SESH_IDLE_END_S:
            return "the DJ stream stopped %d min ago" % int((now - disc) // 60)
        return None
    if now - started >= SESH_NOSTREAM_END_S:
        return "nobody streamed in the first %d min" % int((now - started) // 60)
    return None


def _load_parked():
    try:
        raw = rds.get(SESH_ENDED_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _park_session(reason, now=None):
    """Move the open session to SESH_ENDED_KEY (atomic, WATCH). Never a closing one, never over another parked pot."""
    now = time.time() if now is None else now
    for _ in range(8):
        pipe = rds.pipeline()
        try:
            pipe.watch(LIVESESSION_KEY)
            raw = pipe.get(LIVESESSION_KEY)
            if raw is None:
                return False
            sess = json.loads(raw)
            if sess.get("closing") or sess.get("ended_at") or rds.exists(SESH_ENDED_KEY):
                return False
            sess.update({"ended_at": int(now), "ended_why": reason})
            pipe.multi()
            pipe.set(SESH_ENDED_KEY, json.dumps(sess))
            pipe.delete(LIVESESSION_KEY)
            pipe.execute()
            return True
        except redis.WatchError:
            continue
        finally:
            try:
                pipe.reset()
            except Exception:
                pass
    return False


def _unpark_session():
    """Lever OFF / Clear: a parked session goes back to the live key, where the settlement code expects it. Atomic
    (RENAMENX): never over a session that is open."""
    try:
        return bool(rds.renamenx(SESH_ENDED_KEY, LIVESESSION_KEY))
    except Exception:
        return False


def _owncast_status():
    r = requests.get(OWNCAST_STATUS_URL, timeout=5)
    r.raise_for_status()
    return r.json()


def _sesh_autoend_once(now=None, status_fn=None, feed_fn=None):
    """One look. Returns the reason when it ended a session, else None."""
    if os.path.exists(SESH_AUTOEND_OFF):
        return None
    sess = _load_session()
    if not sess:
        return None
    try:
        st = (status_fn or _owncast_status)()
    except Exception:
        return None
    now = time.time() if now is None else now
    why = _autoend_reason(sess, st, now)
    if not why or not _park_session(why, now):
        return None
    try:
        rds.rpush("livesession:settlements", json.dumps({"ts": int(now), "autoend": True, "why": why,
                                                         "roster": [(d.get("alias") or "?") for d in sess.get("djs", [])]}))
    except Exception:
        pass
    notes = (feed_fn or (lambda: Handler._sesh_feed(None, False)))()
    print("nr_autoreturn: session ended by itself (%s); feed back to radio%s; the pot waits for the lever"
          % (why, (" -- " + "; ".join(notes)) if notes else ""), flush=True)
    return why


def _sesh_autoend():
    while True:
        time.sleep(60)
        try:
            _sesh_autoend_once()
        except Exception as e:
            print("nr_autoreturn: %s" % e, flush=True)


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
    pending["note_token"] = "" if pending["note"] else pending.get("note_token", "")   # nr_xnote (2026-09-15): no message from X -> the page may add one
    pending["x_code"] = code
    pending["x_share"] = int(xrec.get("sats") or 0)


def _xlane_reply(reply: dict, pending: dict = None) -> dict:
    if pending and pending.get("note_token"):            # nr_xnote: the page's own note box stays usable
        return dict(reply)
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
                   via: str = "", full: bool = False, kind: str = ""):
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
                              "ts": int(time.time()), "req_id": req_id, "kind": kind}))
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

_CAPT_T0 = time.time()   # nr_captline: a song first seen in the first seconds after OUR start has an unknown start time


def _capt_running(on):
    """nr_captline: the captain's line is playing right now (on, and a song was handed over in the last 15 min)."""
    if not on:
        return False
    try:
        p = json.loads(rds.get("mood:pushed") or b"{}")
        return bool(p) and time.time() - float(p.get("ts") or 0) < 900
    except Exception:
        return False


def _capt_head_pn():
    """nr_captline: the top song of the line is a Play-now pick -- it plays even while the line is paused."""
    try:
        raw = rds.lindex("mood:list", 0)
        return bool(raw) and bool(json.loads(raw).get("pn"))
    except Exception:
        return False


def _capt_clock():
    """nr_captline: (rid, filename, started) of the newest song on air, ANY lane. started = liquidsoap's on_air_timestamp,
    else the first time we saw it -- None when that first sight came right after our own start (unknown)."""
    try:
        rids = _mood_on_air_rids()
    except Exception:
        return None, "", None
    if not rids:
        return None, "", None
    rid = rids[-1]                               # the newest track last (two during a crossfade)
    try:
        m = request_metadata(rid)
    except Exception:
        return rid, "", None
    fn = m.get("filename") or ""
    try:
        ts = float(m.get("on_air_timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 0:
        return rid, fn, ts
    key = ("mood:onair:%s:%s" % (rid, fn))[:240]
    rds.set(key, str(time.time()), nx=True, ex=7200)
    try:
        since = float(rds.get(key) or 0)
    except (TypeError, ValueError):
        since = 0.0
    return rid, fn, (since if since > _CAPT_T0 + 6 else None)


def _capt_handed():
    """nr_captline: a song of the line is already handed to the radio and waits there (it can no longer move)."""
    try:
        rid, _m = _mood_on_air()
        return bool([r for r in _mood_queue_rids() if r != rid])
    except Exception:
        return False


def _capt_put(path, top):
    """nr_captline: ONE song into the captain's line -- at the TOP (Play now) or at the BOTTOM (+ Queue). The caller has
    checked the path. A line that is not playing starts fresh as "hand-picked" and on. Returns (item, n, fresh, paused)."""
    it = _mood_items_for([path])[0]
    it["hand"] = True                            # chosen by hand: the 24-hour rule does not apply
    if top:
        it["pn"] = True                          # Play now: plays even while the line is paused
    with _mood_lock:
        on = rds.get("mood:on") == b"1"
        fresh = rds.llen("mood:list") == 0 and not _capt_running(on)
        paused = (not fresh) and not on
        pipe = rds.pipeline()
        if fresh:
            pipe.delete("mood:skipped")
            pipe.set("mood:name", "hand-picked")
            pipe.set("mood:played", 0)
            pipe.set("mood:on", "1")
        if top:
            pipe.lpush("mood:list", json.dumps(it))
        else:
            pipe.rpush("mood:list", json.dumps(it))
        pipe.execute()
        n = rds.llen("mood:list")
        _mood_cache["recent_t"] = 0.0
    return it, n, fresh, paused


def _capt_path_ok(path):
    """nr_captline: a song the captain may put in the line -- in the library index AND on disk."""
    if not path or not path.startswith("/srv/") or ".." in path:
        return False
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            hit = con.execute("SELECT 1 FROM tracks WHERE path = ?", (path,)).fetchone() is not None
        finally:
            con.close()
    except Exception:
        hit = False
    hit = hit or bool(_free_meta([path]))         # nr_mylists2: a free-lane song may go in the line too
    return hit and os.path.isfile(path)


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


def _free_meta(paths):
    """nr_mylists2: path -> (artist, title, duration, rights_class) for songs that live in the free lane (free.db)."""
    out = {}
    paths = [p for p in paths if p]
    if not paths:
        return out
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{FREE_DB}?mode=ro", uri=True)
        try:
            for i in range(0, len(paths), 400):
                chunk = paths[i:i + 400]
                q = "SELECT path, artist, title, duration_s, rights_class FROM tracks WHERE path IN (%s)" % ",".join("?" * len(chunk))
                for p, a, t, d, r in con.execute(q, chunk):
                    out[p] = (a or "", t or "", int(d or 0), r or "public_domain")
        finally:
            con.close()
    except Exception:
        pass
    return out


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
    meta.update(_free_meta([p for p in paths if p not in meta]))   # nr_mylists2: free-lane songs keep their own tags + rights
    items = []
    for p in paths:
        a, t, d, r = meta.get(p, ("", os.path.splitext(os.path.basename(p))[0], 0, "collecting_society"))
        items.append({"path": p, "artist": a, "title": t, "dur": d, "rights": r})
    return items


def _mood_pop_next(only_pn=False):
    """The next playable item off the list, skipping recent airings. None when the list is empty.
    nr_captline: a hand-picked song (Play now / + Queue) is never skipped for a recent airing -- the captain chose it.
    only_pn: the line is paused, so only a Play-now song at the top may go."""
    recent = _mood_recent_keys()
    for _ in range(_MOOD_MAX_SKIPS):
        if only_pn and not _capt_head_pn():
            return None
        raw = rds.lpop("mood:list")
        if raw is None:
            return None
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not item.get("hand") and _mood_key(item.get("artist"), item.get("title")) in recent:
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


def _mood_hand_over(only_pn=False):
    """Pop the next playable item and push it. Caller holds _mood_lock. Returns a reason."""
    item = _mood_pop_next(only_pn)
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
    """One feeder tick. Returns a short reason (also handy for tests).
    nr_captline (2026-10-05): a song of the captain's line goes to the radio only when the REQUEST line is empty (paid
    requests play first anyway) and ~25 s before the end of whatever is on air -- not at once -- so Play now, up/down and
    drag can still change the order until then. A Play-now song at the top plays even while the line is paused."""
    try:
        _capt_clock()                            # clock every song on air from first sight, whatever the line does
    except Exception:
        pass
    on = rds.get("mood:on") == b"1"
    pn = _capt_head_pn()
    if not on and not pn and not rds.exists("mood:pushed"):
        return "off"
    if not _mood_lane_ok():
        return "nolane"
    rid, m = _mood_on_air()
    if rid:                                      # clock the on-air mood track from first sight, even while paused
        key = ("mood:onair:%s:%s" % (rid, m.get("filename") or ""))[:240]
        rds.set(key, str(time.time()), nx=True, ex=7200)
    if not on and not pn:
        return "off"
    if _stage_live():
        return "live"
    if rds.llen("mood:list") == 0:
        return "empty"
    with _mood_lock:
        on = rds.get("mood:on") == b"1"
        if not on and not _capt_head_pn():
            return "off"                         # Off/Pause landed while we were looking
        try:
            waiting = [r for r in _mood_queue_rids() if r != rid]
        except Exception:
            return "unknown"                     # a partial socket answer is not "nothing queued"
        if waiting:
            return "queued"                      # one is already handed to the deck
        try:
            if queue_rids():
                return "requests"                # requests play first; until they are done the song stays movable
        except Exception:
            return "unknown"
        if rid:
            since = float(rds.get(key) or time.time())
            dur = _mood_duration(m.get("filename") or "")
            if dur and (dur - (time.time() - since)) > _MOOD_LOOKAHEAD_S:
                return "playing"                 # hand the next one over ~25 s before the end
        else:
            orid, ofn, since = _capt_clock()
            dur = _mood_duration(ofn) if (orid and ofn) else 0
            if since and dur and (dur - (time.time() - since)) > _MOOD_LOOKAHEAD_S:
                return "waiting"                 # nr_captline: ~25 s before the end of the song on air
        return _mood_hand_over(only_pn=not on)


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
    for raw in rds.lrange("mood:list", 0, 49):          # nr_captline: the console shows 50
        try:
            it = json.loads(raw)
            nxt.append({"artist": it.get("artist", ""), "title": it.get("title", ""), "dur": it.get("dur", 0), "path": it.get("path", ""),
                        "hand": bool(it.get("hand")), "pn": bool(it.get("pn"))})
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
    handed = None                                # nr_captline: the song already handed to the radio (it can no longer move)
    if queued:
        try:
            p = json.loads(rds.get("mood:pushed") or b"{}")
            handed = {"artist": p.get("artist", ""), "title": p.get("title", "")} if p else None
        except Exception:
            handed = None
    return {"on": on, "name": name, "left": rds.llen("mood:list"), "played": int(rds.get("mood:played") or 0),
            "next": nxt, "skipped": skipped,
            "now": ({"artist": m.get("artist", ""), "title": m.get("title", "")} if rid else None),
            "queued": queued, "handed": handed, "lane_ok": _mood_lane_ok(), "live": _stage_live()}


# ---- nr_pepper (2026-10-31): songs from outside the steady rotation, peppered in ------------------------------------
# SF: "I do want stuff that is not in that steady rotation playlist to be 'peppered in'", without upsetting its feel.
# After every N rotation songs (console, 3-10, start 4) ONE song from the pepper deck goes into the mood queue, ~25 s
# before the end of the song on air -- the same road and timing as the captain's line, but only while that line is idle,
# the request line is empty and no live session is open. Paid requests always come first (liquidsoap's order).
# The deck (pepper-deck.db) is dealt nightly by nr-pepper-deck; this file keeps what has peppered (pepper.db 'played':
# no repeat until the whole deck has played) and the captain's "Not again" (pepper.db 'never'). Christmas songs come
# from their own pile, 20-30 December only (one pepper in 3 then). The song is stamped source=pepper: the deal counts
# only "rotation" rows, so the rotation deck never skips a card; the play log, BUMA and the 24-h rule see it as usual.
# redis (db 2): pepper:on "1"/"0" | pepper:every | pepper:last_ts | pepper:deck_id | pepper:pos | pepper:xpos |
#   pepper:n (handed over) | pepper:log (last 30 json) | pepper:paths (recent pepper paths, for the site's mark)
# Brakes: the console switch; touch /var/lib/jukebox/pepper.off; a live session; the captain's line; requests waiting.
PEPPER_DECK = "/var/lib/jukebox/pepper-deck.db"
PEPPER_STATE = "/var/lib/jukebox/pepper.db"
PEPPER_OFF = "/var/lib/jukebox/pepper.off"
_PEPPER_EVERY_DEFAULT = 4
_PEPPER_XMAS = ((12, 20), (12, 30))             # SF: "only from 20 december up to 30 dec"
_pepper_cache = {"rot_t": 0.0, "rot_n": 0, "rot_since": 0.0}


def _pepper_db():
    import sqlite3 as _sq
    con = _sq.connect(PEPPER_STATE, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS played (key TEXT PRIMARY KEY, path TEXT, ts INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS never (key TEXT PRIMARY KEY, path TEXT, ts INTEGER)")
    return con


def _pepper_every():
    try:
        n = int(rds.get("pepper:every") or _PEPPER_EVERY_DEFAULT)
    except (TypeError, ValueError):
        n = _PEPPER_EVERY_DEFAULT
    return max(3, min(10, n))


def _pepper_on():
    return rds.get("pepper:on") != b"0" and not os.path.exists(PEPPER_OFF)


def _pepper_rot_since(since):
    """Rotation songs that started airing after `since` (epoch), from the play-log tail; cached 10 s."""
    import datetime as _dt
    now = time.time()
    if now - _pepper_cache["rot_t"] < 10 and _pepper_cache["rot_since"] == since:
        return _pepper_cache["rot_n"]
    n = 0
    try:
        with open(PLAYLOG_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 120_000))
            chunk = f.read().replace(b"\x00", b"").decode("utf-8", "replace")
        for line in chunk.split("\n")[1:]:
            parts = line.rstrip("\r").split("\t")
            if len(parts) != 6 or parts[1] != "play" or not parts[2].startswith("rotation"):
                continue
            try:
                if _dt.datetime.fromisoformat(parts[0]).timestamp() > since:
                    n += 1
            except Exception:
                continue
    except Exception:
        n = 0
    _pepper_cache.update(rot_t=now, rot_n=n, rot_since=since)
    return n


def _pepper_xmas_now():
    lt = time.localtime()
    return _PEPPER_XMAS[0] <= (lt.tm_mon, lt.tm_mday) <= _PEPPER_XMAS[1]


def _pepper_deck_meta():
    import sqlite3 as _sq
    con = _sq.connect(f"file:{PEPPER_DECK}?mode=ro", uri=True)
    try:
        return {k: v for k, v in con.execute("SELECT k, v FROM meta")}
    finally:
        con.close()


def _pepper_next():
    """The next playable card, or None. Moves the cursor past every card it looks at."""
    import sqlite3 as _sq
    meta = _pepper_deck_meta()
    if (rds.get("pepper:deck_id") or b"").decode() != meta.get("id", ""):
        pipe = rds.pipeline()
        pipe.set("pepper:deck_id", meta.get("id", ""))
        pipe.set("pepper:pos", 0)
        pipe.set("pepper:xpos", 0)
        pipe.execute()                           # a fresh deal starts at its top (played songs are already out of it)
    pile, ck = "deck", "pepper:pos"
    if _pepper_xmas_now() and int(rds.get("pepper:n") or 0) % 3 == 0 and int(meta.get("xmas") or 0):
        pile, ck = "xmas", "pepper:xpos"
    recent = _mood_recent_keys()
    rot = _rot_set()
    st = _pepper_db()
    dk = _sq.connect(f"file:{PEPPER_DECK}?mode=ro", uri=True)
    try:
        for _ in range(40):
            pos = int(rds.incr(ck)) - 1
            row = dk.execute("SELECT key, path, artist, title, dur FROM %s WHERE pos=?" % pile, (pos,)).fetchone()
            if row is None:
                if pile == "xmas":
                    rds.set(ck, 0)               # the Christmas pile simply starts over; the deck waits for a new deal
                return None
            key, path, artist, title, dur = row
            if path in rot or not os.path.isfile(path) or _mood_key(artist, title) in recent:
                continue
            if st.execute("SELECT 1 FROM played WHERE key=? UNION SELECT 1 FROM never WHERE key=?", (key, key)).fetchone():
                continue
            return {"key": key, "path": path, "artist": artist, "title": title, "dur": dur, "pile": pile}
        return None
    finally:
        dk.close()
        st.close()


def _pepper_push(item):
    """Hand ONE pepper song to the mood queue, stamped source=pepper. "ok" / "retry" / "dead"."""
    meta = {"rights_class": "collecting_society", "source": "pepper", "artist": item.get("artist") or "",
            "title": item.get("title") or "", "replay_gain": _gain_note(item["path"])}
    meta = {k: str(v or "").replace("#{", "# {") for k, v in meta.items()}
    try:
        lines = (_mood_send("mood.push %s%s" % (_lc._annotate(meta), item["path"]), timeout=_PUSH_TIMEOUT_S) or "").strip().splitlines()
    except Exception:
        return "retry"
    rid = lines[0].strip() if lines else ""
    if not rid.isdigit():
        return "retry"
    try:
        m = request_metadata(rid)
    except Exception:
        m = {}
    if not m or m.get("status") in ("destroyed", "failed"):
        return "dead"
    return "ok"


def _pepper_feed_once():
    """One pepper tick. Returns a short reason (handy in tests and the console)."""
    if not _pepper_on():
        return "off"
    if _stage_live():
        return "live"
    if not os.path.exists(PEPPER_DECK):
        return "nodeck"
    on = rds.get("mood:on") == b"1"
    if (on and rds.llen("mood:list") > 0) or _capt_running(on) or _capt_head_pn():
        return "captain"                         # the captain's line has the air; pepper waits
    last = rds.get("pepper:last_ts")
    if last is None:
        rds.set("pepper:last_ts", str(time.time()))
        return "counting"
    if _pepper_rot_since(float(last)) < _pepper_every():
        return "counting"
    if not _mood_lane_ok():
        return "nolane"
    with _mood_lock:
        try:
            orid, ofn, since = _capt_clock()
            if [r for r in _mood_queue_rids() if r != orid]:
                return "queued"                  # something already waits in the mood lane
            if queue_rids():
                return "requests"
        except Exception:
            return "unknown"
        dur = _mood_duration(ofn) if (orid and ofn) else 0
        if since and dur and (dur - (time.time() - since)) > _MOOD_LOOKAHEAD_S:
            return "waiting"                     # ~25 s before the end of the song on air
        item = _pepper_next()
        if item is None:
            return "empty"
        r = _pepper_push(item)
        if r == "retry":
            rds.decr("pepper:xpos" if item["pile"] == "xmas" else "pepper:pos")   # try the same card next tick
            return "pushfail"
        now = time.time()
        if r == "ok":
            try:
                st = _pepper_db()
                st.execute("INSERT OR REPLACE INTO played VALUES (?,?,?)", (item["key"], item["path"], int(now)))
                st.commit()
                st.close()
            except Exception as e:
                print("pepper: could not record %s: %s" % (item["path"], e), flush=True)
            pipe = rds.pipeline()
            pipe.set("pepper:last_ts", str(now))
            pipe.incr("pepper:n")
            pipe.lpush("pepper:log", json.dumps({"path": item["path"], "artist": item["artist"], "title": item["title"],
                                                 "dur": item["dur"], "ts": int(now), "xmas": item["pile"] == "xmas"}))
            pipe.ltrim("pepper:log", 0, 29)
            pipe.lpush("pepper:paths", item["path"])
            pipe.ltrim("pepper:paths", 0, 4)
            pipe.execute()
            return "pushed"
        return "dead"                            # an unplayable file: its card is passed, the next tick tries the next one


def _pepper_feeder():
    while True:
        time.sleep(_MOOD_TICK_S)
        try:
            _pepper_feed_once()
        except Exception as e:
            try:
                print("pepper feeder: %s" % e, flush=True)
            except Exception:
                pass


def _pepper_is(path):
    """The site's 🌶 mark: this file was handed over as a pepper song recently."""
    try:
        return bool(path) and path.encode() in rds.lrange("pepper:paths", 0, 4)
    except Exception:
        return False


def _pepper_state():
    out = {"on": rds.get("pepper:on") != b"0", "brake_file": os.path.exists(PEPPER_OFF), "every": _pepper_every(),
           "live": _stage_live(), "n": int(rds.get("pepper:n") or 0), "xmas_now": _pepper_xmas_now(), "log": []}
    try:
        meta = _pepper_deck_meta()
        out.update(deck_size=int(meta.get("size") or 0), xmas_size=int(meta.get("xmas") or 0),
                   built=int(meta.get("built") or 0), pos=int(rds.get("pepper:pos") or 0))
    except Exception:
        out.update(deck_size=0, xmas_size=0, built=0, pos=0)
    try:
        last = float(rds.get("pepper:last_ts") or 0)
        out["since"] = _pepper_rot_since(last) if last else 0
    except Exception:
        out["since"] = 0
    for raw in rds.lrange("pepper:log", 0, 9):
        try:
            it = json.loads(raw)
            it["in_rotation"] = it.get("path") in _rot_set()
            out["log"].append(it)
        except Exception:
            pass
    try:
        orid, ofn, _s = _capt_clock()
        out["now_pepper"] = bool(ofn) and _pepper_is(ofn)
    except Exception:
        out["now_pepper"] = False
    return out


def _pepper_never(path):
    """The captain's "Not again": this song never comes back as a pepper. Returns (ok, message)."""
    import sqlite3 as _sq, unicodedata
    con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    row = con.execute("SELECT artist, title FROM tracks WHERE path=?", (path,)).fetchone()
    con.close()
    if not row:
        return False, "not a library song"
    a = unicodedata.normalize("NFKD", row[0] or "").encode("ascii", "ignore").decode().lower()
    t = unicodedata.normalize("NFKD", row[1] or "").encode("ascii", "ignore").decode().lower()
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", re.sub(r"\(.*?\)|\[.*?\]", " ", s)).strip()
    key = "%s\x00%s" % (norm(a), norm(t)) if norm(t) else "path\x00" + path
    st = _pepper_db()
    st.execute("INSERT OR REPLACE INTO never VALUES (?,?,?)", (key, path, int(time.time())))
    st.commit()
    st.close()
    return True, "%s -- never again as a pepper" % " - ".join(x for x in row if x)


# ---- nr_sets (2026-10-31): LISTENER SETS -- 7, 14 or 21 songs, in the listener's order, back to back ---------------
# A listener's idea ("10 nummers kiezen en 1x betalen"), SF's rulings 2026-10-30/31 (captain's log):
#   sizes 7/14/21; library songs only; price = the long-song curve on the set's TOTAL minutes (_play_price), or the
#   singles' flat price times the size when length pricing is off; ONE set per phone/browser per day; only while the
#   request line is quiet (2 or fewer waiting); never during a live session; one set at a time; the 24-hour repeat
#   check runs BEFORE the invoice; one bot post per set (every song carries the same req_id, and the announcers post
#   once per req_id); the captain's switch stops NEW sets, a paid set always plays.
# How it holds the air: once paid, ALL its songs go into the paid queue at once, in order, under the queue lock -- so
# nothing can land between them (paid singles that come later wait behind the set and see its countdown). Pepper, the
# captain's line and jingles already yield to paid songs. A live session that opens mid-set pauses it (liquidsoap
# holds the queue); the rest plays after the session.
# redis (db 2): sets:on "1"/"0" | set:pending:<payment_hash> (json, like the single plays) | set:active (json, until
#   its last song has aired) | set:cid:<cid> (24 h) | set:used (count)
SET_SIZES = (7, 14, 21)
SET_QUIET_MAX = 2
_set_cache = {"t": 0.0, "v": None}


def _sets_on():
    return rds.get("sets:on") != b"0"


def _set_pending_key(ph):
    return "set:pending:%s" % ph


def _set_recent_keys():
    """(artist, title) casefolded of EVERY airing in the last 24 h (any lane): a set never repeats today's songs."""
    import datetime as _dt
    keys = set()
    try:
        cutoff = time.time() - 24 * 3600
        with open(PLAYLOG_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 700_000))
            chunk = f.read().replace(b"\x00", b"").decode("utf-8", "replace")
        for line in chunk.split("\n")[1:]:
            parts = line.rstrip("\r").split("\t")
            if len(parts) != 6 or parts[1] != "play":
                continue
            try:
                if _dt.datetime.fromisoformat(parts[0]).timestamp() >= cutoff:
                    keys.add(_mood_key(parts[4], parts[5]))
            except Exception:
                continue
    except Exception:
        pass
    return keys


def _set_active():
    """The set that holds (or will hold) the air, with its progress -- or None. Cached 3 s.
    {id, n, who, i (1-based song on air, 0 = not started), left_s, songs_left, paused}"""
    now = time.time()
    if now - _set_cache["t"] < 3:
        return _set_cache["v"]
    out = None
    try:
        raw = rds.get("set:active")
        if raw:
            rec = json.loads(raw)
            rids = [s.get("rid") for s in rec.get("songs", []) if s.get("rid")]
            try:
                waiting = set(queue_rids())
                orid, ofn, since = _capt_clock()
            except Exception:
                waiting, orid, ofn, since = None, None, "", None
            if waiting is None:
                out = _set_cache["v"]                     # the radio did not answer: keep what we knew
            else:
                left, i, songs_left = 0.0, 0, 0
                for k, s in enumerate(rec.get("songs", [])):
                    if not s.get("rid"):
                        continue
                    if s["rid"] == orid:
                        i = k + 1
                        el = (now - since) if since else 0
                        left += max(0.0, float(s.get("dur") or 0) - el)
                        songs_left += 1
                    elif s["rid"] in waiting:
                        left += float(s.get("dur") or 0)
                        songs_left += 1
                if songs_left == 0 and now - float(rec.get("queued_at") or 0) > 60:
                    rds.delete("set:active")              # its last song has aired
                else:
                    out = {"id": rec.get("id"), "n": len(rids), "who": rec.get("who") or "", "i": i,
                           "left_s": int(left), "songs_left": songs_left, "paused": _stage_live()}
    except Exception:
        out = None
    _set_cache.update(t=now, v=out)
    return out


def _set_mark(rid_raw, uri, set_id, k, n, note, who, via, full):
    """Like _mark_paid_rid, but every song of the set shares ONE req_id (the announcers post once per req_id) and only
    the first song carries the listener's note."""
    try:
        rid = (rid_raw or "").strip().splitlines()[0].strip() if (rid_raw or "").strip() else ""
        if not rid.isdigit():
            return ""
        rds.setex("jukebox:paid_rid:%s" % rid, 6 * 3600,
                  json.dumps({"uri": uri or "", "note": _clean_note(note, full) if k == 0 else "", "lane": "set",
                              "who": _clean_who(who), "via": _clean_via(via), "full": bool(full),
                              "ts": int(time.time()), "req_id": "set-" + set_id, "kind": "set",
                              "set_note": _clean_note(note, full),   # nr_sets3: the set's shoutout rides on every song
                              "set": {"id": set_id, "k": k + 1, "n": n}}))
        return rid
    except Exception:
        return ""


def _set_queue(ph):
    """Queue a PAID set: every song, in order, under the queue lock. Resumable (song by song). (ok, message, wait)."""
    global _last_play_at
    with _queue_lock:
        raw = rds.get(_set_pending_key(ph))
        if raw is None:
            return False, "expired", 0
        rec = json.loads(raw)
        if rec.get("queued"):
            return True, rec.get("message", "Already queued"), 0
        now = time.time()
        if rec.get("pushed", 0) == 0 and now - _last_play_at < PLAY_COOLDOWN_S:
            return False, "", max(1, int(PLAY_COOLDOWN_S - (now - _last_play_at)) + 1)
        songs = rec["songs"]
        n = len(songs)
        for k in range(int(rec.get("pushed") or 0), n):
            s = songs[k]
            rid = ""
            if os.path.exists(s["path"]):
                try:
                    rid_raw = _push_slow(s["path"], rights_class=s.get("rights") or "collecting_society", source="local",
                                         artist=s.get("artist") or "", title=s.get("title") or "")
                    if _rid_resolved(rid_raw) is not False:
                        rid = _set_mark(rid_raw, s["path"], rec["id"], k, n, rec.get("note") or "", rec.get("who") or "",
                                        "web", False)
                except Exception as e:
                    logging.error("nr_sets: song %d of set %s not pushed: %s", k + 1, rec["id"], e)
            if not rid:
                logging.error("nr_sets: song %d of set %s skipped (file missing or dropped): %s", k + 1, rec["id"], s["path"])
            s["rid"] = rid
            rec["pushed"] = k + 1
            rds.setex(_set_pending_key(ph), PENDING_RECORD_TTL_S, json.dumps(rec))
        got = sum(1 for s in songs if s.get("rid"))
        rec["queued"] = True
        rec["message"] = "Your set is queued: %d songs, in your order." % got + (
            "" if got == n else " %d could not be opened and were skipped -- tell the captain." % (n - got))
        rds.setex(_set_pending_key(ph), PENDING_RECORD_TTL_S, json.dumps(rec))
        total = sum(int(s.get("dur") or 0) for s in songs)
        rds.setex("set:active", total + 4 * 3600, json.dumps({"id": rec["id"], "who": rec.get("who") or "",
                                                              "queued_at": now, "songs": [{"rid": s.get("rid", ""), "dur": s.get("dur", 0)} for s in songs]}))
        if rec.get("cid"):
            rds.setex("set:cid:%s" % rec["cid"], 24 * 3600, rec["id"])
        rds.incr("set:used")
        _set_cache["t"] = 0.0
        _last_play_at = now
        return True, rec["message"], 0


def _set_sweeper():
    """The safety net, as for single plays: a paid set airs even when nobody watches the page any more."""
    while True:
        time.sleep(20)
        try:
            for key in rds.scan_iter(match="set:pending:*", count=100):
                try:
                    raw = rds.get(key)
                    if not raw:
                        continue
                    rec = json.loads(raw)
                    if rec.get("queued"):
                        continue
                    ph = (key.decode() if isinstance(key, bytes) else key).split(":", 2)[2]
                    if time.time() - float(rec.get("minted_at") or 0) > PENDING_PLAY_TTL_S + 3600:
                        continue                          # long past its invoice: LNbits is not asked every 20 s
                    if _is_play_invoice_paid(ph):
                        _set_queue(ph)
                except Exception as e:
                    logging.error("nr_sets sweeper: %s", e)
        except Exception as e:
            logging.error("nr_sets sweeper scan: %s", e)


def _set_info():
    """Public: may a set be bought right now, and what is playing."""
    try:
        waiting = len(queue_rids())
    except Exception:
        waiting = 99
    act = _set_active()
    live = _stage_live()
    why = ""
    if not _sets_on():
        why = "off"
    elif live:
        why = "live"
    elif act:
        why = "busy"
    elif waiting > SET_QUIET_MAX:
        why = "queue"
    return {"on": _sets_on(), "open": not why, "why": why, "sizes": list(SET_SIZES), "waiting": waiting,
            "pricing": "length" if _duration_pricing_on() else "flat", "flat_sats": PLAY_PRICE_SATS, "active": act}


def _set_price(total_s, n):
    return _play_price(total_s) if _duration_pricing_on() else PLAY_PRICE_SATS * n


# --- nr_freeplay (2026-10-17, SF: "a quick hidden page I can access via the captains console, it should have a QR code
# and when random people scan that QR code they should be sent to a unique site that gives them one free jukebox play.
# Of course I should be able to enable and disable that function on the fly as it can be misused"): the boombox walk.
# The captain's page /captain-freeplay.html (console login) switches it ON with a budget of plays and shows a QR that
# changes every minute: a photo of it posted online dies within ~3 minutes. A phone that scans it gets its OWN one-play
# code (3 h). The play is the GIFT road: ship library only, no invoice, no sats, no balance, no ledger -- a free play
# given outright, never credit (SF's no-credit law). OFF kills the QR and every unused code at once; so does a new ON.
# v2 (2026-10-19, SF after the first walk: "set more than just 1 free play per person ... one may not be sufficient"):
# the captain sets the plays per phone (1-5); a code carries that number from the moment it is made. Free plays get
# their own lane "free" (the captain's own gifts keep "gift"), so the Telegram, X and Nostr posts treat them as
# ordinary requests, not as the captain's gift.
FREEPLAY_SLOT_S = 60            # the QR's token changes every minute
FREEPLAY_SLOTS_OK = 3           # this minute and the two before: time to scan, load and claim
FREEPLAY_CODE_TTL_S = 3 * 3600  # a phone's one-play code lasts three hours
FREEPLAY_LEFT_MAX = 420
FREEPLAY_LEFT_DEFAULT = 21
FREEPLAY_PER_MAX = 5           # plays per phone, set by the captain (1 = the first walk's rule)
FREEPLAY_IP_HOUR = 12           # claims per network address per hour (phones at an event share mobile-carrier addresses)
_FP_CODE_RE = re.compile(r"^[0-9a-f]{12}$")


def _fp_get(key):
    v = rds.get(key)
    return v.decode() if isinstance(v, bytes) else v


def _fp_secret():
    """the walk's secret while ON, else None"""
    return _fp_get("freeplay:secret") if _fp_get("freeplay:on") == "1" else None


def _fp_walk(secret):
    """the walk's public id: names the walk on codes and on a phone's "had mine" mark, never the secret itself"""
    return hashlib.sha256(("walk:" + secret).encode()).hexdigest()[:8]


def _fp_token(secret, slot):
    return hmac.new(secret.encode(), ("slot:%d" % slot).encode(), hashlib.sha256).hexdigest()[:12]


def _fp_int(key):
    try:
        return max(0, int(_fp_get(key) or 0))
    except Exception:
        return 0


def _fp_state():
    secret = _fp_secret()
    out = {"on": bool(secret), "left": _fp_int("freeplay:left"), "given": _fp_int("freeplay:given"),
           "per": max(1, min(FREEPLAY_PER_MAX, _fp_int("freeplay:per") or 1)),
           "slot_s": FREEPLAY_SLOT_S, "live": _load_session() is not None}
    if secret:
        now = time.time()
        out["qr_url"] = SITE_URL + "/?jukebox&fp=" + _fp_token(secret, int(now // FREEPLAY_SLOT_S))
        out["next_in"] = int(FREEPLAY_SLOT_S - now % FREEPLAY_SLOT_S) + 1
    return out


def _fp_switch(action, n, per=0):
    if per:
        rds.set("freeplay:per", max(1, min(FREEPLAY_PER_MAX, per)))   # counts for codes made from now on
    if action == "on":
        n = max(1, min(FREEPLAY_LEFT_MAX, n or FREEPLAY_LEFT_DEFAULT))
        rds.set("freeplay:secret", _secrets.token_hex(16))
        rds.set("freeplay:left", n)
        rds.set("freeplay:given", 0)
        rds.set("freeplay:on", "1")
    elif action == "off":
        rds.delete("freeplay:on", "freeplay:secret")
    elif action == "left":
        rds.set("freeplay:left", max(0, min(FREEPLAY_LEFT_MAX, n)))
    else:
        raise ValueError("action must be on, off or left")
    return _fp_state()


def _fp_code(code):
    code = (code or "").strip().lower()
    if not _FP_CODE_RE.match(code):
        return None
    raw = _fp_get("freeplay:code:" + code)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except Exception:
        return None
    rec["n"] = max(1, min(FREEPLAY_PER_MAX, int(rec.get("n") or 1)))
    rec["spent"] = _fp_int("freeplay:used:" + code)
    rec["used"] = rec["spent"] >= rec["n"]
    return rec


def _fp_code_status(code):
    rec, secret = _fp_code(code), _fp_secret()
    if not rec:
        return {"status": "gone"}
    if not secret or rec.get("s") != _fp_walk(secret):
        return {"status": "off"}
    if rec["used"]:
        return {"status": "used"}
    return {"status": "open", "expires_in": max(0, int(rds.ttl("freeplay:code:" + code.strip().lower()) or 0)),
            "left": _fp_int("freeplay:left"), "plays": rec["n"] - rec["spent"]}


def _fp_claim(token, have, ip, done=""):
    """a phone's scan -> (http, payload): the token from the QR becomes the phone's own one-play code. done = the walk
    this phone already had its free play on (kept in its browser): a polite brake on rescans, the budget is the real one"""
    secret = _fp_secret()
    if not secret:
        return 403, {"code": "off", "error": "Free plays are closed right now."}
    if done and done.strip().lower() == _fp_walk(secret):
        return 409, {"code": "had", "error": "You already had your free play on this walk."}
    if have:
        st = _fp_code_status(have)
        if st["status"] == "open":                     # the same phone scanning twice keeps its one code
            return 200, {"code": have.strip().lower(), "expires_in": st["expires_in"], "again": True, "walk": _fp_walk(secret),
                         "plays": st["plays"]}
    slot = int(time.time() // FREEPLAY_SLOT_S)
    token = (token or "").strip().lower()
    if not any(hmac.compare_digest(token, _fp_token(secret, slot - k)) for k in range(FREEPLAY_SLOTS_OK)):
        return 410, {"code": "stale", "error": "This QR has moved on. Scan the captain's QR again."}
    if _fp_int("freeplay:left") <= 0:
        return 410, {"code": "empty", "error": "All free plays are given away for now."}
    iph = hashlib.sha256((secret + "|" + (ip or "")).encode()).hexdigest()[:16]   # never the address itself
    n = rds.incr("freeplay:ip:" + iph)
    if int(n) == 1:
        rds.expire("freeplay:ip:" + iph, 3600)
    if int(n) > FREEPLAY_IP_HOUR:
        return 429, {"code": "rate", "error": "Lots of free plays from this network already. Try again later."}
    code, per = _secrets.token_hex(6), max(1, min(FREEPLAY_PER_MAX, _fp_int("freeplay:per") or 1))
    rds.setex("freeplay:code:" + code, FREEPLAY_CODE_TTL_S, json.dumps({"s": _fp_walk(secret), "made": int(time.time()), "n": per}))
    return 200, {"code": code, "expires_in": FREEPLAY_CODE_TTL_S, "walk": _fp_walk(secret), "plays": per}


def _fp_play(code, artist, title, resolve, push, live):
    """one free play -> (http, payload). The library row is found first; then one of the code's plays is taken
    (atomically, a counter); then one play comes off the budget; then the song is queued. Any failure after the take
    puts both back."""
    code = (code or "").strip().lower()
    secret, rec = _fp_secret(), _fp_code(code)
    if not secret or not rec or rec.get("s") != _fp_walk(secret):
        return 410, {"code": "off", "error": "This free play has expired or the free plays are closed."}
    if rec["used"]:
        return 410, {"code": "used", "error": "This free play is already used."}
    if live:
        return 409, {"code": "live", "error": "A live sesh is on -- the DJ has the deck. Your free play waits until it ends."}
    track, err = resolve(artist, title)
    if err:
        return 404, {"code": "track", "error": err}
    ukey = "freeplay:used:" + code
    spent = int(rds.incr(ukey))
    if spent == 1:
        rds.expire(ukey, FREEPLAY_CODE_TTL_S)
    if spent > rec["n"]:
        rds.decr(ukey)
        return 410, {"code": "used", "error": "This free play is already used."}
    left = int(rds.decr("freeplay:left"))
    if left < 0:
        rds.incr("freeplay:left")
        rds.decr(ukey)
        return 410, {"code": "empty", "error": "All free plays are given away for now."}
    try:
        push(track)
    except Exception as e:
        rds.incr("freeplay:left")
        rds.decr(ukey)
        return 500, {"code": "queue", "error": "Could not queue it (%s). Your free play is still yours." % str(e)[:80]}
    rds.incr("freeplay:given")
    return 200, {"queued": True, "artist": track.artist, "title": track.title, "left": max(0, left),
                 "mine_left": max(0, rec["n"] - spent)}


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
        if parsed.path == "/api/admin/i18n/status":    # nr_pageedit (2026-09-14): DeepL state + waiting translations
            qs = parse_qs(parsed.query)
            if not self._admin_authed((qs.get("token") or [""])[0]):
                return self._json(403, {"error": "not the captain"})
            try:
                return self._json(200, _i18n_status())
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        if parsed.path == "/api/search":
            return self._handle_search(parsed)
        if parsed.path.startswith("/api/browse/"):   # nr_catalog
            return self._handle_browse(parsed)
        if parsed.path.startswith("/api/label/"):    # nr_labels
            return self._handle_label(parsed)
        if parsed.path.startswith("/api/art/"):      # nr_covers
            return self._handle_art(parsed)
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
        if parsed.path == "/api/admin/pepper":           # nr_pepper (2026-10-31): the console panel
            if not self._admin_authed((parse_qs(parsed.query).get("token") or [""])[0]):
                return self._json(403, {"error": "not logged in"})
            return self._json(200, _pepper_state())
        if parsed.path == "/api/admin/rotation/info":
            return self._handle_admin_rotation_info(parsed)
        if parsed.path == "/api/admin/rotation/now":    # nr_rotbtn (2026-10-06): the homepage button
            return self._handle_admin_rotation_now(parsed)
        if parsed.path == "/api/admin/chat/recent":
            return self._handle_admin_chat_recent(parsed)
        if parsed.path == "/api/icecast-status":
            return self._handle_icecast_status()
        if parsed.path in ("/api/listen.m3u", "/api/listen.pls"):
            return self._handle_playlist(parsed)
        if parsed.path in ("/api/admin/mood", "/api/admin/mood/playlists", "/api/admin/mood/find", "/api/admin/mood/list"):   # nr_moodfind, nr_moodlist
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
            return self._handle_chat_recent(parsed)
        if parsed.path == "/api/chat/media":
            return self._handle_chat_media(parsed)
        if parsed.path == "/api/set/info":              # nr_sets (2026-10-31)
            return self._json(200, _set_info())
        if parsed.path == "/api/set/status":
            return self._handle_set_status(parsed)
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
        if parsed.path == "/api/blocks":                          # nr_cal: block heights for the calendar pill
            raw_ts = (parse_qs(parsed.query).get("ts") or [""])[0]
            return self._json(200, _blocks_at([int(x) for x in raw_ts.split(",") if x.strip().isdigit()][:60]))
        if parsed.path == "/api/donate/recent":
            return self._json(200, {"donations": _donate_recent()})
        if parsed.path == "/api/donate/ticker":                  # nr_cardband: the site card's gift band
            return self._json(200, _donate_ticker())
        if parsed.path == "/api/charts":                         # nr_charts: the top requests, counted from the play log
            cq = parse_qs(parsed.query)
            try:
                return self._json(200, _charts((cq.get("period") or [""])[0], _int0((cq.get("n") or [""])[0]),
                                               (cq.get("lane") or [""])[0]))
            except Exception as e:
                return self._json(500, {"error": "charts: %s" % str(e)[:200]})
        if parsed.path == "/api/admin/freeplay":                 # nr_freeplay: the captain's walk page
            if not self._admin_authed((parse_qs(parsed.query).get("token") or [""])[0]):
                return self._json(403, {"error": "not the captain"})
            try:
                return self._json(200, _fp_state())
            except Exception as e:
                return self._json(503, {"error": "freeplay: %s" % str(e)[:120]})
        if parsed.path == "/api/freeplay/code":                  # nr_freeplay: a phone checks its one-play code
            try:
                return self._json(200, _fp_code_status((parse_qs(parsed.query).get("c") or [""])[0]))
            except Exception as e:
                return self._json(503, {"error": "freeplay: %s" % str(e)[:120]})
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
            out["disc"] = _disc_url(t.get("filename") or "")     # nr_labels: a Great 78 record's own label
            out["cover"] = "" if out["disc"] else (_cover_url(t.get("filename") or "")
                                                   or _cover_url(t.get("initial_uri") or ""))   # nr_covers
            out["pepper"] = _pepper_is(t.get("filename") or "")     # nr_pepper: the site's mark
            mk = _paid_mark(t.get("rid"), t.get("filename") or "", t.get("initial_uri") or "")
            if mk:
                out["pepper"] = False
                out["requested"] = True
                out["lane"] = mk.get("lane") or "paid"
                out["kind"] = mk.get("kind") or ""          # nr_freeplay2: "free" = a free play from the boombox QR
                out["req_id"] = mk.get("req_id") or ""
                if _notes_on() and not _note_hidden(out["req_id"]):
                    out["note"] = _clean_note(mk.get("note") or "", bool(mk.get("full")))
                    out["who"] = _clean_who(mk.get("who") or "")
                    # nr_at: a surface shows the @ only if this says the line was typed there. An old
                    # record has no "via" and reads as "web", which every surface treats as not-mine.
                    out["note_via"] = _clean_via(mk.get("via") or "")
            if mk and isinstance(mk.get("set"), dict):   # nr_sets3: "SET 3/7" on the site, one honest bot post per set
                out["set"] = {"k": int(mk["set"].get("k") or 0), "n": int(mk["set"].get("n") or 0),
                              "note": (_clean_note(mk.get("set_note") or "", False)
                                       if (_notes_on() and not _note_hidden(out["req_id"])) else "")}
            return out
        out = {"now_playing": pub(now_playing), "queue": [pub(t) for t in queue]}
        # nr_timeleft (2026-10-30, a listener: "how long does this song still last?"): when the song on air started,
        # how long it is, and our clock -- the page counts down. Unknown start or length -> no fields, the page shows nothing.
        out["server_time"] = round(time.time(), 1)
        try:
            _sa = _set_active()                          # nr_sets: the set countdown (site + card)
            if _sa:
                out["set"] = {"i": _sa["i"], "n": _sa["n"], "left_s": _sa["left_s"], "paused": _sa["paused"]}
        except Exception:
            pass
        try:
            fn = (now_playing or {}).get("filename") or ""
            if fn and out["now_playing"] is not None:
                _c_rid, c_fn, c_start = _capt_clock()
                if c_start and c_fn == fn:
                    dur = _mood_duration(fn) or (_free_meta([fn]).get(fn) or ("", "", 0, ""))[2]
                    if dur and 0 <= time.time() - c_start < dur + 30:
                        out["now_playing"]["started"] = round(c_start, 1)
                        out["now_playing"]["duration_s"] = int(dur)
        except Exception:
            pass
        raw_wl = rds.get("wavlake:notice")
        if raw_wl is not None:
            try:
                out["wavlake"] = json.loads(raw_wl)
            except Exception:
                pass
        self._json(200, out)

    def _handle_history(self, parsed):
        """Public, read-only: the last N aired tracks from the play log. Exposes
        only ts/artist/title and nr_histreq's "req" (paid request / gift / none) -- NOT rights_class/source
        (that royalty attribution stays internal), never who asked."""
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
        # nr_searchdash (2026-10-12, SF: "can't find the current song"): "Comus - The Malgaard Suite" -- the lone "-"
        # was a word no song contains. A piece without a letter or digit is not a word.
        toks = [t for t in q.lower().split() if re.search(r"\w", t)][:6]
        if not toks:
            return self._json(200, {"results": []})
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

    def _handle_admin_rotation_now(self, parsed):
        """nr_rotbtn (2026-10-06): the song on air, for the CAPTAIN's rotation button on the homepage.
        state: in (in the rotation pool) | out (can be added) | cant (+ reason). Never public."""
        if not self._admin_authed((parse_qs(parsed.query).get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        if _stage_live():
            return self._json(200, {"state": "cant", "reason": "a live session is open", "title": "", "path": ""})
        try:
            rid = on_air_rid()
            if not rid:
                return self._json(200, {"state": "cant", "reason": "nothing on air", "title": "", "path": ""})
            m = request_metadata(rid)
            t = _track_summary(rid)
        except Exception as e:
            return self._json(502, {"error": "could not reach the radio: %s" % str(e)[:120]})
        path = (t.get("filename") or "").strip() or (t.get("initial_uri") or "").strip().split("?")[0]
        out = {"artist": t.get("artist") or "", "title": t.get("title") or "", "path": path}
        why = _rot_never(path, m)
        if why:
            out.update(state="cant", reason=why)
            return self._json(200, out)
        try:
            import sqlite3 as _sq
            con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            hit = con.execute("SELECT 1 FROM tracks WHERE path=?", (path,)).fetchone()
            con.close()
            if not hit:   # nr_rotfree (2026-10-29, SF): a CC & Public Domain song may join the rotation too
                fcon = _sq.connect(f"file:{FREE_DB}?mode=ro", uri=True)
                hit = fcon.execute("SELECT 1 FROM tracks WHERE path=?", (path,)).fetchone()
                fcon.close()
        except Exception:
            hit = None
        if not hit:
            out.update(state="cant", reason="not in the library index")
        elif path in _read_set("/var/lib/jukebox/blocklist.txt"):
            out.update(state="cant", reason="expelled (blocklist)")
        else:
            out["state"] = "in" if path in _rot_set() else "out"
        return self._json(200, out)

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
            if not path.startswith("/srv/"):
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
                # nr_aboard (2026-10-29, SF): one honest number -- every library FILE once (duplicate rows share a
                # sha256), plus the CC & Public Domain lane (free.db). The free lane is optional: no file = 0.
                con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
                lib = con.execute("SELECT COUNT(DISTINCT sha256) + SUM(sha256 IS NULL) FROM tracks").fetchone()[0] or 0
                con.close()
                free = 0
                try:
                    fcon = sqlite3.connect(f"file:{FREE_DB}?mode=ro", uri=True)
                    free = fcon.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] or 0
                    fcon.close()
                except Exception:
                    pass
                _STATS["lib"], _STATS["free"], _STATS["n"] = lib, free, lib + free
                _STATS["t"] = now
            except Exception as e:
                if not _STATS["n"]:
                    return self._json(502, {"error": f"index unavailable: {e}"})
        # nr_pricemode (2026-09-15): the pricing switch, for every reader of words (site, Nostr and Telegram help)
        self._json(200, {"tracks": _STATS["n"], "library": _STATS["lib"], "free": _STATS["free"],   # nr_aboard
                         "pricing": "length" if _duration_pricing_on() else "flat", "min_sats": PLAY_PRICE_SATS})

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
        if src == "free":   # nr_freelane: a random handful from the free lane only
            try:
                import sqlite3 as _sq
                con = _sq.connect(f"file:{FREE_DB}?mode=ro", uri=True)
                rows = con.execute("SELECT artist, title, album, duration_s FROM tracks ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
                con.close()
            except Exception as e:
                return self._json(502, {"error": f"free index unavailable: {e}"})
            return self._json(200, {"label": "CC & public domain", "results": [
                {"artist": a or "", "title": t or "", "album": al or "", "source": "free",
                 "duration_s": d, "sats": _play_price(d)} for a, t, al, d in rows]})
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
                "SELECT artist, title, album, duration_s FROM tracks "
                "WHERE title IS NOT NULL AND title != '' ORDER BY RANDOM() LIMIT ?",
                (n,),
            ).fetchall()
            con.close()
        except Exception as e:
            return self._json(502, {"error": f"library index unavailable: {e}"})
        # nr_sampleprice (2026-09-24, SF: /play random listed a 35-minute mix at 21 sats, the invoice said 2780):
        # a sampled row carries its length and its price exactly like a search hit, from the same _play_price the
        # invoice uses -- the Telegram bot and the site's "taste" list both fell back to 21 when these were missing.
        self._json(200, {"label": "A taste of what's aboard", "results": [
            {"artist": a or "", "title": t or "", "album": al or "", "source": "library",
             "duration_s": d, "sats": _play_price(d)} for a, t, al, d in rows
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

    def _handle_chat_recent(self, parsed=None):
        # nr_nostrchat (2026-10-03): ?src=all = the site's chat panel: Telegram AND the Nostr live chat of our zap.stream
        # page (nr-nostrchat -> /var/lib/jukebox/nostrchat.json), one list by time, rows marked src "tg" / "nostr".
        # Without it (the video overlay): Telegram only, exactly as before -- the broadcast picture does not change.
        want_all = parsed is not None and (parse_qs(parsed.query).get("src") or [""])[0] == "all"
        if telegram_chat is None and not want_all:
            return self._json(200, {"messages": []})
        # feed_hidden mirrors the captain's chat brake (the bridge's flag
        # file): the OVERLAY chat strip obeys it, the site chat tab ignores it
        msgs = telegram_chat.recent(50) if telegram_chat is not None else []
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
        if want_all:
            # the same ban list drops a Nostr name, short npub or hex key:  redis-cli -n 2 sadd chat:banned <npub or name>
            nost = [m for m in _nostr_chat_recent(50)
                    if not ({str(m.get("username") or ""), str(m.get("npub") or ""), str(m.get("pubkey") or "")} & banned)]
            msgs = [dict(m, src="tg") for m in msgs] + nost
            msgs.sort(key=lambda m: m.get("date") or 0)
            msgs = msgs[-60:]
        self._json(200, {"messages": msgs,
                         "feed_hidden": os.path.exists("/tmp/nr-chat-off")})

    def _handle_nostr_post(self):
        """nr_nostrpost (2026-10-03): the chat bar. The listener's Nostr browser extension signed the message (the key never
        leaves their browser); the station checks it and hands it to the relays, so their browser never connects to them.
        Only a live-chat message (kind 1311) for OUR zap.stream event, 1-280 characters, no links, signed within 10 min,
        valid id + signature, not on the blocklist or the chat ban list; 5 a minute per author, 10 per visitor."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 8192:
            return self._json(413, {"error": "too big", "code": "other"})
        try:
            ev = json.loads(self.rfile.read(length).decode("utf-8")).get("event") or {}
            kind, content, tags, ca = ev["kind"], ev["content"], ev["tags"], int(ev["created_at"])
            shape = (kind == 1311 and isinstance(content, str) and isinstance(tags, list)
                     and re.fullmatch(r"[0-9a-f]{64}", str(ev["pubkey"])) and re.fullmatch(r"[0-9a-f]{64}", str(ev["id"]))
                     and re.fullmatch(r"[0-9a-f]{128}", str(ev["sig"])))
        except Exception:
            shape = False
        if not shape:
            return self._json(400, {"error": "not a Nostr chat message", "code": "other"})
        if not any(isinstance(t, list) and len(t) >= 2 and t[0] == "a" and t[1] == _NOSTRPOST_A for t in tags):
            return self._json(400, {"error": "not for this station's chat", "code": "other"})
        text = content.strip()
        if not text or len(content) > 280:
            return self._json(400, {"error": "1 to 280 characters", "code": "other"})
        if abs(time.time() - ca) > 600:
            return self._json(400, {"error": "signed too long ago (or the clock is off)", "code": "other"})
        if re.search(r"https?://|www\.|lnurl1|lnbc[0-9]", content, re.I):
            return self._json(400, {"error": "no links here, please", "code": "links"})
        try:
            lib, ann = _nostrpost_libs()
        except Exception:
            return self._json(503, {"error": "Nostr is not available right now", "code": "other"})
        ser = json.dumps([0, ev["pubkey"], ca, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
        try:
            good_sig = (hashlib.sha256(ser.encode("utf-8")).hexdigest() == ev["id"]
                        and lib.schnorr_verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"])))
        except Exception:
            good_sig = False
        if not good_sig:
            return self._json(400, {"error": "the signature does not check out", "code": "other"})
        try:
            if ann.blocked(content, ann.load_blocklist()):
                return self._json(400, {"error": "blocked", "code": "blocked"})
        except Exception:
            pass
        try:
            n = lib.bech32_encode("npub", bytes.fromhex(ev["pubkey"]))
            short = n[:10] + "\u2026" + n[-4:]
        except Exception:
            short = ""
        try:
            banned = {b.decode() if isinstance(b, bytes) else str(b) for b in rds.smembers("chat:banned")}
        except Exception:
            banned = set()
        if ev["pubkey"] in banned or (short and short in banned):
            return self._json(403, {"error": "blocked", "code": "blocked"})
        ip = (self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
              or self.client_address[0])
        if not _nostrpost_rate("pk:" + ev["pubkey"], 5) or not _nostrpost_rate("ip:" + ip, 10):
            return self._json(429, {"error": "slow down a little", "code": "rate"})
        clean = {k: ev[k] for k in ("id", "pubkey", "created_at", "kind", "tags", "content", "sig")}
        try:
            results = lib.publish(clean, _NOSTRPOST_RELAYS)
        except Exception:
            results = []
        good = sum(1 for _, ok, _ in results if ok)
        if not good:
            return self._json(502, {"error": "no Nostr relay accepted it right now", "code": "other"})
        logging.info("nostr chat post %s from %s: %d/%d relays", ev["id"][:12], short or "?", good, len(results))
        return self._json(200, {"ok": True, "id": ev["id"], "accepted": good, "relays": len(results)})

    def _handle_art(self, parsed):
        """nr_covers: GET /api/art/<key>?s=t (96 px) | m (400 px). Public; a Wavlake id, or a key this API handed out."""
        key = parsed.path.rsplit("/", 1)[-1]
        if not _COVER_KEY.match(key):
            return self._json(404, {"error": "no cover"})
        t, m = _cover_files(key)
        if os.path.exists(m[:-4] + ".blocked"):
            return self._json(404, {"error": "no cover"})
        fn = t if (parse_qs(parsed.query).get("s") or ["m"])[0] == "t" else m
        if not os.path.exists(fn) and not _cover_make(key):
            return self._json(404, {"error": "no cover"})
        with open(fn, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_label(self, parsed):
        """nr_labels: GET /api/label/<Great 78 id>?s=t (96 px) | m (400 px). Public; only identifiers we carry."""
        ident = parsed.path.rsplit("/", 1)[-1]
        size = "t" if (parse_qs(parsed.query).get("s") or ["m"])[0] == "t" else "m"
        _disc_map()
        if not _DISC_ID.match(ident) or ident not in _DISC["ids"]:
            return self._json(404, {"error": "no label for that record"})
        fn = os.path.join(_DISC_DIR, size, ident + ".jpg")
        if not os.path.exists(fn) and not _disc_fetch(ident, size, fn):
            return self._json(404, {"error": "the label is not available right now"})
        with open(fn, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=2592000, immutable")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _browse_all(self, what, one):
        """nr_allcat (2026-10-24, SF: "all field lanes should be selected from the get go"): the catalog's default view over
        the ship library plus, when the page asks for it (lanes=library,free -- the free lane is still private), the CC &
        public domain lane. Wavlake joins from the page itself (a second call per artist / filter), so a slow Wavlake never
        holds up the station's own lists. Every row keeps its own lane (source), so its play takes that lane's road."""
        lanes = [x for x in one("lanes", 30).lower().split(",") if x in ("library", "free")] or ["library"]
        try:
            off = max(0, min(int(one("offset", 7) or 0), 200000))
        except ValueError:
            off = 0
        if what == "loose":                                   # nr_catbox2
            return self._json(200, _cat_loose(_cat_index_all(lanes), lanes, one("letter", 1).upper() or "A", off))
        if what == "artists":
            idx = _cat_index_all(lanes)
            q = _cat_key(one("q", 60))
            loose = []
            if q:
                keys = _cat_find(idx, q)
            else:
                letter = (one("letter", 1).upper() or "A")
                keys = [k for k in idx["order"] if _cat_letter(idx["groups"][k]["name"]) == letter]
                # nr_catbox: one-song names wait in their letter's "needs sorting" box
                loose = [k for k in keys if idx["groups"][k]["n"] <= 1]
                keys = loose if one("box", 8) == "loose" else [k for k in keys if idx["groups"][k]["n"] > 1]
            page = keys[off:off + 300]
            return self._json(200, {"total": len(keys), "offset": off, "letters": idx["letters"], "loose": 0 if q else len(loose),
                                    "artists": [{"name": idx["groups"][k]["name"], "n": idx["groups"][k]["n"],
                                                 "others": idx["groups"][k]["others"]} for k in page]})
        if what == "artist":
            k = _cat_key(one("name"))
            name, albums, others, n = "", [], [], 0
            for lane in lanes:
                g = _cat_index(lane)["groups"].get(k)
                if not g:
                    continue
                name = name or g["name"]
                own = {}
                for a, t, al, d, p in _cat_artist_rows(lane, g):
                    n += 1
                    if _cat_own(_cat_index(lane), a) == k:   # nr_catclean + nr_catthe: folded spellings count as the artist's own
                        own.setdefault(al, []).append(_cat_row(lane, a, t, al, d, p))
                    else:
                        others.append(_cat_row(lane, a, t, al, d, p))
                albums += [{"album": al, "tracks": tr} for al, tr in own.items()]
            if not name:
                return self._json(404, {"error": "no such artist"})
            return self._json(200, {"name": name, "n": n, "albums": albums, "others": others})
        if what == "recent":
            rows = [dict(r, lane="library") for r in _cat_recent("library")]
            return self._json(200, {"offset": off, "total": len(rows), "albums": rows[off:off + 60]})
        return self._json(404, {"error": "unknown browse view"})

    def _browse_wavlake(self, what, one):
        """nr_wavcat (2026-10-24, SF: "can we add wavlake to it?" -- "not to download, just to select a tune"): the catalog's
        Wavlake view, live from Wavlake's public API. Wavlake shares no full artist list, so the left list is "top on
        Wavlake" (the artists in the jukebox's Wavlake deck: the sats rankings over 1/7/30/90 days) or a search of all of
        Wavlake; an artist shows its newest albums. Rows carry the track's guid, so a tap takes the normal V4V road."""
        price = WAVLAKE_PLAY_PRICE_SATS
        def row(t):
            return {"artist": t.artist or "", "title": t.title or "", "album": t.album or "", "duration_s": int(t.duration_s or 0),
                    "sats": price, "source": "wavlake", "guid": t.source_uri or "",
                    "cover": ("/api/art/w" + t.source_uri.lower()) if _WL_ID.match(t.source_uri or "") else ""}   # nr_covers
        def search(term):
            data = wavlake._get_json("/content/search?term=" + requests.utils.quote(term), 8, 120)
            items = data if isinstance(data, list) else ((data or {}).get("data") or [])
            return [x for x in items if isinstance(x, dict)]
        if what == "artists":
            q = one("q", 60)
            names = {}
            if q:
                for it in search(q):
                    n = ((it.get("name") if it.get("type") == "artist" else it.get("artist")) or "").strip()
                    if n:
                        names[n] = names.get(n, 0) + (5 if it.get("type") == "artist" else 1)
            else:
                _wavlake_top(1)                                    # fills / refreshes the deck (cached 15 min)
                for r in (_WAV_TOP.get("rows") or []):
                    n = (r.get("artist") or "").strip()
                    if n:
                        names[n] = names.get(n, 0) + 1
            order = sorted(names, key=lambda n: (-names[n], n.casefold()))[:300]
            return self._json(200, {"total": len(order), "offset": 0, "letters": {}, "top": not q,
                                    "artists": [{"name": n, "n": names[n] if not q else 0, "others": 0} for n in order]})
        if what == "artist":
            name = one("name")
            hits = [x for x in search(name) if x.get("type") == "artist" and x.get("id")]
            hit = next((x for x in hits if (x.get("name") or "").strip().casefold() == name.strip().casefold()), hits[0] if hits else None)
            if not hit:
                return self._json(404, {"error": "no such artist on Wavlake"})
            d = wavlake._get_json("/content/artist/" + hit["id"], 8, wavlake.CACHE_PAGE_S)
            if isinstance(d, list):
                d = d[0] if d else {}
            albums = [x for x in ((d or {}).get("albums") or []) if isinstance(x, dict) and x.get("id")]
            albums.sort(key=lambda x: str(x.get("releaseDate") or ""), reverse=True)
            out, n = [], 0
            deadline = time.monotonic() + 20                       # Wavlake punishes bursts: newest 10, one by one
            for al in albums[:10]:
                tracks, _ok = wavlake._album_tracks(al["id"], d.get("name") or name, deadline)
                if tracks:
                    out.append({"album": al.get("title") or tracks[0].album or "", "tracks": [row(t) for t in tracks]})
                    n += len(tracks)
            return self._json(200, {"name": d.get("name") or hit.get("name") or name, "n": n, "albums": out, "others": []})
        return self._json(404, {"error": "not in the Wavlake view"})

    def _browse_decades(self, what, one):
        """nr_catdecade (2026-10-29): the CC & Public Domain lane by recording year (free_meta.year, nr-freelane5).
        decades -> [{decade, n}] (cached 10 min); decade?d=1920&offset=N -> 200 songs, oldest year first."""
        now = time.time()
        if what == "decades":
            with _CAT_LOCK:
                c = _CAT.get(("decades",))
            if not c or now - c["t"] > _CAT_TTL:
                con = sqlite3.connect(f"file:{FREE_DB}?mode=ro", uri=True)
                try:
                    rows = con.execute("SELECT MAX(1890, (m.year / 10) * 10), COUNT(*) FROM free_meta m JOIN tracks t ON t.path = m.path "
                                       "WHERE m.year IS NOT NULL AND t.title IS NOT NULL AND trim(t.title) != '' GROUP BY 1 ORDER BY 1").fetchall()
                finally:
                    con.close()
                c = {"t": now, "rows": [{"decade": int(d), "n": n} for d, n in rows]}
                with _CAT_LOCK:
                    _CAT[("decades",)] = c
            return self._json(200, {"decades": c["rows"], "total": sum(r["n"] for r in c["rows"])})
        try:
            d = int(one("d", 4) or 0)
            off = max(0, min(int(one("offset", 7) or 0), 400000))
        except ValueError:
            return self._json(400, {"error": "bad decade"})
        if d < 1890 or d > 2020 or d % 10:
            return self._json(400, {"error": "bad decade"})
        lo, hi = (1880 if d == 1890 else d), d + 9
        con = sqlite3.connect(f"file:{FREE_DB}?mode=ro", uri=True)
        try:
            total = con.execute("SELECT COUNT(*) FROM free_meta m JOIN tracks t ON t.path = m.path WHERE m.year BETWEEN ? AND ? "
                                "AND t.title IS NOT NULL AND trim(t.title) != ''", (lo, hi)).fetchone()[0]
            rows = con.execute("SELECT t.artist, t.title, COALESCE(t.album, ''), COALESCE(t.duration_s, 0), t.path, m.year "
                               "FROM free_meta m JOIN tracks t ON t.path = m.path WHERE m.year BETWEEN ? AND ? "
                               "AND t.title IS NOT NULL AND trim(t.title) != '' "
                               "ORDER BY m.year, t.artist COLLATE NOCASE, t.title COLLATE NOCASE LIMIT 200 OFFSET ?", (lo, hi, off)).fetchall()
        finally:
            con.close()
        tracks = []
        for ar, ti, al, du, p, y in rows:
            r = _cat_row("free", ar, ti, al, du, p)
            r["year"] = int(y)
            tracks.append(r)
        return self._json(200, {"decade": d, "total": total, "offset": off, "next": off + len(tracks), "tracks": tracks})

    def _handle_browse(self, parsed):
        """nr_catalog: GET /api/browse/artists | artist | recent | album  (lane=library|free). Public, read-only."""
        qs = parse_qs(parsed.query)
        def one(k, n=200):
            return (qs.get(k) or [""])[0].strip()[:n]
        if parsed.path.rsplit("/", 1)[-1] in ("decades", "decade"):   # nr_catdecade: always the CC & Public Domain lane
            try:
                return self._browse_decades(parsed.path.rsplit("/", 1)[-1], one)
            except Exception as e:
                logging.exception("decade browse failed")
                return self._json(503, {"error": "the decade list is not available right now (%s)" % str(e)[:60]})
        if one("lane", 10).lower() == "wavlake":          # nr_wavcat
            try:
                return self._browse_wavlake(parsed.path.rsplit("/", 1)[-1], one)
            except Exception as e:
                logging.exception("wavlake browse failed")
                return self._json(503, {"error": "Wavlake is not answering right now (%s)." % str(e)[:60]})
        if one("lane", 10).lower() == "all":              # nr_allcat
            return self._browse_all(parsed.path.rsplit("/", 1)[-1], one)
        lane = "free" if one("lane", 10).lower() == "free" else "library"
        what = parsed.path.rsplit("/", 1)[-1]
        try:
            off = max(0, min(int(one("offset", 7) or 0), 200000))
        except ValueError:
            off = 0
        try:
            if what == "loose":                               # nr_catbox2
                return self._json(200, _cat_loose(_cat_index(lane), [lane], one("letter", 1).upper() or "A", off))
            if what == "artists":
                idx = _cat_index(lane)
                q = _cat_key(one("q", 60))
                loose = []
                if q:
                    keys = _cat_find(idx, q)
                else:
                    letter = (one("letter", 1).upper() or "A")
                    keys = [k for k in idx["order"] if _cat_letter(idx["groups"][k]["name"]) == letter]
                    # nr_catbox: one-song names wait in their letter's "needs sorting" box
                    loose = [k for k in keys if idx["groups"][k]["n"] <= 1]
                    keys = loose if one("box", 8) == "loose" else [k for k in keys if idx["groups"][k]["n"] > 1]
                page = keys[off:off + 300]
                return self._json(200, {"total": len(keys), "offset": off, "letters": idx["letters"], "loose": 0 if q else len(loose),
                                        "artists": [{"name": idx["groups"][k]["name"], "n": idx["groups"][k]["n"],
                                                     "others": idx["groups"][k]["others"]} for k in page]})
            if what == "artist":
                idx = _cat_index(lane)
                k = _cat_key(one("name"))
                g = idx["groups"].get(k)
                if not g:
                    return self._json(404, {"error": "no such artist"})
                names = list(dict.fromkeys(g["variants"]))[:900]
                con = sqlite3.connect(f"file:{_cat_db(lane)}?mode=ro", uri=True)
                try:
                    rows = con.execute("SELECT artist, title, COALESCE(album, ''), COALESCE(duration_s, 0), path FROM tracks "
                                       "WHERE artist IN (%s) AND title IS NOT NULL AND trim(title) != '' "
                                       "ORDER BY COALESCE(album, ''), path LIMIT %d" % (",".join("?" * len(names)), _CAT_MAX_TRACKS * 3),
                                       names).fetchall()
                finally:
                    con.close()
                rows = _cat_dedupe(rows)[:_CAT_MAX_TRACKS]
                own, others = {}, []
                for a, t, al, d, p in rows:
                    if _cat_own(_cat_index(lane), a) == k:   # nr_catclean + nr_catthe: folded spellings count as the artist's own
                        own.setdefault(al, []).append(_cat_row(lane, a, t, al, d, p))
                    else:
                        others.append(_cat_row(lane, a, t, al, d, p))
                return self._json(200, {"name": g["name"], "n": len(rows),
                                        "albums": [{"album": al, "tracks": tr} for al, tr in own.items()],
                                        "others": others})
            if what == "recent":
                rows = _cat_recent(lane)
                return self._json(200, {"offset": off, "total": len(rows), "albums": rows[off:off + 60]})
            if what == "album":
                con = sqlite3.connect(f"file:{_cat_db(lane)}?mode=ro", uri=True)
                try:
                    rows = con.execute("SELECT artist, title, COALESCE(album, ''), COALESCE(duration_s, 0), path FROM tracks "
                                       "WHERE artist = ? AND COALESCE(album, '') = ? AND title IS NOT NULL AND trim(title) != '' "
                                       "ORDER BY path LIMIT ?", (one("artist"), one("album", 300), _CAT_MAX_TRACKS * 3)).fetchall()
                finally:
                    con.close()
                rows = _cat_dedupe(rows)[:_CAT_MAX_TRACKS]
                return self._json(200, {"artist": one("artist"), "album": one("album", 300),
                                        "tracks": [_cat_row(lane, a, t, al, d, p) for a, t, al, d, p in rows]})
            return self._json(404, {"error": "unknown browse view"})
        except Exception as e:
            logging.exception("browse failed")
            return self._json(503, {"error": "The catalog is busy (%s)." % str(e)[:60]})

    def _handle_search(self, parsed):
        qs = parse_qs(parsed.query)
        q = (qs.get("q") or [""])[0].strip()
        # src filter (SF 2026-07-30, the chips beside Shuffle): "" = both
        # lanes, "wavlake" = Wavlake only (wider window), "library" = ship
        # library only. Unknown values fall back to both lanes.
        src = (qs.get("src") or [""])[0].strip().lower()
        if src not in ("", "wavlake", "library", "free"):   # nr_freelane
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

        if src == "free":   # nr_freelane: the free lane only, never mixed with the library or Wavlake
            tracks = asyncio.run(free_local.search(q, limit=lim + 1, offset=off))
            more = len(tracks) > lim
            return self._json(200, {"results": [{"artist": t.artist, "title": t.title, "album": t.album, "source": "free",
                                                 "sats": _play_price(t.duration_s), "duration_s": t.duration_s}
                                                for t in tracks[:lim]], "offset": off, "more": bool(more)})
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
            if src == "" and off == 0:   # nr_freepublic: "all lanes" = the library + the free lane + Wavlake
                try:
                    ft = asyncio.run(free_local.search(q, limit=6))
                    results += [{"artist": t.artist, "title": t.title, "album": t.album, "source": "free",
                                 "sats": _play_price(t.duration_s), "duration_s": t.duration_s} for t in ft]
                except Exception:
                    pass                                 # the free lane never breaks the library search

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
                    words = [w for w in q.lower().split() if re.search(r"\w", w)]   # nr_searchdash
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
            # nr_rss24 (2026-10-01): the last 24 hours, sent as "application/xml" -- as "application/rss+xml" a
            # browser offered to DOWNLOAD the feed (SF clicked the footer link and got a save dialog); as XML it
            # shows it. Feed readers read both.
            return self._xml(200, buma_reports.rss().decode("utf-8"), content_type="application/xml; charset=utf-8")
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
        if parsed.path == "/api/admin/mood/find":
            # nr_moodfind (2026-09-16, SF: "search in a playlist ... a piece I've lost but I know it's there"): every word
            # must appear in the file name, the folder name or -- for a song that never reached the server -- in the
            # artist/title of the .missing.tsv list. Playlist files are read fresh (they are small).
            q = (parse_qs(parsed.query).get("q") or [""])[0]
            words = [w for w in re.split(r"\s+", q.lower().strip()) if re.search(r"\w", w)][:8]   # nr_searchdash
            if not words:
                return self._json(400, {"error": "type a few words first"})
            hits, paths = [], []
            try:
                for f in sorted(os.listdir(_PLAYLIST_DIR)):
                    full = os.path.join(_PLAYLIST_DIR, f)
                    if f.endswith(".m3u"):
                        with open(full, encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                p = line.strip()
                                if not p or p.startswith("#"):
                                    continue
                                hay = p.lower()
                                if all(w in hay for w in words):
                                    hits.append({"playlist": f[:-4], "path": p, "on_server": True}); paths.append(p)
                    elif f.endswith(".missing.tsv"):
                        with open(full, encoding="utf-8", errors="replace") as fh:
                            next(fh, None)
                            for line in fh:
                                cols = line.rstrip("\n").split("\t")
                                if len(cols) < 2:
                                    continue
                                hay = (cols[0] + " " + cols[1]).lower()
                                if all(w in hay for w in words):
                                    hits.append({"playlist": f[:-len(".missing.tsv")], "artist": cols[0], "title": cols[1], "on_server": False,
                                                 "why": cols[3] if len(cols) > 3 else ""})
                    if len(hits) >= 80:
                        break
            except OSError as e:
                return self._json(502, {"error": "playlists: %s" % e})
            if paths:
                try:
                    import sqlite3 as _sq
                    con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
                    tags = {}
                    for p in set(paths):
                        row = con.execute("SELECT artist, title, duration_s FROM tracks WHERE path = ?", (p,)).fetchone()
                        if row:
                            tags[p] = row
                    con.close()
                except Exception:
                    tags = {}
                for h in hits:
                    if h.get("path"):
                        row = tags.get(h["path"])
                        base = os.path.splitext(os.path.basename(h["path"]))[0]
                        h["artist"] = (row[0] if row else "") or ""
                        h["title"] = (row[1] if row else "") or base
                        h["dur"] = int(row[2] or 0) if row else 0
                        h["indexed"] = bool(row)
            return self._json(200, {"results": hits[:80], "more": len(hits) > 80, "words": words})
        if parsed.path == "/api/admin/mood/list":
            # nr_moodlist (2026-09-16, SF: "scroll the entirety of a playlist"): every song of one playlist, in order,
            # with tags from the index; the songs that never reached the server come from the .missing.tsv beside it.
            name = os.path.basename((parse_qs(parsed.query).get("name") or [""])[0]).replace("..", "")
            if not name:
                return self._json(400, {"error": "which playlist?"})
            src = os.path.join(_PLAYLIST_DIR, name + ".m3u")
            if not os.path.isfile(src):
                return self._json(404, {"error": "no such playlist"})
            try:
                with open(src, encoding="utf-8", errors="replace") as fh:
                    paths = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
            except OSError as e:
                return self._json(502, {"error": "playlist: %s" % e})
            tags = {}
            try:
                import sqlite3 as _sq
                con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
                for p in set(paths):
                    row = con.execute("SELECT artist, title, duration_s FROM tracks WHERE path = ?", (p,)).fetchone()
                    if row:
                        tags[p] = row
                con.close()
            except Exception:
                tags = {}
            for p, fm in _free_meta([p for p in set(paths) if p not in tags]).items():   # nr_mylists2: the free lane
                tags[p] = (fm[0], fm[1], fm[2])
            songs = []
            for i, p in enumerate(paths, 1):
                row = tags.get(p)
                songs.append({"n": i, "path": p, "artist": (row[0] if row else "") or "", "title": ((row[1] if row else "") or os.path.splitext(os.path.basename(p))[0]),
                              "dur": int(row[2] or 0) if row else 0, "indexed": bool(row)})
            missing = []
            try:
                with open(os.path.join(_PLAYLIST_DIR, name + ".missing.tsv"), encoding="utf-8", errors="replace") as fh:
                    next(fh, None)
                    for line in fh:
                        cols = line.rstrip("\n").split("\t")
                        if len(cols) >= 2:
                            missing.append({"artist": cols[0], "title": cols[1], "why": cols[3] if len(cols) > 3 else ""})
            except OSError:
                pass
            return self._json(200, {"name": name, "songs": songs, "missing": missing})
        try:
            return self._json(200, _mood_state())
        except Exception as e:
            return self._json(502, {"error": "mood state: %s" % e})

    def _handle_set_post(self):
        """nr_sets: mint the invoice for a listener set. Every brake is checked BEFORE a single sat is asked for."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 60000:
            return self._json(400, {"error": "bad request"})
        qs = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        info = _set_info()
        if not info["open"]:
            msg = {"off": "Sets are switched off right now.",
                   "live": "A live session is on. Sets wait until it ends.",
                   "busy": "A set is playing. The next set can start after it.",
                   "queue": "The queue is busy. A set can start when 2 or fewer songs wait."}.get(info["why"], "Not now.")
            return self._json(409, {"error": msg, "why": info["why"]})
        if not LNBITS_PLAY_INVOICE_KEY:
            return self._json(500, {"error": "Payments not configured"})
        try:
            size = int((qs.get("size") or ["0"])[0])
        except ValueError:
            size = 0
        if size not in SET_SIZES:
            return self._json(400, {"error": "a set has 7, 14 or 21 songs"})
        cid = "".join(c for c in (qs.get("cid") or [""])[0] if c.isalnum())[:40]
        if len(cid) < 12:
            return self._json(400, {"error": "this browser sent no id -- reload the page and try again"})
        if rds.exists("set:cid:%s" % cid):
            return self._json(429, {"error": "One set per day per phone or browser. Come back tomorrow for the next one."})
        try:
            songs_in = json.loads((qs.get("songs") or ["[]"])[0])
        except Exception:
            songs_in = None
        if not isinstance(songs_in, list) or len(songs_in) != size:
            return self._json(400, {"error": "the set must hold exactly %d songs" % size})
        local = chain.by_name["local"]
        recent = _set_recent_keys()
        songs, seen, again = [], set(), []
        for s in songs_in:
            if not isinstance(s, dict):
                return self._json(400, {"error": "bad song"})
            artist = str(s.get("artist") or "").strip()[:300]
            title = str(s.get("title") or "").strip()[:300]
            if not artist or not title:
                return self._json(400, {"error": "a song without a name"})
            want_album = str(s.get("album") or "").strip().lower()[:200]
            try:
                want_dur = int(float(s.get("duration_s") or 0))
            except Exception:
                want_dur = 0
            if want_dur < 0 or want_dur > 86400:
                want_dur = 0
            def fits(t, wa=want_album, wd=want_dur):
                if wa and (t.album or "").strip().lower() != wa:
                    return False
                return not (wd > 0 and abs(_int0(t.duration_s) - wd) > 2)
            t = _exact_hit(local, artist, title, fits) or _exact_hit(local, artist, title, lambda t: True)
            if t is None or not t.local_path or not os.path.exists(t.local_path):
                return self._json(404, {"error": "Not on the shelf right now: %s - %s. Pick another song; you have not been charged." % (artist, title)})
            key = _mood_key(t.artist, t.title)
            if key in seen:
                return self._json(400, {"error": "%s - %s is in the set twice." % (t.artist, t.title)})
            seen.add(key)
            if key in recent:
                again.append("%s - %s" % (t.artist, t.title))
            songs.append({"path": t.local_path, "artist": t.artist, "title": t.title, "dur": _int0(t.duration_s),
                          "rights": t.rights_class.value})
        if again:
            return self._json(409, {"error": "Already played in the last 24 hours: %s. Swap them for others; you have not been charged." % "; ".join(again[:5]),
                                    "again": again})
        total = sum(s["dur"] for s in songs)
        price = _set_price(total, size)
        try:
            invoice = _create_play_invoice(memo="Zap: a listener set, %d songs, %d min" % (size, round(total / 60)), sats=price)
        except Exception as e:
            return self._json(502, {"error": "Could not create invoice: %s" % e})
        if not invoice.get("payment_hash") or not invoice.get("bolt11"):
            return self._json(502, {"error": "LNbits returned an unexpected invoice response"})
        rec = {"id": os.urandom(5).hex(), "songs": songs, "cid": cid, "sats": price, "queued": False, "pushed": 0,
               "note": _clean_note((qs.get("note") or [""])[0], False) if _notes_on() else "",
               "who": _clean_who((qs.get("who") or [""])[0]) if _notes_on() else "", "minted_at": time.time()}
        rds.setex(_set_pending_key(invoice["payment_hash"]), PENDING_RECORD_TTL_S, json.dumps(rec))
        self._json(200, {"payment_hash": invoice["payment_hash"], "bolt11": invoice["bolt11"], "sats": price,
                         "total_s": total, "n": size, "notes_enabled": False, "status_path": "/api/set/status",
                         "message": "Zap %d sats to queue your set of %d songs" % (price, size)})

    def _handle_set_status(self, parsed):
        ph = (parse_qs(parsed.query).get("payment_hash") or [""])[0].strip()
        if not ph:
            return self._json(400, {"error": "missing payment_hash"})
        raw = rds.get(_set_pending_key(ph))
        if raw is None:
            return self._json(404, {"error": "unknown or expired payment_hash"})
        rec = json.loads(raw)
        if rec.get("queued"):
            return self._json(200, {"paid": True, "queued": True, "message": rec.get("message", "Queued")})
        try:
            paid = _is_play_invoice_paid(ph)
        except Exception as e:
            return self._json(502, {"error": "Could not reach LNbits: %s" % e})
        if not paid:
            return self._json(200, {"paid": False, "queued": False})
        ok, message, wait = _set_queue(ph)
        if wait:
            return self._json(200, {"paid": True, "queued": False, "message": "Zap seen! Queuing your set in %ds" % wait})
        if not ok:
            return self._json(500, {"error": "Paid but could not queue the set: %s" % message})
        self._json(200, {"paid": True, "queued": True, "message": message})

    def _handle_pepper_post(self, parsed):
        """nr_pepper: on / off / every (n=3..10) / keep (path -> the steady rotation) / never (path: not again)."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        act = parsed.path.rsplit("/", 1)[-1]
        path = (qs.get("path") or [""])[0]
        try:
            if act in ("on", "off"):
                rds.set("pepper:on", "1" if act == "on" else "0")
                if act == "on":
                    rds.set("pepper:last_ts", str(time.time()))     # count afresh: no pepper straight after switching on
                return self._json(200, {"ok": True, "message": "pepper %s" % act.upper()})
            if act == "every":
                n = int((qs.get("n") or ["0"])[0])
                if not 3 <= n <= 10:
                    return self._json(400, {"error": "pick a number from 3 to 10"})
                rds.set("pepper:every", n)
                return self._json(200, {"ok": True, "message": "a pepper song after every %d rotation songs" % n})
            if act == "keep":
                ok, msg = _rot_add(path)
                return self._json(200 if ok else 409, {"ok": ok, "message": msg} if ok else {"error": msg})
            if act == "never":
                ok, msg = _pepper_never(path)
                return self._json(200 if ok else 409, {"ok": ok, "message": msg} if ok else {"error": msg})
        except Exception as e:
            return self._json(500, {"error": str(e)[:200]})
        return self._json(404, {"error": "unknown pepper action"})

    def _handle_mood_post(self, parsed):
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        act = parsed.path.rsplit("/", 1)[-1]
        try:
            if act == "load":
                # nr_multimood (2026-10-03, SF: "select two, three or however many I like to put into a temporary
                # rotation"): one OR MORE name= values. The lists are joined in the order given (the order the captain
                # ticked them); a song in two lists plays once. One name = exactly the old behaviour.
                names = []
                for raw_name in qs.get("name") or [""]:
                    nm = os.path.basename(raw_name).replace("..", "")
                    if not nm.endswith(".m3u"):
                        nm += ".m3u"
                    if nm[:-4] and nm not in names:
                        names.append(nm)
                if not names:
                    return self._json(400, {"error": "no such playlist"})
                if len(names) > 50:
                    return self._json(400, {"error": "at most 50 playlists at once"})
                paths, seen_p, doubles = [], set(), 0
                for nm in names:
                    src = os.path.join(_PLAYLIST_DIR, nm)
                    if not os.path.isfile(src):
                        return self._json(400, {"error": "no such playlist: %s" % nm[:-4]})
                    with open(src, encoding="utf-8") as fh:
                        for l in fh:
                            l = l.strip()
                            if not l or l.startswith("#"):
                                continue
                            if l in seen_p:
                                doubles += 1
                                continue
                            seen_p.add(l)
                            paths.append(l)
                if not paths:
                    return self._json(400, {"error": "that playlist is empty on the server" if len(names) == 1 else "those playlists are empty on the server"})
                shown = [nm[:-4] for nm in names]
                name = (" + ".join(shown) if len(shown) <= 3 else " + ".join(shown[:2]) + " + %d more" % (len(shown) - 2)) + ".m3u"
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
                return self._json(200, {"ok": True, "message": "%s: %d songs queued%s%s -- starts after the current song%s" % (
                    name[:-4], len(items), " (shuffled)" if shuffle else "",
                    (" (%d double%s left out)" % (doubles, "" if doubles == 1 else "s")) if doubles else "", note)})
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
            if act == "add":
                # nr_moodbrowse + nr_captline: "+ Queue" -- ONE song to the BOTTOM of the captain's line. A line that is
                # not playing starts fresh as "hand-picked"; a paused line stays paused (the message says so).
                want = (qs.get("path") or [""])[0]
                if not want:
                    return self._json(400, {"error": "no path given"})
                if not _capt_path_ok(want):
                    return self._json(400, {"error": "that song is not in the library index"})
                it, n, fresh, paused = _capt_put(want, top=False)
                who = ("%s - %s" % (it.get("artist", ""), it.get("title", ""))).strip(" -")
                tail = " -- a new hand-picked line, it starts after the current song" if fresh else (
                       " -- your line is PAUSED; Resume plays it" if paused else "")
                return self._json(200, {"ok": True, "message": "%s added at the end of your line (%d in it)%s" % (who, n, tail)})
            if act == "move":
                # nr_captline (SF: "a manual drag option for the queue"): one song to another place in the line. It is named
                # by its place AND its path, so a line that moved on meanwhile (the feeder took the top song) is refused.
                want = (qs.get("path") or [""])[0]
                try:
                    i = int((qs.get("from") or [""])[0])
                    j = int((qs.get("to") or [""])[0])
                except ValueError:
                    return self._json(400, {"error": "bad position"})
                with _mood_lock:
                    items = rds.lrange("mood:list", 0, -1)
                    try:
                        same = 0 <= i < len(items) and json.loads(items[i]).get("path") == want
                    except Exception:
                        same = False
                    if not same:
                        return self._json(409, {"error": "your line moved on meanwhile -- look again"})
                    j = max(0, min(j, len(items) - 1))
                    raw = items.pop(i)
                    items.insert(j, raw)
                    pipe = rds.pipeline()
                    pipe.delete("mood:list")
                    pipe.rpush("mood:list", *items)
                    pipe.execute()
                try:
                    it = json.loads(raw)
                    who = ("%s - %s" % (it.get("artist", ""), it.get("title", ""))).strip(" -")
                except Exception:
                    who = "the song"
                return self._json(200, {"ok": True, "message": "%s is now number %d in your line" % (who, j + 1)})
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
        if parsed.path == "/api/chat/nostr":           # nr_nostrpost: the chat bar (a listener's own signed message)
            return self._handle_nostr_post()
        if parsed.path == "/api/request":
            return self._handle_request()
        if parsed.path == "/api/x/code":           # nr_xlane: the bot mints a code (localhost only)
            return self._handle_x_code_mint()
        if parsed.path == "/api/set":                   # nr_sets (2026-10-31)
            return self._handle_set_post()
        if parsed.path in ("/api/admin/sets/on", "/api/admin/sets/off"):
            length = int(self.headers.get("Content-Length", 0))
            sq = parse_qs(self.rfile.read(length).decode()) if length else {}
            if not self._admin_authed((sq.get("token") or [""])[0]):
                return self._json(403, {"error": "not logged in"})
            on = parsed.path.endswith("/on")
            rds.set("sets:on", "1" if on else "0")
            return self._json(200, {"ok": True, "message": "listener sets %s" % ("ON" if on else "OFF -- a paid set still plays")})
        if parsed.path == "/api/play":
            return self._handle_play()
        if parsed.path == "/api/admin/note":
            return self._handle_note_add()
        if parsed.path == "/api/admin/giftplay":
            return self._handle_giftplay()
        if parsed.path in ("/api/admin/freeplay", "/api/freeplay/claim", "/api/freeplay/play"):   # nr_freeplay
            return self._handle_freeplay_post(parsed)
        if parsed.path == "/api/donate":
            return self._handle_donate()
        if parsed.path in ("/api/admin/playlist/load", "/api/admin/playlist/shuffle"):
            # nr_mood (2026-09-05): RETIRED. These replaced the background rotation FILE with a playlist,
            # and the deal (nr-shuffle) silently undid that within 6 h. The captain's mood queue
            # (/api/admin/mood/*) plays a playlist ABOVE the rotation without touching it.
            return self._json(410, {"error": "retired -- use the captain's mood queue (/api/admin/mood/*)"})
        if parsed.path.startswith("/api/admin/mood/"):
            return self._handle_mood_post(parsed)
        if parsed.path.startswith("/api/admin/pepper/"):  # nr_pepper (2026-10-31)
            return self._handle_pepper_post(parsed)
        if parsed.path in ("/api/admin/rotation/add", "/api/admin/rotation/remove",
                           "/api/admin/chat/ban", "/api/admin/chat/unban"):
            return self._handle_admin_console_post(parsed)
        if parsed.path in ("/api/admin/page/save", "/api/admin/i18n/translate"):   # nr_pageedit (2026-09-14)
            length = int(self.headers.get("Content-Length", 0))
            if length > 400000:
                return self._json(413, {"error": "too big"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                return self._json(400, {"error": "bad json"})
            if not isinstance(body, dict) or not self._admin_authed(str(body.get("token") or "")):
                return self._json(403, {"error": "not the captain"})
            try:
                if parsed.path == "/api/admin/page/save":
                    return self._json(200, _page_save(body.get("page"), body.get("lang"), body.get("edits")))
                reports = []
                if body.get("page"):
                    reports.append(_page_translate(body.get("page"), keys=body.get("keys") or None, missing=bool(body.get("missing"))))
                elif body.get("missing"):
                    for pg in _PAGE_EDIT_PAGES:
                        reports.append(_page_translate(pg, missing=True))
                else:
                    byp = {}
                    for i in _stale_load(): byp.setdefault(i.get("page"), []).append(i.get("key"))
                    for pg, ks in byp.items():
                        if pg in _PAGE_EDIT_PAGES: reports.append(_page_translate(pg, keys=ks))
                return self._json(200, {"ok": True, "reports": reports})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": "failed: %s" % str(e)[:300]})
        if parsed.path == "/api/admin/remaining":      # nr_remaining (2026-09-14): the captain types what is left
            length = int(self.headers.get("Content-Length", 0))
            pqs = parse_qs(self.rfile.read(length).decode())
            if not self._admin_authed((pqs.get("token") or [""])[0]):
                return self._json(403, {"error": "invalid or missing token"})
            raw = re.sub(r"[^0-9]", "", (pqs.get("sats") or [""])[0])   # "3.005.000,-" -> 3005000; whole sats only
            if raw == "":
                rds.delete("jukebox:sats_remaining"); rds.delete("jukebox:sats_remaining_ts")
                return self._json(200, {"ok": True, "remaining_sats": None})
            if len(raw) > 12:
                return self._json(400, {"error": "that is not a sats amount"})
            rds.set("jukebox:sats_remaining", raw); rds.set("jukebox:sats_remaining_ts", str(int(time.time())))
            return self._json(200, {"ok": True, "remaining_sats": int(raw)})
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
        if parsed.path == "/api/admin/play":            # nr_captainplay (2026-09-14): the captain queues a library track, free
            return self._handle_admin_play()
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
        if not sess or not LNBITS_STAGE_ADMIN_KEY or sess.get("ended_at"):      # nr_autoreturn: ended = not live
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
        sess = _load_session() or _load_parked()          # nr_autoreturn: a parked pot still waits for the lever
        if not sess:
            return self._json(200, {"live": False, "wavlake_owed": owed})
        out = {"live": True, "percent": sess.get("percent"),
               "djs": [{"alias": d.get("alias"), "address": d.get("address"),
                        "nopay": bool(d.get("nopay"))} for d in sess.get("djs", [])],
               "started_at": sess.get("started_at"), "now_dj": sess.get("now_dj"),
               "now_djs": sess.get("now_djs") or [], "wavlake_owed": owed,
               "closing_age_s": (int(time.time()) - int(sess.get("closing") or 0)) if sess.get("closing") else None}
        if sess.get("ended_at"):                          # nr_autoreturn
            out.update({"ended_at": sess.get("ended_at"), "ended_why": sess.get("ended_why") or ""})
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

    def _open_session(self, djs, percent, pause_set=False):
        """Shared lever-ON path: sweep the dust-era targets, snapshot the stage
        balance as the pot baseline, store the session. djs = [{alias,address}]."""
        # nr_sets (SF's rule a: a listener's set finishes first; rule b: the captain may pause it for an important live set)
        _sa = _set_active()
        if _sa and not pause_set:
            return self._json(409, {"set_playing": True, "error": "A listener's set is playing: song %d of %d, about %d minutes left. "
                                    "Wait for it to finish, or open the session anyway: the set pauses and the rest plays "
                                    "after the session." % (max(_sa['i'], 1), _sa['n'], round(_sa['left_s'] / 60))})
        ghost = _load_session()
        if ghost is not None:
            who = ", ".join((d.get("alias") or d.get("address") or "?") for d in ghost.get("djs", [])) or "empty pool"
            age_h = (int(time.time()) - int(ghost.get("started_at", 0))) / 3600.0
            return self._json(409, {"error": "A session is already open (%s; started %.1f h ago). "
                                    "Settle it (lever OFF), or clear the ghost from the console -- "
                                    "opening over it would split tonight's pot with yesterday's crew (the 08-07 incident)." % (who, age_h)})
        parked = _load_parked()                           # nr_autoreturn
        if parked is not None:
            return self._json(409, {"error": "The last session ended by itself (%s) and its pot still waits. "
                                    "Throw the lever OFF first to pay it, then open the new one." % (parked.get("ended_why") or "stream stopped")})
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
        return self._open_session([{"alias": alias or dj, "address": dj}], percent,
                                  pause_set=(qs.get("pause_set") or [""])[0] == "1")

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
        return self._open_session(djs, percent, pause_set=(qs.get("pause_set") or [""])[0] == "1")

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
        _unpark_session()                                 # nr_autoreturn: Clear works on a parked pot too
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
        _unpark_session()                                 # nr_autoreturn: a parked pot is settled like any other

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
            # nr_shelf (2026-09-27): the same 24-row cliff as /api/play -- ask the index for a row of that
            # exact name whose file is on disk.
            track = _exact_hit(local, artist, title,
                               lambda t: bool(t.local_path) and os.path.exists(t.local_path))
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

    def _handle_freeplay_post(self, parsed):
        """nr_freeplay: the captain's switch, a phone's claim, a phone's free play (the rules sit at _fp_state)."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 4000:
            return self._json(413, {"error": "too big"})
        qs = parse_qs(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
        def one(k):
            return (qs.get(k) or [""])[0].strip()
        try:
            if parsed.path == "/api/admin/freeplay":
                if not self._admin_authed(one("token")):
                    return self._json(403, {"error": "not the captain"})
                try:
                    return self._json(200, _fp_switch(one("action"), _int0(one("n")), _int0(one("per"))))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            if parsed.path == "/api/freeplay/claim":
                ip = (self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                      or self.client_address[0])
                code, out = _fp_claim(one("t"), one("have"), ip, one("done"))
                return self._json(code, out)
            code, out = _fp_play(one("code"), one("artist")[:300], one("title")[:300],
                                 lambda ar, ti: self._fp_resolve(ar, ti, one("source")), self._fp_push,
                                 _load_session() is not None)
            return self._json(code, out)
        except Exception as e:
            return self._json(503, {"error": "The station is busy (%s). Try again in a moment." % str(e)[:80]})

    def _fp_resolve(self, artist, title, source=""):
        """the library row for a free play: the same lookup as the captain's gift, and the file must be on disk"""
        if not artist or not title:
            return None, "missing artist/title"
        local = _lane_local(source)   # nr_freelane
        matches = asyncio.run(local.search(f"{artist} {title}", limit=SEARCH_LIMIT_MAX))
        track = next((t for t in matches if t.artist.strip().lower() == artist.lower()
                      and t.title.strip().lower() == title.lower()), None)
        if not track:
            track = _exact_hit(local, artist, title, lambda t: bool(t.local_path) and os.path.exists(t.local_path))
        if not track:
            return None, "That song is not in the ship library. Pick another one."
        if not track.local_path or not os.path.exists(track.local_path):
            return None, "That one has slipped off the shelf. Pick another song."
        return track, None

    def _fp_push(self, track):
        # nr_freeplay2: lane "gift" as before (X + Nostr skip it, the site says "on the house"); kind "free" tells the
        # Telegram post that the captain gave it to a stranger, not that he picked the song himself
        _mark_paid_rid(push_track(track.local_path, rights_class=track.rights_class.value, source="gift",
                                  artist=track.artist, title=track.title), track.local_path, "", "gift", kind="free")

    def _handle_admin_play(self):
        """nr_captainplay (2026-09-14): the captain's own request from the console -- one library track, queued now,
        free of charge, on the same road as the Telegram /gift (source "gift": no money, no ledger, no credit).
        Refused while a live session holds the deck. The path comes from the console's own search."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode()) if length else {}
        if not self._admin_authed((qs.get("token") or [""])[0]):
            return self._json(403, {"error": "not logged in"})
        path = (qs.get("path") or [""])[0]
        if not path.startswith("/srv/") or ".." in path:
            return self._json(400, {"error": "bad path"})
        if _load_session() is not None:
            return self._json(409, {"error": "live session on -- the DJ has the deck"})
        import sqlite3 as _sq
        con = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = con.execute("SELECT artist, title, rights_class FROM tracks WHERE path = ?", (path,)).fetchone()
        con.close()
        if not row:                                 # nr_mylists2: a free-lane song
            fm = _free_meta([path]).get(path)
            row = (fm[0], fm[1], fm[3]) if fm else None
        if not row:
            return self._json(404, {"error": "not in the library index"})
        if not os.path.exists(path):
            return self._json(404, {"error": "file missing from the library disks"})
        artist, title, rights = row[0] or "", row[1] or "", row[2] or "collecting_society"
        # nr_captline (2026-10-05, SF: "pressing the play now button does not move TOY above Fontaines D.C."): Play now no
        # longer joins the END of the request line (liquidsoap cannot reorder that line safely). It goes to the TOP of the
        # captain's line: after the current song and any requests already waiting (SF's ruling: paid stays first).
        if not _capt_path_ok(path):
            return self._json(404, {"error": "not in the library index"})
        try:
            it, n, fresh, paused = _capt_put(path, top=True)
        except Exception as e:
            return self._json(500, {"error": "could not queue: %s" % e})
        try:
            ahead = len(queue_rids())
        except Exception:
            ahead = 0
        bits = []
        if ahead:
            bits.append("%d request%s already waiting" % (ahead, "" if ahead == 1 else "s"))
        if _capt_handed():
            bits.append("the one song of your line already handed to the radio")
        msg = "'%s - %s' is at the top of your line -- it plays after the current song%s" % (
            artist, title, (" and " + " and ".join(bits)) if bits else "")
        if paused:
            msg += "; your line stays paused, only this song plays"
        if not _mood_lane_ok():
            msg += "; NOTE: the radio has no mood lane right now -- nothing plays until it is back"
        return self._json(200, {"ok": True, "message": msg})

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
               "expires_in": max(0, int(ttl or 0)),
               "note": (_clean_note(rec.get("note") or "") if _notes_on() else "")}   # nr_xnote: shown read-only on the page
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
                try:                              # nr_wavcache: the artist removed it -> our copy goes too
                    os.remove(os.path.join(WAVLAKE_SPOOL_DIR, guid + ".mp3"))
                except OSError:
                    pass
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
                reply = _xlane_reply(reply, pending)
                try:
                    (_xcode_extra if xrec.get("_extra") else _xcode_bind)(xcode, xrec, pending, invoice, reply)
                except Exception:
                    pass                                   # the invoice and its record exist; the reply must still go out
                _xcode_release(xcode)
            return self._json(200, reply)

        # Never trust a client-supplied path -- re-look-up the track ourselves so
        # only files actually in the indexed library can ever be queued.
        local = _lane_local((qs.get("source") or [""])[0])   # nr_freelane: free.db only for a free row
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
        if track is None and not named:
            # nr_shelf (2026-09-27, SF: "/play iron maiden" answered "That one slipped off the shelf" three
            # times): the exact name can be MISSING from the 24-row window although search just showed the
            # row. The song "Iron Maiden" by Iron Maiden drowns among rows that carry those words twice. So
            # ask the index: the tapped row first (album + length hints), then any row of that exact name.
            # The ghost guard below still checks the file, and the price follows the row that is found.
            track = _exact_hit(local, artist, title, _fits)
            if track is None and (want_album or want_dur > 0):
                track = _exact_hit(local, artist, title, lambda t: True)
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
                _con = sqlite3.connect(local.db_path)   # nr_freelane: the twin is hunted in the same lane
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
            reply = _xlane_reply(reply, pending)
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
        remaining, remaining_ts = _remaining_sats()           # nr_remaining: typed by the captain, never measured
        self._json(200, {"total_sats": total, "dj_paid_sats": dj_paid,
                         "og_paid_sats": og_sats, "og_plays": og_plays,
                         "remaining_sats": remaining, "remaining_ts": remaining_ts})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=_sweep_paid_plays, daemon=True).start()
    threading.Thread(target=_sesh_autoend, daemon=True).start()    # nr_autoreturn (2026-10-22)
    threading.Thread(target=_mood_feeder, daemon=True).start()     # nr_mood (2026-09-05)
    threading.Thread(target=_pepper_feeder, daemon=True).start()   # nr_pepper (2026-10-31)
    threading.Thread(target=_set_sweeper, daemon=True).start()     # nr_sets (2026-10-31)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"library_api listening on 127.0.0.1:{PORT}")
    server.serve_forever()
