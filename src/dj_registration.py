#!/usr/bin/env python3
"""
dj_registration.py -- self-service registration for guest DJs, plus a slot-request
roster (PoC).

    GET  /                  -> registration form
    POST /register           -> body: name=... ; creates an LNbits user + wallet +
                                 Lightning Address for the DJ, returns it to them
    GET  /request-slot        -> slot request form (date + studio hour)
    POST /request-slot        -> body: dj_name=...; date=YYYY-MM-DD; hour_cet=0-23
    GET  /admin?token=...      -> pending/approved/rejected queue (token-gated)
    POST /admin/approve        -> body: id=...; token=...
    POST /admin/reject         -> body: id=...; token=...

Meant to be served over clearnet (via Caddy /dj/*), same as the main site -- see
Handoff.md. Split-payment percentage assignment is a deliberate manual step done
later in LNbits directly, once there's consensus on the split -- this app only
handles onboarding (name -> wallet -> address) and slot scheduling requests.
Approval is manual by design: SF reviews and approves/rejects each request.

Talks to LNbits' own REST API as an admin, using LNBITS_ADMIN_API_KEY (never
hardcoded, never sent to the DJ). Keeps its own small local record of who's
registered and who's requested slots so SF doesn't have to dig through LNbits by hand.
"""

import json
import logging
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import requests

LNBITS_BASE_URL = os.environ.get("LNBITS_BASE_URL", "http://127.0.0.1:5000")
LNBITS_ADMIN_API_KEY = os.environ.get("LNBITS_ADMIN_API_KEY")
DJ_ADMIN_TOKEN = os.environ.get("DJ_ADMIN_TOKEN")
PORT = int(os.environ.get("DJ_REGISTRATION_PORT", "5001"))
DB_PATH = os.environ.get("DJ_REGISTRATION_DB", "/opt/lightning-stack/dj_registrations.sqlite3")

TELEGRAM_CHAT_BOT_TOKEN = os.environ.get("TELEGRAM_CHAT_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # public noderunnersradio group
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")  # SF's private DM
ADMIN_URL = f"https://noderunnersradio.com/dj/admin?token={DJ_ADMIN_TOKEN}"

FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Noderunners Radio -- DJ Registration</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 520px; margin: 60px auto; padding: 0 20px; }
h1 { color: #f7931a; font-size: 1.3rem; }
input, select { width:100%; padding:10px; margin:10px 0; background:#0f262d; border:1px solid #1d3a42; color:#e6eff0; border-radius:4px; box-sizing:border-box; }
button { padding:10px 20px; background:#f7931a; border:none; border-radius:4px; color:#0a1a1f; font-weight:bold; cursor:pointer; }
.result { margin-top: 20px; padding: 14px; border: 1px solid #1d3a42; border-radius: 4px; word-break: break-all; }
.err { color: #ff6b6b; }
a { color: #f7931a; }
</style></head>
<body>
<h1>Noderunners Radio -- Guest DJ Registration</h1>
<p>Enter your DJ name to get your own Lightning Address for the split-sat stream. Split percentages aren't decided yet -- this just sets you up to receive once they are.</p>
<form method="POST" action="/dj/register">
  <input name="name" placeholder="DJ name" required maxlength="40" pattern="[A-Za-z0-9_\-\. ]+">
  <button type="submit">Register</button>
</form>
__RESULT__
</body></html>"""

SLOT_FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Noderunners Radio -- Request a DJ slot</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 520px; margin: 60px auto; padding: 0 20px; }
h1 { color: #f7931a; font-size: 1.3rem; }
input, select { width:100%; padding:10px; margin:10px 0; background:#0f262d; border:1px solid #1d3a42; color:#e6eff0; border-radius:4px; box-sizing:border-box; }
button { padding:10px 20px; background:#f7931a; border:none; border-radius:4px; color:#0a1a1f; font-weight:bold; cursor:pointer; }
.result { margin-top: 20px; padding: 14px; border: 1px solid #1d3a42; border-radius: 4px; word-break: break-all; }
.err { color: #ff6b6b; }
.clocks { display:flex; gap:8px; margin: 10px 0; }
.clock { flex:1; background:#0f262d; border:1px solid #1d3a42; border-radius:4px; padding:8px; text-align:center; }
.clock .lbl { display:block; font-size:0.7rem; color:#8aa; }
.clock .val { display:block; font-size:1rem; font-weight:bold; }
</style></head>
<body>
<h1>Request a DJ slot</h1>
<p>Pick a date and studio-time (Amsterdam) hour. Nothing's confirmed until Noderunners approves it.</p>
<form method="POST" action="/dj/request-slot">
  <input name="dj_name" placeholder="Your DJ name" required maxlength="40" pattern="[A-Za-z0-9_\-\. ]+">
  <input name="telegram_handle" placeholder="Telegram handle, e.g. @yourname" required maxlength="40">
  <input name="nostr_npub" placeholder="Nostr npub (optional)" maxlength="80">
  <input type="date" name="date" id="date" required>
  <select name="hour_cet" id="hour_cet" required>
    __HOUR_OPTIONS__
  </select>
  <div class="clocks">
    <div class="clock"><span class="lbl">Studio (Amsterdam)</span><span class="val" id="cet-out">--:--</span></div>
    <div class="clock"><span class="lbl">UTC</span><span class="val" id="utc-out">--:--</span></div>
    <div class="clock"><span class="lbl">Your local time</span><span class="val" id="local-out">--:--</span></div>
  </div>
  <button type="submit">Submit request</button>
</form>
__RESULT__
<script>
var dateInput = document.getElementById('date');
var hourSelect = document.getElementById('hour_cet');
function pad(n) { return String(n).padStart(2, '0'); }
function update() {
  if (!dateInput.value) return;
  var hourCet = parseInt(hourSelect.value, 10);
  var probe = new Date(dateInput.value + 'T12:00:00Z');
  var cetOffsetGuess = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Amsterdam', timeZoneName: 'shortOffset' }).formatToParts(probe).find(function(p){ return p.type === 'timeZoneName'; }).value;
  var offsetHours = parseInt(cetOffsetGuess.replace('GMT', '').replace('+', '') || '1', 10);
  var utcHour = ((hourCet - offsetHours) % 24 + 24) % 24;
  var d = new Date(dateInput.value + 'T00:00:00Z');
  d.setUTCHours(utcHour, 0, 0, 0);
  document.getElementById('cet-out').textContent = pad(hourCet) + ':00';
  document.getElementById('utc-out').textContent = pad(utcHour) + ':00';
  document.getElementById('local-out').textContent = d.toLocaleString(undefined, { hour: '2-digit', minute: '2-digit' });
}
dateInput.addEventListener('input', update);
hourSelect.addEventListener('change', update);
update();
</script>
</body></html>"""

ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Noderunners Radio -- DJ slot requests</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 720px; margin: 40px auto; padding: 0 20px; }
h1 { color: #f7931a; font-size: 1.3rem; }
table { width:100%; border-collapse: collapse; margin: 16px 0; }
td, th { border-bottom:1px solid #1d3a42; padding:8px; text-align:left; font-size:0.85rem; }
form.inline { display:inline; }
button { padding:6px 12px; background:#f7931a; border:none; border-radius:4px; color:#0a1a1f; font-weight:bold; cursor:pointer; margin-right:4px; }
button.reject { background:#3a1d1d; color:#ff6b6b; }
.pending { color:#f7931a; }
.approved { color:#7bd88f; }
.rejected { color:#666; }
</style></head>
<body>
<h1>DJ slot requests</h1>
__ROWS__
</body></html>"""


def _init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS dj_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dj_name TEXT NOT NULL,
            lnbits_user_id TEXT,
            lnbits_wallet_id TEXT,
            lightning_address TEXT,
            registered_at INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS slot_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dj_name TEXT NOT NULL,
            telegram_handle TEXT,
            nostr_npub TEXT,
            slot_date TEXT NOT NULL,
            hour_cet INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at INTEGER
        )
    """)
    con.commit()
    con.close()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    return slug[:20] or "dj"


def _record_registration(name, user_id, wallet_id, ln_address):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO dj_registrations (dj_name, lnbits_user_id, lnbits_wallet_id, lightning_address, registered_at) VALUES (?, ?, ?, ?, ?)",
        (name, user_id, wallet_id, ln_address, int(time.time())),
    )
    con.commit()
    con.close()


def _record_slot_request(dj_name, telegram_handle, nostr_npub, slot_date, hour_cet):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO slot_requests (dj_name, telegram_handle, nostr_npub, slot_date, hour_cet, status, requested_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (dj_name, telegram_handle, nostr_npub, slot_date, hour_cet, int(time.time())),
    )
    con.commit()
    con.close()


def _list_slot_requests():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, dj_name, telegram_handle, nostr_npub, slot_date, hour_cet, status, requested_at FROM slot_requests ORDER BY status = 'pending' DESC, slot_date ASC"
    ).fetchall()
    con.close()
    return rows


def _set_slot_status(request_id, status):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE slot_requests SET status = ? WHERE id = ?", (status, request_id))
    con.commit()
    con.close()


def _admin_headers():
    return {"X-Api-Key": LNBITS_ADMIN_API_KEY, "Content-Type": "application/json"}


def create_dj_wallet(dj_name: str) -> dict:
    """Creates an LNbits user + wallet + Lightning Address for a new DJ.
    Raises RuntimeError with a plain-language message on any failure -- one bad
    registration must not crash the server for the next DJ."""
    slug = _slugify(dj_name)

    resp = requests.post(
        f"{LNBITS_BASE_URL}/users/api/v1/user",
        headers=_admin_headers(),
        json={"username": f"dj_{slug}_{int(time.time())}"},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Could not create LNbits user: {resp.status_code} {resp.text}")
    user = resp.json()
    user_id = user.get("id") or user.get("user_id")

    resp = requests.post(
        f"{LNBITS_BASE_URL}/users/api/v1/user/{user_id}/wallet",
        headers=_admin_headers(),
        json={"wallet_name": f"{dj_name} -- split wallet"},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Could not create wallet: {resp.status_code} {resp.text}")
    wallet = resp.json()
    wallet_id = wallet.get("id")
    wallet_api_key = wallet.get("adminkey") or wallet.get("inkey")

    resp = requests.post(
        f"{LNBITS_BASE_URL}/lnurlp/api/v1/links",
        headers={"X-Api-Key": wallet_api_key, "Content-Type": "application/json"},
        json={
            "description": f"{dj_name} -- Noderunners Radio",
            "min": 1,
            "max": 1000000,
            "username": slug,
            "zaps": False,
        },
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Could not create Lightning Address: {resp.status_code} {resp.text}")
    link = resp.json()
    ln_address = f"{slug}@{LNBITS_BASE_URL.replace('http://', '').replace('https://', '')}"

    _record_registration(dj_name, user_id, wallet_id, ln_address)
    return {"dj_name": dj_name, "wallet_id": wallet_id, "lightning_address": ln_address}


def _telegram_send(chat_id, text):
    if not TELEGRAM_CHAT_BOT_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_CHAT_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logging.warning("telegram notify failed: %s", e)


def _notify_new_slot_request(dj_name, date, hour):
    _telegram_send(
        TELEGRAM_CHAT_ID,
        f"New DJ slot request from {dj_name} for {date} {hour:02d}:00 CET -- pending review. @noderunnersfm @plebroyale",
    )
    _telegram_send(
        TELEGRAM_ADMIN_CHAT_ID,
        f"New DJ slot request: {dj_name}, {date} {hour:02d}:00 CET. Review/approve: {ADMIN_URL}",
    )


def _hour_options(selected=20):
    opts = []
    for h in range(24):
        sel = " selected" if h == selected else ""
        opts.append(f'<option value="{h}"{sel}>{h:02d}:00</option>')
    return "\n".join(opts)


class Handler(BaseHTTPRequestHandler):
    def _html(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_admin(self, qs):
        token = (qs.get("token") or [""])[0]
        if not DJ_ADMIN_TOKEN or token != DJ_ADMIN_TOKEN:
            self._html(403, "forbidden")
            return False
        return True

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._html(200, FORM_HTML.replace("__RESULT__", ""))

        if self.path == "/request-slot":
            html = SLOT_FORM_HTML.replace("__HOUR_OPTIONS__", _hour_options()).replace("__RESULT__", "")
            return self._html(200, html)

        if self.path.startswith("/admin"):
            path, _, query = self.path.partition("?")
            qs = parse_qs(query)
            if not self._check_admin(qs):
                return
            rows = _list_slot_requests()
            row_html = []
            for rid, name, tg, npub, date, hour, status, requested_at in rows:
                cls = status
                actions = ""
                if status == "pending":
                    actions = (
                        f'<form class="inline" method="POST" action="/dj/admin/approve">'
                        f'<input type="hidden" name="id" value="{rid}"><input type="hidden" name="token" value="{DJ_ADMIN_TOKEN}">'
                        f'<button type="submit">Approve</button></form>'
                        f'<form class="inline" method="POST" action="/dj/admin/reject">'
                        f'<input type="hidden" name="id" value="{rid}"><input type="hidden" name="token" value="{DJ_ADMIN_TOKEN}">'
                        f'<button type="submit" class="reject">Reject</button></form>'
                    )
                contact = tg or "--"
                if npub:
                    contact += f"<br><span style='color:#8aa;font-size:0.75rem'>{npub}</span>"
                row_html.append(
                    f"<tr><td>{name}</td><td>{contact}</td><td>{date} {hour:02d}:00 CET</td>"
                    f'<td class="{cls}">{status}</td><td>{actions}</td></tr>'
                )
            table = (
                "<table><tr><th>DJ</th><th>Contact</th><th>Requested slot</th><th>Status</th><th></th></tr>"
                + "".join(row_html)
                + "</table>"
                if rows
                else "<p>No slot requests yet.</p>"
            )
            return self._html(200, ADMIN_HTML.replace("__ROWS__", table))

        self._html(404, "not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)

        if self.path == "/register":
            name = (qs.get("name") or [""])[0].strip()
            if not name:
                return self._html(400, FORM_HTML.replace("__RESULT__", '<p class="err">Enter a name.</p>'))
            try:
                result = create_dj_wallet(name)
                result_html = f"""
                <div class="result">
                  <p>Registered, {result['dj_name']}!</p>
                  <p>Your Lightning Address:<br><b>{result['lightning_address']}</b></p>
                  <p>Split percentages aren't set yet -- Noderunners Radio will configure your share once that's decided.</p>
                  <p><a href="/dj/request-slot">Request a DJ slot &rarr;</a></p>
                </div>"""
                return self._html(200, FORM_HTML.replace("__RESULT__", result_html))
            except Exception as e:
                return self._html(500, FORM_HTML.replace("__RESULT__", f'<p class="err">Registration failed: {e}</p>'))

        if self.path == "/request-slot":
            name = (qs.get("dj_name") or [""])[0].strip()
            telegram_handle = (qs.get("telegram_handle") or [""])[0].strip()
            nostr_npub = (qs.get("nostr_npub") or [""])[0].strip()
            date = (qs.get("date") or [""])[0].strip()
            try:
                hour = int((qs.get("hour_cet") or ["20"])[0])
            except ValueError:
                hour = 20
            html = SLOT_FORM_HTML.replace("__HOUR_OPTIONS__", _hour_options(hour))
            if not name or not date or not telegram_handle:
                return self._html(400, html.replace("__RESULT__", '<p class="err">Fill in your DJ name, Telegram handle, and a date.</p>'))
            _record_slot_request(name, telegram_handle, nostr_npub, date, hour)
            _notify_new_slot_request(name, date, hour)
            result_html = f'<div class="result"><p>Request submitted for {date} {hour:02d}:00 (studio time). We\'ll reach out on Telegram once it\'s reviewed.</p></div>'
            return self._html(200, html.replace("__RESULT__", result_html))

        if self.path == "/admin/approve" or self.path == "/admin/reject":
            if not self._check_admin(qs):
                return
            try:
                rid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                rid = 0
            status = "approved" if self.path == "/admin/approve" else "rejected"
            if rid:
                _set_slot_status(rid, status)
            self.send_response(303)
            self.send_header("Location", f"/dj/admin?token={DJ_ADMIN_TOKEN}")
            self.end_headers()
            return

        self._html(404, "not found")


def main():
    if not LNBITS_ADMIN_API_KEY:
        raise SystemExit("LNBITS_ADMIN_API_KEY not set -- refusing to start")
    if not DJ_ADMIN_TOKEN:
        print("WARNING: DJ_ADMIN_TOKEN not set -- /admin will refuse all access")
    _init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"dj_registration listening on 127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
