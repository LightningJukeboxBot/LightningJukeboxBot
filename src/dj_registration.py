#!/usr/bin/env python3
"""
dj_registration.py -- self-service registration for guest DJs.

    GET  /                 -> registration form
    POST /register          -> body: name=... ; creates an LNbits user + wallet +
                                Lightning Address for the DJ, returns it to them

Meant to be served over Tor only (see the vault infra notes for the hidden-service
setup) -- not linked from the public site. Split-payment percentage assignment is
a deliberate manual step done later in LNbits directly, once there's consensus on
the split -- this app only handles onboarding (name -> wallet -> address).

Talks to LNbits' own REST API as an admin, using LNBITS_ADMIN_API_KEY (never
hardcoded, never sent to the DJ). Keeps its own small local record of who's
registered so SF doesn't have to dig through LNbits' user list by hand.
"""

import json
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
PORT = int(os.environ.get("DJ_REGISTRATION_PORT", "5001"))
DB_PATH = os.environ.get("DJ_REGISTRATION_DB", "/opt/lightning-stack/dj_registrations.sqlite3")

FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Noderunners Radio -- DJ Registration</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 520px; margin: 60px auto; padding: 0 20px; }
h1 { color: #f7931a; font-size: 1.3rem; }
input { width:100%; padding:10px; margin:10px 0; background:#0f262d; border:1px solid #1d3a42; color:#e6eff0; border-radius:4px; }
button { padding:10px 20px; background:#f7931a; border:none; border-radius:4px; color:#0a1a1f; font-weight:bold; cursor:pointer; }
.result { margin-top: 20px; padding: 14px; border: 1px solid #1d3a42; border-radius: 4px; word-break: break-all; }
.err { color: #ff6b6b; }
</style></head>
<body>
<h1>Noderunners Radio -- Guest DJ Registration</h1>
<p>Enter your DJ name to get your own Lightning Address for the split-sat stream. Split percentages aren't decided yet -- this just sets you up to receive once they are.</p>
<form method="POST" action="/register">
  <input name="name" placeholder="DJ name" required maxlength="40" pattern="[A-Za-z0-9_\\-\\. ]+">
  <button type="submit">Register</button>
</form>
__RESULT__
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


class Handler(BaseHTTPRequestHandler):
    def _html(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._html(200, FORM_HTML.replace("__RESULT__", ""))
        self._html(404, "not found")

    def do_POST(self):
        if self.path != "/register":
            return self._html(404, "not found")

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
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
            </div>"""
            self._html(200, FORM_HTML.replace("__RESULT__", result_html))
        except Exception as e:
            self._html(500, FORM_HTML.replace("__RESULT__", f'<p class="err">Registration failed: {e}</p>'))


def main():
    if not LNBITS_ADMIN_API_KEY:
        raise SystemExit("LNBITS_ADMIN_API_KEY not set -- refusing to start")
    _init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"dj_registration listening on 127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
