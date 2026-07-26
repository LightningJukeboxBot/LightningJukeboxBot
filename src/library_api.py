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
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import redis
import requests
from resolvers.local import LocalResolver
from resolvers.musicbrainz import MusicBrainzResolver
from resolvers.wanted import WantedQueue
from resolvers.chain import ResolverChain
from liquidsoap_control import push_track, on_air_rid, queue_rids, request_metadata, skip, flush_and_skip
from telegram_chat_relay import TelegramChatRelay

DB_PATH = "/var/lib/jukebox/library.db"
PLAYLOG_PATH = "/var/log/liquidsoap/playlog.tsv"
PORT = 7100
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
LNBITS_BASE_URL = os.environ.get("LNBITS_BASE_URL", "http://127.0.0.1:5000")
LNBITS_PLAY_INVOICE_KEY = os.environ.get("LNBITS_PLAY_INVOICE_KEY")
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
DJ_SPLIT_PERCENT_DEFAULT = int(os.environ.get("DJ_SPLIT_PERCENT", "25"))  # rebuild rate: 75/25
PENDING_PLAY_TTL_S = 900  # invoice must be paid within 15 minutes or it's dead

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


def _track_summary(rid: str) -> dict:
    m = request_metadata(rid)
    return {
        "rid": rid,
        "artist": m.get("artist", "?"),
        "title": m.get("title", "?"),
        "album": m.get("album"),
        "status": m.get("status"),
    }


def _create_play_invoice(memo: str) -> dict:
    resp = requests.post(
        f"{LNBITS_BASE_URL}/api/v1/payments",
        headers={"X-Api-Key": LNBITS_PLAY_INVOICE_KEY, "Content-Type": "application/json"},
        json={"out": False, "amount": PLAY_PRICE_SATS, "memo": memo},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "payment_hash": data.get("payment_hash") or data.get("checking_id"),
        "bolt11": data.get("payment_request") or data.get("bolt11"),
    }


def _is_play_invoice_paid(payment_hash: str) -> bool:
    resp = requests.get(
        f"{LNBITS_BASE_URL}/api/v1/payments/{payment_hash}",
        headers={"X-Api-Key": LNBITS_PLAY_INVOICE_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    paid = data.get("paid")
    if paid is None:
        # some LNbits versions nest status under "details" or use "status": "success"
        paid = data.get("status") == "success" or bool(data.get("details", {}).get("paid"))
    return bool(paid)


def _create_donate_invoice(amount_sats: int, name: str = "", message: str = "") -> dict:
    memo = f"Noderunners Radio donation from {name}" if name else "Noderunners Radio donation"
    if message:
        memo += f": {message}"
    # live mode: mint on the stage wallet so the split machinery sees the payment;
    # calm seas: the plain donate jar as always
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


def _donate_recent(limit=10):
    """Sanitized recent incoming donations across jar + stage, newest first --
    feeds the overlay/site alert popups. Exposes ONLY amount, when, and the
    name/message the donor typed to be shown on screen. Cached ~10s."""
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
                # site donations: "Noderunners Radio donation from {name}: {message}"
                if "donation from " in memo:
                    rest = memo.split("donation from ", 1)[1]
                    name, _, message = rest.partition(": ")
                # lnurlp/zap comments ride in extra
                if not message:
                    c = extra.get("comment")
                    if isinstance(c, list):
                        c = c[0] if c else ""
                    message = c or ""
                items.append({"ts": p.get("time") or 0, "sats": amount // 1000,
                              "name": name.strip()[:40], "message": str(message).strip()[:255]})
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


def _stage_live() -> bool:
    """Live mode = a settlement session is currently open."""
    try:
        return _load_session() is not None
    except Exception:
        return False


def _session_pot(sess) -> dict:
    """Pot so far (sats) + projected whole-sat share per DJ. Read-only."""
    pot_msat = max(0, _stage_balance_msat() - int(sess.get("baseline_msat", 0)))
    n = max(1, len(sess.get("djs", [])))
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


def _pending_play_key(payment_hash: str) -> str:
    return f"pending_play:{payment_hash}"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/search":
            return self._handle_search(parsed)
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
        # public shape: artist/title/album only (drop rid/status internals)
        def pub(t):
            return None if t is None else {"artist": t.get("artist"), "title": t.get("title"), "album": t.get("album")}
        self._json(200, {"now_playing": pub(now_playing), "queue": [pub(t) for t in queue]})

    def _handle_history(self, parsed):
        """Public, read-only: the last N aired tracks from the play log. Exposes
        only ts/artist/title -- NOT rights_class/source (that royalty attribution
        stays internal)."""
        qs = parse_qs(parsed.query)
        try:
            n = max(1, min(int((qs.get("n") or ["25"])[0]), 100))
        except ValueError:
            n = 25
        plays = []
        try:
            with open(PLAYLOG_PATH, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 6 or parts[1] != "play":
                        continue
                    ts, _event, _source, _rights, artist, title = parts
                    if not artist and not title:
                        continue                    # skip blank transition rows
                    plays.append({"ts": ts, "artist": artist, "title": title})
        except FileNotFoundError:
            pass
        plays = plays[-n:][::-1]                     # last n, newest first
        self._json(200, {"history": plays})

    def _handle_chat_recent(self):
        if telegram_chat is None:
            return self._json(200, {"messages": []})
        self._json(200, {"messages": telegram_chat.recent(50)})

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

        self.send_response(200)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/octet-stream"))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        for chunk in upstream.iter_content(chunk_size=65536):
            self.wfile.write(chunk)

    def _handle_search(self, parsed):
        qs = parse_qs(parsed.query)
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(200, {"results": []})

        local = chain.by_name["local"]
        tracks = asyncio.run(local.search(q, limit=10))
        results = [{"artist": t.artist, "title": t.title, "album": t.album} for t in tracks]
        self._json(200, {"results": results})

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

        self._json(200, {"now_playing": now_playing, "queue": queue})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/request":
            return self._handle_request()
        if parsed.path == "/api/play":
            return self._handle_play()
        if parsed.path == "/api/donate":
            return self._handle_donate()
        if parsed.path == "/api/admin/skip":
            return self._handle_admin_action(skip, "Skipped current track")
        if parsed.path == "/api/admin/flush":
            return self._handle_admin_action(flush_and_skip, "Flushed queue and skipped")
        if parsed.path == "/api/admin/livesplit/on":
            return self._handle_livesplit_on()
        if parsed.path == "/api/admin/livesplit/pool":
            return self._handle_livesplit_pool()
        if parsed.path == "/api/admin/livesplit/off":
            return self._handle_livesplit_off()
        if parsed.path == "/api/admin/livesplit/nowplaying":
            return self._handle_livesplit_nowplaying()
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
        sess = _load_session()
        if not sess:
            return self._json(200, {"live": False})
        out = {"live": True, "percent": sess.get("percent"),
               "djs": [{"alias": d.get("alias"), "address": d.get("address")} for d in sess.get("djs", [])],
               "started_at": sess.get("started_at"), "now_dj": sess.get("now_dj"), "now_djs": sess.get("now_djs") or []}
        try:
            out.update(_session_pot(sess))
        except Exception as e:
            out["pot_error"] = str(e)
        self._json(200, out)

    def _open_session(self, djs, percent):
        """Shared lever-ON path: sweep the dust-era targets, snapshot the stage
        balance as the pot baseline, store the session. djs = [{alias,address}]."""
        if _load_session() is not None:
            return self._json(409, {"error": "A session is already open -- settle it first (lever OFF)"})
        _sweep_stage_targets()
        _sweep_jar_targets()
        try:
            baseline = _stage_balance_msat()
        except Exception as e:
            return self._json(502, {"error": f"Could not read the stage wallet: {e}"})
        sess = {"started_at": int(time.time()), "baseline_msat": baseline,
                "percent": percent, "djs": djs}
        rds.set(LIVESESSION_KEY, json.dumps(sess))
        names = ", ".join(d["alias"] for d in djs)
        self._json(200, {"message": f"Session OPEN -- pot is filling; {percent}% to {len(djs)} DJ{'s' if len(djs) > 1 else ''} at settlement: {names}",
                         "live": True, "dj_percent": percent, "count": len(djs)})

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
        if not addrs:
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

        djs = [{"alias": (aliases[i] if i < len(aliases) and aliases[i] else a), "address": a}
               for i, a in enumerate(addrs)]
        return self._open_session(djs, percent)

    def _handle_livesplit_nowplaying(self):
        """Mark which pool DJ is on deck right now (purely cosmetic -- drives
        the overlay highlight; settlement math is untouched). Manual for now,
        slot-schedule automation later."""
        length = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(length).decode())
        token = (qs.get("token") or [""])[0]
        if not self._admin_authed(token):
            return self._json(403, {"error": "invalid or missing token"})
        sess = _load_session()
        if not sess:
            return self._json(400, {"error": "no session open"})
        alias = (qs.get("dj") or [""])[0].strip()
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
            return self._json(400, {"error": "unknown DJ: " + alias})
        sess["now_djs"] = now
        sess["now_dj"] = now[0] if now else None   # legacy single-DJ field
        rds.set(LIVESESSION_KEY, json.dumps(sess))
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

        sess = _load_session()
        if not sess:
            return self._json(200, {"message": "No session was open -- swept stray split targets; donations stay 100% with the radio",
                                    "live": False})

        try:
            pot_msat = max(0, _stage_balance_msat() - int(sess.get("baseline_msat", 0)))
        except Exception as e:
            return self._json(502, {"error": f"Could not read the stage wallet -- session left open: {e}"})

        # atomic claim: redis DELETE returns 1 for exactly ONE caller -- a second
        # concurrent or repeated lever-off loses the claim and pays NOTHING
        # (fix for the 2026-07-23 double-settlement)
        if rds.delete(LIVESESSION_KEY) != 1:
            return self._json(200, {"message": "Session already settled -- nothing paid twice.", "live": False})

        djs = sess.get("djs", [])
        percent = int(sess.get("percent", DJ_SPLIT_PERCENT_DEFAULT))
        n = max(1, len(djs))
        per_dj_sats = int(pot_msat * percent / 100 / n) // 1000  # whole sats, floored

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

        if pending.get("queued"):
            return self._json(200, {"paid": True, "queued": True, "message": pending.get("message", "Already queued")})

        try:
            paid = _is_play_invoice_paid(payment_hash)
        except Exception as e:
            return self._json(502, {"error": f"Could not reach LNbits: {e}"})

        if not paid:
            return self._json(200, {"paid": False, "queued": False})

        global _last_play_at
        now = time.time()
        if now - _last_play_at < PLAY_COOLDOWN_S:
            wait = round(PLAY_COOLDOWN_S - (now - _last_play_at))
            return self._json(200, {"paid": True, "queued": False, "message": f"Paid! Queuing in {wait}s (anti-spam)"})

        try:
            push_track(
                pending["local_path"],
                rights_class=pending.get("rights_class", ""),
                source=pending.get("source", ""),
            )
            _last_play_at = now
        except Exception as e:
            return self._json(500, {"error": f"Paid but could not queue track: {e}"})

        message = f"Queued '{pending['artist']} - {pending['title']}' -- should play shortly"
        pending["queued"] = True
        pending["message"] = message
        rds.setex(_pending_play_key(payment_hash), PENDING_PLAY_TTL_S, json.dumps(pending))
        self._json(200, {"paid": True, "queued": True, "message": message})

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

        # Only a MusicBrainz-confirmed real song gets added -- keeps the
        # wanted list a genuine demand signal instead of a typo bin.
        hits = asyncio.run(musicbrainz.search(q, limit=1))
        if not hits:
            return self._json(200, {"votes": 0, "message": "No results found", "added": False})

        votes = asyncio.run(wanted.add(q))
        self._json(200, {"votes": votes, "message": "Added to the wanted list", "added": True})

    def _handle_play(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        artist = (qs.get("artist") or [""])[0].strip()
        title = (qs.get("title") or [""])[0].strip()
        if not artist or not title:
            return self._json(400, {"error": "missing artist/title"})

        if not LNBITS_PLAY_INVOICE_KEY:
            return self._json(500, {"error": "Payments not configured (LNBITS_PLAY_INVOICE_KEY missing)"})

        # Never trust a client-supplied path -- re-look-up the track ourselves so
        # only files actually in the indexed library can ever be queued.
        local = chain.by_name["local"]
        matches = asyncio.run(local.search(f"{artist} {title}", limit=5))
        track = next(
            (t for t in matches
             if t.artist.strip().lower() == artist.lower() and t.title.strip().lower() == title.lower()),
            None,
        )
        if not track:
            return self._json(404, {"error": "Track not found in library"})

        try:
            invoice = _create_play_invoice(memo=f"Play: {track.display()}")
        except Exception as e:
            return self._json(502, {"error": f"Could not create invoice: {e}"})

        if not invoice.get("payment_hash") or not invoice.get("bolt11"):
            return self._json(502, {"error": "LNbits returned an unexpected invoice response"})

        pending = {
            "artist": track.artist,
            "title": track.title,
            "local_path": track.local_path,
            "rights_class": track.rights_class.value,
            "source": track.source,
            "queued": False,
        }
        rds.setex(_pending_play_key(invoice["payment_hash"]), PENDING_PLAY_TTL_S, json.dumps(pending))

        self._json(200, {
            "payment_hash": invoice["payment_hash"],
            "bolt11": invoice["bolt11"],
            "sats": PLAY_PRICE_SATS,
            "message": f"Pay {PLAY_PRICE_SATS} sats to queue '{track.display()}'",
        })

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
        if not LNBITS_DONATE_INVOICE_KEY:
            return self._json(500, {"error": "Donations not configured (LNBITS_DONATE_INVOICE_KEY missing)"})
        try:
            invoice = _create_donate_invoice(amount, name, message)
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
        self._json(200, {"total_sats": total, "dj_paid_sats": dj_paid})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"library_api listening on 127.0.0.1:{PORT}")
    server.serve_forever()
