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

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

import requests

LNBITS_BASE_URL = os.environ.get("LNBITS_BASE_URL", "http://127.0.0.1:5000")
LNBITS_ADMIN_API_KEY = os.environ.get("LNBITS_ADMIN_API_KEY")
DJ_ADMIN_TOKEN = os.environ.get("DJ_ADMIN_TOKEN")
LIBRARY_API_URL = os.environ.get("LIBRARY_API_URL", "http://127.0.0.1:7100")
PORT = int(os.environ.get("DJ_REGISTRATION_PORT", "5001"))
DB_PATH = os.environ.get("DJ_REGISTRATION_DB", "/opt/lightning-stack/dj_registrations.sqlite3")

# Public domain where LNbits serves .well-known/lnurlp -- what a DJ's Lightning
# Address must say after the @. LNBITS_BASE_URL is the localhost view and would
# print a useless 127.0.0.1 address.
LN_ADDRESS_DOMAIN = os.environ.get("LN_ADDRESS_DOMAIN", "lnbits.plebprojects.com")

TELEGRAM_CHAT_BOT_TOKEN = os.environ.get("TELEGRAM_CHAT_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # public noderunnersradio group
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")  # SF's private DM
# Deliberately NO token in this URL: it rides a Telegram DM, which is not
# end-to-end encrypted. SF logs in on the captain's console instead.
ADMIN_URL = "https://noderunnersradio.com/admin"

# Captain login: library_api.py handles the actual username+password login and
# mints a signed session cookie; we share ADMIN_SESSION_SECRET so that same
# cookie unlocks the DJ admin here too. Token stays as a fallback.
ADMIN_SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET")
SESSION_COOKIE = "nr_admin_session"

# --- DJ portal (login + avatar upload) ---------------------------------------
# Each DJ gets a CLAIM CODE at registration; name + code = their login. The
# avatar arrives as an already-browser-converted webp/png/jpeg (max ~400 KB,
# client resizes to 256px) and is served straight from the site's assets.
AVATAR_DIR = "/home/sf/noderunners-site/assets/avatars"
AVATAR_MAX_BYTES = 400_000
PORTAL_COOKIE = "dj_portal"
PORTAL_TTL_S = 90 * 24 * 3600


def _session_valid(val: str) -> bool:
    if not ADMIN_SESSION_SECRET or not val or "." not in val:
        return False
    exp, sig = val.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(ADMIN_SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)

# --- Registration gate (added 2026-07-22) ---------------------------------
# Public DJ sign-up is CLOSED while the donation-split mechanism is still being
# wired. The Guest DJ Guide is live/readable, but nobody can create a wallet or
# book a slot yet. The token-gated /admin routes are unaffected.
# To REOPEN later: set env DJ_REGISTRATION_OPEN=1 (or flip this default to "1"),
# then: sudo systemctl restart dj-registration
REGISTRATION_OPEN = os.environ.get("DJ_REGISTRATION_OPEN", "0") == "1"

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

html{ background:var(--ether,#0a1a1f); }
body{ background:transparent; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14;
  -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent);
          mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
body::after{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:radial-gradient(ellipse at 20% 0%, rgba(247,147,26,0.06), transparent 55%); }
</style></head>
<body>
<nav style="display:flex;gap:2px;margin:0 0 20px;padding:6px 0;border-bottom:1px solid #1d3a42;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase;flex-wrap:wrap;" aria-label="Site menu">
  <a href="/" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Home</a>
  <a href="/progress" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Progress</a>
  <a href="/dj-handbook" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Guest DJ Guide</a>
  <a href="/manifesto" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Manifesto</a>
  <a href="/login" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;margin-left:auto;">Log in</a>
</nav>
<h1>Noderunners Radio -- Guest DJ Registration</h1>
<p>Enter your DJ name to get your own wallet and Lightning Address. Live-session tips pool up and settle to the crew automatically -- the split ladder is on the <a href="/roadmap" style="color:#f5a623;">roadmap</a>.</p>
<form method="POST" action="/register">
  <input name="name" placeholder="DJ name" required maxlength="40" pattern="[A-Za-z0-9_\-\. ]+">
  <input type="password" name="pass" placeholder="Choose a strong password (min 12 characters)" required minlength="12" maxlength="128">
  <input type="password" name="pass2" placeholder="Repeat the password" required minlength="12" maxlength="128">
  <p style="font-size:0.78rem;color:#8aa4aa;margin:-4px 0 8px;line-height:1.5;">Tip: 4+ random words beat l33t gibberish. Password managers welcome aboard.</p>
  <p style="font-size:0.78rem;color:#8aa4aa;margin:-4px 0 8px;line-height:1.5;">&#9888; Pick your DJ name carefully &mdash; <b>names are set in stone</b> (one DJ = one name = one Lightning Address). Need a change later? Tag <b>@noderunnersfm</b> or <b>@plebroyale</b> in <a href="https://t.me/noderunnersradio" target="_blank" rel="noopener">t.me/noderunnersradio</a>.</p>
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

html{ background:var(--ether,#0a1a1f); }
body{ background:transparent; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14;
  -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent);
          mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
body::after{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:radial-gradient(ellipse at 20% 0%, rgba(247,147,26,0.06), transparent 55%); }
</style></head>
<body>
<h1>Request a DJ slot</h1>
<p>Pick a date and studio-time (Amsterdam) hour. Nothing's confirmed until Noderunners approves it.</p>
<form method="POST" action="/dj/request-slot">
  <input name="dj_name" placeholder="Your DJ name" value="__DJ_NAME__" required maxlength="40" pattern="[A-Za-z0-9_\-\. ]+">
  <input name="telegram_handle" placeholder="Telegram handle, e.g. @yourname" required maxlength="40">
  <p style="font-size:0.78rem;color:#8aa4aa;margin:-4px 0 8px;line-height:1.5;">No Telegram yet? <a href="https://telegram.org/apps" target="_blank" rel="noopener" style="color:#f5a623;font-weight:bold;text-decoration:underline;">Get it here &#8599;</a>, then set your @username so we can reach you:<br>
  &bull; iPhone: Settings &rarr; tap your profile &rarr; Username<br>
  &bull; Android: &#9776; menu &rarr; Settings &rarr; tap your name &rarr; Username</p>
  <input name="nostr_npub" placeholder="Nostr npub (optional)" maxlength="80">
  <input type="date" name="date" id="date" required>
  <div id="dateEcho" style="font-size:0.78rem;color:#8aa4aa;margin:-4px 0 8px;"></div>
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
  document.getElementById('dateEcho').textContent = dateInput.value ? 'Selected: ' + dateInput.value + ' (YYYY-MM-DD)' : '';
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

html{ background:var(--ether,#0a1a1f); }
body{ background:transparent; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14;
  -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent);
          mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
body::after{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:radial-gradient(ellipse at 20% 0%, rgba(247,147,26,0.06), transparent 55%); }
</style></head>
<body>
<h1>DJ slot requests</h1>
__ROWS__
</body></html>"""


INVITE_THANKS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Word sent / Noderunners Radio</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width:560px; margin:70px auto; padding:0 22px; line-height:1.6; }
.card { background:#0f262d; border:1px solid #1d3a42; border-radius:8px; padding:24px 26px; }
h1 { color:#f7931a; font-size:1.3rem; margin:6px 0 14px; }
a { color:#f7931a; font-weight:bold; text-decoration:none; }
</style></head>
<body><div class="card">
  <div style="font-size:1.7rem;">&#9875;</div>
  <h1>Your word reached the bridge</h1>
  <p>The captain reads these by hand, so it can take a while. If we have a berth for you, we will reach out on the contact you left.</p>
  <p><a href="/dj-handbook">&rarr; Read the Guest DJ Guide</a> &nbsp;&middot;&nbsp; <a href="https://t.me/noderunnersradio">&rarr; The Telegram</a></p>
</div></body></html>
"""

CLOSED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Guest DJ sign-up -- closed for now / Noderunners Radio</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width:560px; margin:70px auto; padding:0 22px; line-height:1.6; }
.card { background:#0f262d; border:1px solid #1d3a42; border-radius:8px; padding:24px 26px; }
.anchor { font-size:1.7rem; }
h1 { color:#f7931a; font-size:1.35rem; margin:6px 0 14px; }
p { color:#c9d6d9; }
a { color:#f7931a; font-weight:bold; text-decoration:none; }
.links { margin-top:18px; display:flex; gap:18px; flex-wrap:wrap; font-size:0.92rem; }

html{ background:var(--ether,#0a1a1f); }
body{ background:transparent; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14;
  -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent);
          mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
body::after{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:radial-gradient(ellipse at 20% 0%, rgba(247,147,26,0.06), transparent 55%); }
</style></head>
<body>
<div class="card">
  <div class="anchor">&#9875;</div>
  <h1>Guest DJ sign-up is closed for now</h1>
  <p>The Dread Node's DJ registration isn't open yet &mdash; we're still wiring her up. Nobody can create a wallet or book a slot just yet.</p>
  <p>Read the <a href="/dj-handbook">Guest DJ Guide</a> to see exactly how it'll work, and watch the Telegram for the all-aboard.</p>
  <form method="POST" action="/invite-request" style="margin-top:20px;border-top:1px solid #1d3a42;padding-top:16px;">
    <p style="margin:0 0 10px;"><b>Want aboard?</b> Leave word for the captain. The crew grows slowly and on purpose.</p>
    <input name="name" placeholder="Your name or DJ name" maxlength="40" required
      style="width:100%;padding:9px;margin:4px 0;border-radius:4px;border:1px solid #1d3a42;background:#0a1a1f;color:#e6eff0;font-family:inherit;">
    <input name="contact" placeholder="Where to reach you (Telegram @handle, nostr npub, ...)" maxlength="80"
      style="width:100%;padding:9px;margin:4px 0;border-radius:4px;border:1px solid #1d3a42;background:#0a1a1f;color:#e6eff0;font-family:inherit;">
    <textarea name="message" placeholder="What do you play? Where did you hear about us?" maxlength="500" rows="3"
      style="width:100%;padding:9px;margin:4px 0;border-radius:4px;border:1px solid #1d3a42;background:#0a1a1f;color:#e6eff0;font-family:inherit;"></textarea>
    <input name="website" style="display:none;" tabindex="-1" autocomplete="off">
    <button type="submit" style="margin-top:6px;padding:9px 16px;border:none;border-radius:5px;background:#f7931a;color:#111;font-weight:bold;font-family:inherit;cursor:pointer;">Send word to the captain</button>
    <p style="font-size:0.8rem;color:#8aa4aa;margin:8px 0 0;">No account is created. Nothing is published. The captain reads these by hand.</p>
  </form>
  <div class="links">
    <a href="/dj-handbook">&rarr; Guest DJ Guide</a>
    <a href="https://t.me/noderunnersradio" target="_blank" rel="noopener">&rarr; Telegram</a>
  </div>
</div>
</body></html>"""


PORTAL_LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DJ Portal -- Noderunners Radio</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 460px; margin: 60px auto; padding: 0 20px; }
h1 { color: #f7931a; font-size: 1.3rem; }
input { width:100%; padding:10px; margin:10px 0; background:#0f262d; border:1px solid #1d3a42; color:#e6eff0; border-radius:4px; box-sizing:border-box; }
button { padding:10px 20px; background:#f7931a; border:none; border-radius:4px; color:#0a1a1f; font-weight:bold; cursor:pointer; }
.err { color: #ff6b6b; }
a { color: #f7931a; }
p { line-height:1.5; }

html{ background:var(--ether,#0a1a1f); }
body{ background:transparent; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14;
  -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent);
          mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
body::after{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:radial-gradient(ellipse at 20% 0%, rgba(247,147,26,0.06), transparent 55%); }
</style></head>
<body>
<nav style="display:flex;gap:2px;margin:0 0 20px;padding:6px 0;border-bottom:1px solid #1d3a42;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase;flex-wrap:wrap;" aria-label="Site menu">
  <a href="/" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Home</a>
  <a href="/progress" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Progress</a>
  <a href="/dj-handbook" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Guest DJ Guide</a>
  <a href="/manifesto" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Manifesto</a>
  <a href="/login" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;margin-left:auto;">Log in</a>
</nav>
<h1>DJ Portal -- log in</h1>
<p style="color:#8aa4aa;font-size:0.85rem;">One door for the whole crew — DJs and the captain both log in here.</p>
<form method="POST" action="/login" id="loginForm">
  <input name="dj_name" id="loginName" placeholder="DJ name" required maxlength="64" autocomplete="username">
  <input type="password" name="pass" id="loginPass" placeholder="Password" required maxlength="128" autocomplete="current-password">
  <button type="submit">Log in</button>
</form>
<details style="margin-top:16px;"><summary style="cursor:pointer;color:#8aa4aa;">No password yet, or lost it? Use your recovery code</summary>
  <form method="POST" action="/login">
    <input name="dj_name" placeholder="DJ name" required maxlength="40">
    <input name="code" placeholder="Recovery code (21 characters)" required maxlength="32">
    <button type="submit">Log in with code</button>
  </form>
  <p style="color:#8aa4aa;font-size:0.85rem;">Registered before passwords existed? Your old claim code IS your recovery code &mdash; log in with it once, then set a password inside.</p>
</details>
__RESULT__
<p style="margin-top:18px;">New here? <a href="/register">Register as a guest DJ &rarr;</a></p>
<p style="color:#8aa4aa;font-size:0.85rem;">No code either? Ask the captain: tag <b>@noderunnersfm</b> or <b>@plebroyale</b> in <a href="https://t.me/noderunnersradio">the Telegram</a>.</p>
<script>
document.getElementById('loginForm').addEventListener('submit', function(e){
  e.preventDefault();
  var form = this;
  var u = document.getElementById('loginName').value;
  var p = document.getElementById('loginPass').value;
  fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:'username=' + encodeURIComponent(u) + '&password=' + encodeURIComponent(p)})
    .then(function(r){ if (r.ok){ window.location = '/admin'; } else { form.submit(); } })
    .catch(function(){ form.submit(); });
});
</script>
</body></html>"""

PORTAL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DJ Portal -- Noderunners Radio</title>
<style>
body { background:#0a1a1f; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 520px; margin: 50px auto; padding: 0 20px; }
h1 { color: #f7931a; font-size: 1.3rem; }
.card { background:#0f262d; border:1px solid #1d3a42; border-radius:8px; padding:18px 20px; margin:16px 0; }
.muted { color:#8aa4aa; font-size:0.85rem; }
.avatar { width:128px; height:128px; border-radius:50%; object-fit:cover; border:3px solid #d06b29; background:#081418; display:block; margin:10px 0; }
button, .btn { padding:9px 16px; background:#f7931a; border:none; border-radius:4px; color:#0a1a1f; font-weight:bold; cursor:pointer; font-family:inherit; font-size:0.9rem; }
input, textarea { width:100%; padding:9px; margin:6px 0; background:#081418; border:1px solid #1d3a42; color:#e6eff0; border-radius:4px; box-sizing:border-box; font-family:inherit; font-size:0.9rem; }
textarea { resize:vertical; }
input[type=file] { color:#8aa4aa; margin:10px 0; max-width:100%; }
a { color:#f7931a; }
#msg { font-size:0.85rem; margin-top:8px; }
.mono { background:rgba(55,82,90,.3); border:1px solid #1d3a42; border-radius:4px; padding:1px 6px; word-break:break-all; }
/* portal drawers (SF 2026-08-26): the open tab is BOLD + marked + thick-underlined --
   three signals, none of them colour (captain and crew include colourblind eyes) */
.ptabs { display:flex; gap:2px; border-bottom:2px solid #1d3a42; margin:14px 0 6px; flex-wrap:wrap; }
.ptabs button { background:none; border:none; color:#8aa4aa; font-family:inherit; font-size:0.8rem;
  letter-spacing:0.06em; text-transform:uppercase; padding:8px 11px; cursor:pointer;
  border-bottom:3px solid transparent; border-radius:0; font-weight:normal; }
.ptabs button.on { color:#e6eff0; font-weight:700; border-bottom:3px solid #f7931a; }
.ptabs button.on::before { content:"\u25b8 "; }
.pane { display:none; }
.pane.on { display:block; }

html{ background:var(--ether,#0a1a1f); }
body{ background:transparent; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14;
  -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent);
          mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
body::after{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:radial-gradient(ellipse at 20% 0%, rgba(247,147,26,0.06), transparent 55%); }
</style></head>
<body>
<nav style="display:flex;gap:2px;margin:0 0 20px;padding:6px 0;border-bottom:1px solid #1d3a42;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase;flex-wrap:wrap;" aria-label="Site menu">
  <a href="/" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Home</a>
  <a href="/progress" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Progress</a>
  <a href="/dj-handbook" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Guest DJ Guide</a>
  <a href="/manifesto" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Manifesto</a>
  <a href="/dj/portal/logout" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;margin-left:auto;">Log out</a>
</nav>
<h1>Ahoy, __DJ_NAME__</h1>
<div class="ptabs" id="ptabs" role="tablist">
  <button type="button" data-pane="sats" class="on">Sats</button>
  <button type="button" data-pane="page">My page</button>
  <button type="button" data-pane="hosting">Hosting</button>
  __LIVE_TAB__
  <button type="button" data-pane="account">Account</button>
</div>
<div class="pane" data-pane="hosting">
__HOST_CARD__
</div>
<div class="pane" data-pane="live">
__LIVE_CARDS__
</div>
<div class="pane on" data-pane="sats">
<div class="card">
  <div class="muted"><b>Your payout address</b> &mdash; where session pay lands. __PAYOUT_STATE__</div>
  <form method="POST" action="/dj/portal/payout">
    <input name="payout" placeholder="you@walletofsatoshi.com &mdash; your OWN Lightning Address" value="__PAYOUT_VALUE__" maxlength="100">
    <button type="submit">Save payout address</button>
  </form>
  <p class="muted" style="margin-bottom:0;">Got your own wallet? Paste its Lightning Address and session payouts fly straight to <b>you</b> &mdash; never parking on the station. One small Lightning routing fee per payout. Leave empty to use your station wallet instead.</p>
</div>
<div class="card">
  <div class="muted">Sats parked on the station:</div>
  <p style="font-size:1.5rem;margin:8px 0;"><b>__BALANCE__</b></p>
  <p style="border:1px solid #f7931a;border-radius:6px;padding:10px 12px;line-height:1.55;margin:10px 0;">&#9888; <b>THE STATION IS NOT A BANK.</b> Sats that land here are yours &mdash; move them to a wallet only <b>you</b> control, after every session. Self-custody, always.</p>
  <p><a class="btn" href="__WALLET_URL__" target="_blank" rel="noopener">Open my station wallet &#8599;</a></p>
  <p class="muted">Inside the wallet: hit <b>Send</b>, paste an invoice or Lightning Address from your own wallet (Wallet of Satoshi, Phoenix, Breez, your own node...), send everything. That link is your full access &mdash; treat it like cash and don't share it.</p>
  <div class="muted">Your Lightning Address (where session payouts land): <span class="mono"><!--email_off-->__LN_ADDRESS__<!--/email_off--></span></div>
  <p class="muted" style="margin-bottom:0;">DJ name &amp; address are <b>set in stone</b> (one DJ = one name = one address). Need a change? Tag <b>@noderunnersfm</b> or <b>@plebroyale</b> in <a href="https://t.me/noderunnersradio" target="_blank" rel="noopener">t.me/noderunnersradio</a>.</p>
</div>
</div>
<div class="pane" data-pane="account">
<div class="card">
  <div class="muted">__PASS_HEAD__</div>
  <form method="POST" action="/dj/portal/setpass">
    <input type="password" name="pass" placeholder="New password (min 12 characters)" required minlength="12" maxlength="128">
    <input type="password" name="pass2" placeholder="Repeat it" required minlength="12" maxlength="128">
    <button type="submit">Save password</button>
  </form>
</div>
</div>
<div class="pane" data-pane="page">
<div class="card">
  <div class="muted">Your bio &amp; links &mdash; shown with your avatar on your public DJ page (noderunnersradio.com/YourName):</div>
  <form method="POST" action="/dj/portal/profile">
    <textarea name="blurb" rows="3" maxlength="280" placeholder="Short blurb, 280 characters max">__BLURB__</textarea>
    <input name="link1" placeholder="Link (https://...)" value="__LINK1__" maxlength="200">
    <input name="link2" placeholder="Link (https://...)" value="__LINK2__" maxlength="200">
    <input name="link3" placeholder="Link (https://...)" value="__LINK3__" maxlength="200">
    <input name="telegram" placeholder="Telegram @handle (optional -- shown on your public page)" value="__TG__" maxlength="33">
    <button type="submit">Save profile</button>
  </form>
</div>
<div class="card">
  <div class="muted">Your avatar -- shows on the live stream and the site. Pick any image; it's resized in your browser to a lightweight 256px square before upload (max ~400 KB).</div>
  <img id="av" class="avatar" src="__AVATAR_SRC__" onerror="this.style.opacity=.25">
  <input type="file" id="file" accept="image/*">
  <div id="cropBox" style="display:none;">
    <canvas id="cropCanvas" width="256" height="256" style="width:220px;height:220px;border-radius:50%;border:2px solid #1d3a42;touch-action:none;cursor:grab;background:#081418;display:block;margin:8px 0;"></canvas>
    <input type="range" id="zoom" min="100" max="300" value="100">
    <div class="muted">Drag the picture to position it, use the slider to zoom.</div>
    <button type="button" id="saveAvatar" style="margin-top:6px;">Save avatar</button>
  </div>
  <div id="msg" class="muted"></div>
</div>
</div>
<p><a href="/dj/request-slot?dj_name=__DJ_NAME_URL__">Request a DJ slot &rarr;</a> &middot; <a href="/dj/portal/logout">Log out</a></p>
<script>
(function(){
  var input = document.getElementById('file');
  var msg = document.getElementById('msg');
  var av = document.getElementById('av');
  var box = document.getElementById('cropBox');
  var cv = document.getElementById('cropCanvas');
  var ctx = cv.getContext('2d');
  var zoomEl = document.getElementById('zoom');
  var img = null, base = 1, z = 1, ox = 0, oy = 0;

  function clamp(){
    var dw = img.naturalWidth * base * z, dh = img.naturalHeight * base * z;
    if (ox > 0) ox = 0;
    if (oy > 0) oy = 0;
    if (ox < 256 - dw) ox = 256 - dw;
    if (oy < 256 - dh) oy = 256 - dh;
  }
  function draw(){
    if (!img) return;
    clamp();
    ctx.clearRect(0, 0, 256, 256);
    ctx.drawImage(img, ox, oy, img.naturalWidth * base * z, img.naturalHeight * base * z);
  }
  input.addEventListener('change', function(){
    var f = input.files && input.files[0];
    if (!f) return;
    img = new Image();
    img.onload = function(){
      base = 256 / Math.min(img.naturalWidth, img.naturalHeight);
      z = 1; zoomEl.value = 100;
      ox = (256 - img.naturalWidth * base) / 2;
      oy = (256 - img.naturalHeight * base) / 2;
      box.style.display = 'block';
      msg.textContent = 'Drag to position, slide to zoom, then hit Save.';
      draw();
    };
    img.onerror = function(){ msg.textContent = 'That does not look like an image.'; };
    img.src = URL.createObjectURL(f);
  });
  zoomEl.addEventListener('input', function(){
    if (!img) return;
    var nz = parseInt(zoomEl.value, 10) / 100;
    ox = 128 - (128 - ox) * (nz / z);
    oy = 128 - (128 - oy) * (nz / z);
    z = nz;
    draw();
  });
  var drag = null;
  cv.addEventListener('pointerdown', function(e){
    e.preventDefault();
    drag = {x: e.clientX, y: e.clientY};
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener('pointermove', function(e){
    if (!drag) return;
    var r = cv.getBoundingClientRect();
    var s = 256 / r.width;
    ox += (e.clientX - drag.x) * s;
    oy += (e.clientY - drag.y) * s;
    drag = {x: e.clientX, y: e.clientY};
    draw();
  });
  cv.addEventListener('pointerup', function(){ drag = null; });
  cv.addEventListener('pointercancel', function(){ drag = null; });
  document.getElementById('saveAvatar').addEventListener('click', function(){
    if (!img) return;
    function upload(blob){
      if (!blob){ msg.textContent = 'Could not convert this image.'; return; }
      if (blob.size > 400000){ msg.textContent = 'Still too big -- zoom in a bit or pick a simpler image.'; return; }
      msg.textContent = 'Uploading (' + Math.round(blob.size / 1024) + ' KB)...';
      fetch('/dj/portal/avatar', { method:'POST', headers:{'Content-Type': blob.type}, body: blob })
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
        .then(function(res){
          if (!res.ok){ msg.textContent = res.d.error || 'Upload failed.'; return; }
          msg.textContent = 'Avatar updated!';
          av.style.opacity = 1;
          av.src = res.d.url + '?t=' + Date.now();
          box.style.display = 'none';
        }).catch(function(){ msg.textContent = 'Upload failed -- try again.'; });
    }
    cv.toBlob(function(b){
      if (b) upload(b);
      else cv.toBlob(function(b2){ upload(b2); }, 'image/png');
    }, 'image/webp', 0.85);
  });
})();
</script>
<script>
(function(){
  var bs = document.querySelectorAll('#ptabs button');
  var ps = document.querySelectorAll('.pane');
  function openPane(name){
    var found = false;
    Array.prototype.forEach.call(ps, function(p){
      var on = p.getAttribute('data-pane') === name;
      p.classList.toggle('on', on);
      if (on) found = true;
    });
    Array.prototype.forEach.call(bs, function(b){
      b.classList.toggle('on', b.getAttribute('data-pane') === name);
    });
    return found;
  }
  Array.prototype.forEach.call(bs, function(b){
    b.addEventListener('click', function(){
      openPane(b.getAttribute('data-pane'));
      try { localStorage.setItem('nrPortalTab', b.getAttribute('data-pane')); } catch(err){}
    });
  });
  var hasLive = document.querySelector('#ptabs button[data-pane="live"]') !== null;
  var last = null;
  try { last = localStorage.getItem('nrPortalTab'); } catch(err){}
  if (last && document.querySelector('#ptabs button[data-pane="' + last + '"]')) openPane(last);
  if (hasLive){
    // a host with a session OPEN lands straight on the Live drawer -- the
    // mid-set tools must never be buried (SF 2026-08-26)
    fetch('/api/livesplit', {cache:'no-store'}).then(function(r){ return r.json(); })
      .then(function(d){ if (d && d.live) openPane('live'); }).catch(function(){});
  }
})();
</script>
</body></html>"""


PUBLIC_DJ_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__DJ_NAME__ -- Noderunners Radio DJ</title>
<style>
body { background:transparent; color:#e6eff0; font-family: ui-monospace, monospace; max-width: 520px; margin: 50px auto; padding: 0 20px; }
html { background:#0a1a1f; }
body::before{ content:""; position:fixed; inset:0; z-index:-1; pointer-events:none; background:url("/assets/ship-bg.jpg") center 20% / cover no-repeat; opacity:0.14; -webkit-mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); mask-image:linear-gradient(to bottom, rgba(0,0,0,0.9), rgba(0,0,0,0.25) 55%, transparent); }
h1 { color: #f7931a; font-size: 1.4rem; margin: 12px 0 6px; }
.card { background:rgba(15,38,45,0.82); border:1px solid #1d3a42; border-radius:8px; padding:20px 22px; margin:16px 0; }
.avatar { width:128px; height:128px; border-radius:50%; object-fit:cover; border:3px solid #d06b29; background:#081418; display:block; }
.muted { color:#8aa4aa; font-size:0.85rem; }
a { color:#f7931a; }
.mono { background:rgba(55,82,90,.3); border:1px solid #1d3a42; border-radius:4px; padding:1px 6px; word-break:break-all; }
</style></head>
<body>
<nav style="display:flex;gap:2px;margin:0 0 20px;padding:6px 0;border-bottom:1px solid #1d3a42;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase;flex-wrap:wrap;" aria-label="Site menu">
  <a href="/" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Home</a>
  <a href="/progress" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Progress</a>
  <a href="/dj-handbook" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Guest DJ Guide</a>
  <a href="/manifesto" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;">Manifesto</a>
  <a href="/login" style="color:#8aa4aa;text-decoration:none;padding:2px 10px;margin-left:auto;">Log in</a>
</nav>
<div class="card">
  <img class="avatar" src="__AVATAR_SRC__" onerror="this.style.opacity=.25" alt="">
  <h1>__DJ_NAME__</h1>
  __BLURB__
  __LINKS__
  <p class="muted">Tip __DJ_NAME__ directly, listener-to-artist: <span class="mono"><!--email_off-->__LN_ADDRESS__<!--/email_off--></span></p>
__TG_LINE__
</div>
<p class="muted">Guest DJ aboard <a href="/">Noderunners Radio</a> &mdash; <a href="/manifesto">the public is the DJ</a>.</p>
<p class="muted">Are you __DJ_NAME__? <a href="/login">Log in to your quarters &rarr;</a></p>
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS invite_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact TEXT,
            message TEXT,
            created_at INTEGER,
            handled INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS host_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_name TEXT NOT NULL,
            session_date TEXT NOT NULL,
            venue_label TEXT NOT NULL,
            slots INTEGER NOT NULL DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT,
            created_at INTEGER,
            decided_at INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS session_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            length_min INTEGER NOT NULL DEFAULT 60,
            dj_name TEXT
        )
    """)
    # portal columns (safe to re-run; ALTER fails silently when present)
    for ddl in ("ALTER TABLE dj_registrations ADD COLUMN claim_code TEXT",
                "ALTER TABLE host_sessions ADD COLUMN start_time TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN avatar TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN pass_hash TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN blurb TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN links TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN wallet_inkey TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN removed INTEGER DEFAULT 0",
                "ALTER TABLE dj_registrations ADD COLUMN removed_at INTEGER",
                "ALTER TABLE dj_registrations ADD COLUMN payout_address TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN is_host INTEGER DEFAULT 0",
                "ALTER TABLE dj_registrations ADD COLUMN venue_label TEXT",
                "ALTER TABLE dj_registrations ADD COLUMN telegram_handle TEXT"):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    # recovery codes: backfill missing ones AND upgrade short pre-2026-07-24 codes
    # to the 21-character format. Old 8-char codes stop working here -- the console
    # crew list always shows the CURRENT code, so SF always DMs the right one.
    for (rid,) in con.execute(
            "SELECT id FROM dj_registrations WHERE claim_code IS NULL OR length(claim_code) < 21").fetchall():
        con.execute("UPDATE dj_registrations SET claim_code = ? WHERE id = ?",
                    (_new_code(), rid))
    con.commit()
    con.close()
    os.makedirs(AVATAR_DIR, exist_ok=True)


def _esc_attr(s: str) -> str:
    """Escape a value for safe embedding in an HTML attribute."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    return slug[:20] or "dj"


def _record_registration(name, user_id, wallet_id, ln_address, claim_code, pass_hash=None, wallet_inkey=None):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO dj_registrations (dj_name, lnbits_user_id, lnbits_wallet_id, lightning_address, registered_at, claim_code, pass_hash, wallet_inkey) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, user_id, wallet_id, ln_address, int(time.time()), claim_code, pass_hash, wallet_inkey),
    )
    con.commit()
    con.close()


def _find_dj_by_login(name, code):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id FROM dj_registrations WHERE lower(dj_name) = lower(?) AND claim_code = ? AND COALESCE(removed, 0) = 0",
        (name.strip(), code.strip()),
    ).fetchone()
    con.close()
    return row[0] if row else None


PASS_ITERATIONS = 200_000

# LNbits' own sqlite -- read-only, ONLY to look up a wallet's invoice/read key
# for DJs registered before we started storing it ourselves. Never written to.
LNBITS_DB_PATH = os.environ.get("LNBITS_DB_PATH", "/opt/lightning-stack/lnbits_data/database.sqlite3")


def _hash_pass(passphrase):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), bytes.fromhex(salt), PASS_ITERATIONS)
    return f"{PASS_ITERATIONS}${salt}${dk.hex()}"


def _check_pass(passphrase, stored):
    try:
        iters, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, AttributeError):
        return False


CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no confusable characters


def _new_code():
    """21-character recovery code (~103 bits) -- SF picked the length, of course."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(21))


def _name_taken(name):
    # counts removed DJs too -- names are set in stone, even retired ones stay reserved
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM dj_registrations WHERE lower(dj_name) = lower(?)", (name.strip(),)).fetchone()
    con.close()
    return row is not None


def _find_dj_by_pass(name, passphrase):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, pass_hash FROM dj_registrations WHERE lower(dj_name) = lower(?) AND COALESCE(removed, 0) = 0",
        (name.strip(),),
    ).fetchone()
    con.close()
    if row and row[1] and _check_pass(passphrase, row[1]):
        return row[0]
    return None


def _set_pass(reg_id, passphrase):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE dj_registrations SET pass_hash = ? WHERE id = ?", (_hash_pass(passphrase), reg_id))
    con.commit()
    con.close()


def _set_profile(reg_id, blurb, links, telegram=""):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE dj_registrations SET blurb = ?, links = ?, telegram_handle = ? WHERE id = ?",
                (blurb, links, telegram, reg_id))
    con.commit()
    con.close()


def _portal_dest(reg_id):
    """After any portal action, land the DJ on their own pretty page, not
    /login (SF 2026-07-30: the ugly URL kept surfacing after every save)."""
    row = _dj_row(reg_id)
    return ("/" + quote(row[1])) if row else "/login"


def _dj_row_full(reg_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, dj_name, lightning_address, avatar, pass_hash, blurb, links, lnbits_user_id, lnbits_wallet_id, payout_address, COALESCE(is_host, 0), COALESCE(venue_label, ''), COALESCE(telegram_handle, '') FROM dj_registrations WHERE id = ?",
        (reg_id,),
    ).fetchone()
    con.close()
    return row


def _wallet_inkey(reg_id):
    """Invoice/read key for the DJ's wallet: our own record first, else a one-time
    read-only lookup in LNbits' db (pre-portal-v2 DJs), cached back into our row."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT wallet_inkey, lnbits_wallet_id FROM dj_registrations WHERE id = ?", (reg_id,)).fetchone()
    con.close()
    if not row:
        return None
    inkey, wallet_id = row
    if inkey:
        return inkey
    if not wallet_id:
        return None
    try:
        lcon = sqlite3.connect(f"file:{LNBITS_DB_PATH}?mode=ro", uri=True, timeout=2)
        lrow = lcon.execute("SELECT inkey FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
        lcon.close()
    except sqlite3.Error:
        return None
    if not lrow or not lrow[0]:
        return None
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE dj_registrations SET wallet_inkey = ? WHERE id = ?", (lrow[0], reg_id))
    con.commit()
    con.close()
    return lrow[0]


def _list_host_sheets(name):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, session_date, venue_label, slots, status, COALESCE(note,'') FROM host_sessions "
        "WHERE lower(host_name) = lower(?) ORDER BY session_date ASC, id ASC LIMIT 12", (name,)).fetchall()
    con.close()
    return rows


def _add_host_sheet(name, date, venue, start_time, lengths, slot_djs=None):
    # lengths = ordered slot lengths in minutes; slot_djs = optional parallel
    # DJ names (the host sets the lineup -- SF's ruling 2026-07-30)
    slot_djs = slot_djs or []
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO host_sessions (host_name, session_date, venue_label, slots, start_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (name, date, venue, len(lengths), start_time, int(time.time())))
    sid = cur.lastrowid
    for pos, mins in enumerate(lengths):
        dj = (slot_djs[pos].strip()[:40] if pos < len(slot_djs) else "") or None
        con.execute("INSERT INTO session_slots (session_id, position, length_min, dj_name) VALUES (?, ?, ?, ?)",
                    (sid, pos, mins, dj))
    con.commit()
    con.close()


def _slot_times(start_time, lengths):
    """['20:00', '21:00', ...] — each slot's start, from the sheet's start time."""
    try:
        h, m = (int(x) for x in (start_time or "20:00").split(":"))
    except ValueError:
        h, m = 20, 0
    out, mins = [], h * 60 + m
    for ln in lengths:
        out.append(f"{(mins // 60) % 24:02d}:{mins % 60:02d}")
        mins += ln
    return out


def _sheet_start(session_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT COALESCE(start_time, '20:00') FROM host_sessions WHERE id = ?", (session_id,)).fetchone()
    con.close()
    return row[0] if row else "20:00"


def _sheet_slots(session_id):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT position, length_min, COALESCE(dj_name,'') FROM session_slots "
        "WHERE session_id = ? ORDER BY position", (session_id,)).fetchall()
    con.close()
    return rows


def _hosting_toggle_html(is_host, venue):
    """Self-service hosting (SF's ruling 2026-07-30, bench v3): every DJ flips
    this on their own page. The captain still approves every sheet, so the
    safety net does not move. State = words + knob position, never color."""
    if is_host:
        word, action, btn = "I can HOST at my place", "0", "Switch OFF &mdash; I stop hosting"
        venue_row = f"""
  <form method="POST" action="/dj/portal/hostmode" style="margin-top:8px;">
    <input type="hidden" name="on" value="1">
    <input name="venue" value="{_esc_attr(venue)}" placeholder="Your venue label, e.g. SOL's place" maxlength="60" required>
    <button type="submit">Save venue</button>
  </form>
  <div class="muted" style="font-size:0.8rem;">Your sessions happen at YOUR place &mdash; a different venue is a different host. Sheets you publish carry this label.</div>"""
    else:
        word, action, btn = "I do NOT host", "1", "Switch ON &mdash; I can host at my place"
        venue_row = ""
    return f"""
<div class="card">
  <b>&#9875; Hosting</b> &mdash; <b>{word}</b>
  <form method="POST" action="/dj/portal/hostmode" style="margin-top:6px;">
    <input type="hidden" name="on" value="{action}">
    {'' if is_host else '<input name="venue" placeholder="Your venue label, e.g. SOL' + chr(39) + 's place" maxlength="60" required>'}
    <button type="submit">{btn}</button>
  </form>{venue_row}
  <div class="muted" style="font-size:0.8rem;margin-top:4px;">Flip this if you have gear and a place and want to run sessions. The captain approves every session sheet before it goes public.</div>
</div>"""


def _host_card_html(name, venue=""):
    # state carried by symbol + words, never color alone
    stat_words = {"pending": "&#8987; waiting for the captain",
                  "published": "&#10004; PUBLIC -- on the calendar",
                  "rejected": "&#10008; rejected",
                  "removed": "&#10008; removed from the calendar by the captain"}
    rows = _list_host_sheets(name)
    sheets = ""
    if rows:
        parts = []
        for sid, d, v, s, st, n in rows:
            slots = _sheet_slots(sid)
            if slots:
                times = _slot_times(_sheet_start(sid), [ln for _p, ln, _dj in slots])
                slot_txt = " &middot; ".join(
                    f"{t}&nbsp;({ln}&prime;{' · ' + _esc_attr(dj) if dj else ''})"
                    for t, (_p, ln, dj) in zip(times, slots))
            else:
                slot_txt = f"{int(s)} slots"
            parts.append(
                f'<div style="padding:4px 0;border-top:1px dashed rgba(138,164,170,0.35);">'
                f'{_esc_attr(d)} &middot; {_esc_attr(v)} &mdash; {stat_words.get(st, _esc_attr(st))}'
                f'<br><span class="muted" style="font-size:0.8rem;">{slot_txt}</span>'
                + (f'<br><span class="muted" style="font-size:0.8rem;">captain\'s note: {_esc_attr(n)}</span>' if n else "")
                + "</div>")
        sheets = '<div style="margin-top:10px;"><b>Your session sheets</b>' + "".join(parts) + "</div>"
    return f"""
<div class="card">
  <div class="muted"><b>&#9875; HOST MODE is ON for your account</b> &mdash; you have the gear and the venue: publish dates, DJs sign up.</div>
  <form method="POST" action="/dj/portal/hostsheet" id="sheetForm">
    <input type="date" name="date" required min="{time.strftime('%Y-%m-%d')}" title="Pick the session date -- today or later">
    <input name="venue" value="{_esc_attr(venue)}" readonly title="Your registered venue from the Hosting card -- sessions happen at YOUR place." style="opacity:0.65;border-style:dashed;">
    <input type="time" name="start" required title="First slot start time" style="max-width:280px;">
    <div id="slotRows" style="margin:10px 0 4px;"></div>
    <button type="button" id="addSlot" style="margin-left:0;background:transparent;color:#e6eff0;border:1px dashed #8aa4aa;">+ add a slot</button>
    <div style="margin-top:10px;">
      <button type="submit">Send session sheet to the captain</button>
    </div>
  </form>
  <p class="muted" style="margin-bottom:0;">You set the running order and each slot's length &mdash; the times compute themselves. Nothing goes public until the captain approves.</p>
  {sheets}
</div>
<script>
(function(){{
  var box = document.getElementById('slotRows'), form = document.getElementById('sheetForm');
  var lens = [60, 60, 60];
  var djs = ['', '', ''];   /* per-slot DJ name, optional (SF's ruling: hosts set the lineup) */
  function eA(s){{ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }}
  function startMins(){{
    var m = /^([0-9]{{1,2}}):([0-9]{{2}})$/.exec(form.start.value.trim());
    return m ? (parseInt(m[1],10)*60 + parseInt(m[2],10)) : null;
  }}
  function fmt(mins){{ mins = ((mins % 1440) + 1440) % 1440;
    return ('0'+Math.floor(mins/60)).slice(-2) + ':' + ('0'+(mins%60)).slice(-2); }}
  function draw(){{
    var s = startMins(), t = s, html = '';
    lens.forEach(function(ln, i){{
      var when = (s === null) ? '--:--' : fmt(t); if (s !== null) t += ln;
      html += '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;flex-wrap:wrap;">' +
        '<b style="min-width:56px;color:#f5a623;">' + when + '</b>' +
        '<span>slot ' + (i+1) + ':</span>' +
        '<input type="number" name="slotlen" value="' + ln + '" min="10" max="240" step="5" data-i="' + i + '" style="width:80px;"> min' +
        '<input name="slotdj" value="' + eA(djs[i]) + '" placeholder="DJ (optional)" maxlength="40" data-i="' + i + '" style="width:150px;">' +
        '<span style="white-space:nowrap;display:inline-flex;gap:6px;">' +
        '<button type="button" class="mv" data-i="' + i + '" data-d="-1" ' + (i===0?'disabled':'') + ' style="padding:2px 8px;background:transparent;color:#e6eff0;border:1px solid #8aa4aa;">&#9650;</button>' +
        '<button type="button" class="mv" data-i="' + i + '" data-d="1" ' + (i===lens.length-1?'disabled':'') + ' style="padding:2px 8px;background:transparent;color:#e6eff0;border:1px solid #8aa4aa;">&#9660;</button>' +
        '<button type="button" class="rm" data-i="' + i + '" ' + (lens.length<2?'disabled':'') + ' style="padding:2px 8px;background:transparent;color:#ff6b6b;border:1px solid #ff6b6b;">&#10005;</button>' +
        '</span></div>';
    }});
    box.innerHTML = html;
  }}
  box.addEventListener('input', function(e){{
    var t = e.target;
    if (t.name === 'slotdj'){{ djs[t.getAttribute('data-i')] = t.value; return; }}
    if (t.name !== 'slotlen') return;
    lens[t.getAttribute('data-i')] = Math.max(10, Math.min(240, parseInt(t.value,10) || 60));
    var keep = document.activeElement === t;  /* live-update times without stealing focus */
    draw();
    if (keep){{ var again = box.querySelector('input[name="slotlen"][data-i="' + t.getAttribute('data-i') + '"]'); if (again){{ again.focus(); }} }}
  }});
  box.addEventListener('click', function(e){{
    var mv = e.target.closest('.mv');
    if (mv){{ var i = +mv.getAttribute('data-i'), d = +mv.getAttribute('data-d');
      var x = lens[i]; lens[i] = lens[i+d]; lens[i+d] = x;
      var y = djs[i]; djs[i] = djs[i+d]; djs[i+d] = y; draw(); return; }}
    var rm = e.target.closest('.rm');
    if (rm && lens.length > 1){{ var ri = +rm.getAttribute('data-i');
      lens.splice(ri, 1); djs.splice(ri, 1); draw(); }}
  }});
  document.getElementById('addSlot').addEventListener('click', function(){{
    if (lens.length < 12){{ lens.push(60); djs.push(''); }} draw();
  }});
  form.start.addEventListener('input', draw);
  draw();
}})();
</script>"""


def _set_payout(reg_id, address):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE dj_registrations SET payout_address = ? WHERE id = ?", (address, reg_id))
    con.commit()
    con.close()


def _heal_wallet_ids(reg_id):
    """True (user_id, wallet_id) for the DJ's LNbits wallet. Wallet ids stored
    before 2026-07-24 could be wrong (caused LNbits' "Wallet not found" page);
    verify read-only against LNbits' own db by USER id and cache the correction."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT lnbits_user_id, lnbits_wallet_id, wallet_inkey FROM dj_registrations WHERE id = ?", (reg_id,)).fetchone()
    con.close()
    if not row:
        return (None, None)
    user_id, wallet_id, inkey = row
    if not user_id:
        return (None, None)
    try:
        lcon = sqlite3.connect(f"file:{LNBITS_DB_PATH}?mode=ro", uri=True, timeout=2)
        lrow = lcon.execute('SELECT id, inkey FROM wallets WHERE user = ? LIMIT 1', (user_id,)).fetchone()
        lcon.close()
    except sqlite3.Error:
        return (user_id, wallet_id)
    if lrow and lrow[0]:
        real_id, real_inkey = lrow
        if real_id != wallet_id or (real_inkey and real_inkey != inkey):
            con = sqlite3.connect(DB_PATH)
            con.execute("UPDATE dj_registrations SET lnbits_wallet_id = ?, wallet_inkey = ? WHERE id = ?",
                        (real_id, real_inkey or inkey, reg_id))
            con.commit()
            con.close()
        return (user_id, real_id)
    return (user_id, wallet_id)


def _wallet_balance_sats(reg_id):
    inkey = _wallet_inkey(reg_id)
    if not inkey:
        return None
    try:
        r = requests.get(f"{LNBITS_BASE_URL}/api/v1/wallet", headers={"X-Api-Key": inkey}, timeout=8)
        if r.status_code < 300:
            return int(r.json().get("balance", 0)) // 1000
    except (requests.RequestException, ValueError):
        pass
    return None


def _dj_row(reg_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, dj_name, lightning_address, avatar FROM dj_registrations WHERE id = ?",
        (reg_id,),
    ).fetchone()
    con.close()
    return row


def _set_avatar(reg_id, filename):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE dj_registrations SET avatar = ? WHERE id = ?", (filename, reg_id))
    con.commit()
    con.close()


def _avatar_map():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT dj_name, avatar FROM dj_registrations WHERE avatar IS NOT NULL AND avatar != '' AND COALESCE(removed, 0) = 0"
    ).fetchall()
    con.close()
    return {name: "/assets/avatars/" + av for name, av in rows}


def _mint_portal_cookie(reg_id):
    exp = str(int(time.time()) + PORTAL_TTL_S)
    sig = hmac.new(ADMIN_SESSION_SECRET.encode(), f"dj|{reg_id}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{reg_id}.{exp}.{sig}"


def _portal_auth(cookie_header):
    """Returns the DJ's registration id from a valid portal cookie, else None."""
    if not ADMIN_SESSION_SECRET or not cookie_header:
        return None
    val = ""
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == PORTAL_COOKIE:
            val = v
    try:
        reg_id, exp, sig = val.split(".")
        good = hmac.new(ADMIN_SESSION_SECRET.encode(), f"dj|{reg_id}|{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return int(reg_id)
    except (ValueError, AttributeError):
        pass
    return None




def _mint_console_session():
    """Same shape as library_api's _make_session -- the two services share
    ADMIN_SESSION_SECRET by design (see the portal-cookie note above). 60 s
    life: it exists only for the localhost hop in the host-door relays."""
    exp = str(int(time.time()) + 60)
    sig = hmac.new(ADMIN_SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _host_of(reg_id):
    """(dj_name, venue_label) when the registration is an active host, else None."""
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT dj_name, COALESCE(is_host,0), COALESCE(venue_label,'') "
            "FROM dj_registrations WHERE id = ? AND COALESCE(removed,0) = 0",
            (reg_id,)).fetchone()
    finally:
        con.close()
    return (row[0], row[2]) if row and row[1] else None


def _venue_slug(label):
    """Mirror of library_api's venue key rule -- keep the two in step."""
    return "".join(c for c in (label or "").lower() if c.isalnum() or c in "_-")[:24] or "default"


# Plain string on purpose -- no f-string, so the JS braces stay sane.
# B&W-first (SF): the ON state is solid border + filled + arrow prefix + bold;
# OFF is a dashed outline. Shape carries the meaning, color is a bonus.
ONDECK_CARD = """
<div class="card">
  <div class="muted"><b>&#127911; ON DECK &mdash; highlight who is playing</b> &mdash; tap a name when the DJ changes, tap again to unmark. The overlay follows in ~10&nbsp;s. Highlight only &mdash; it never moves sats.</div>
  <div id="ondeckRow" class="muted" style="margin-top:8px;">checking for an open session&hellip;</div>
</div>
<div class="card">
  <div class="muted"><b>&#128204; YOUR OVERLAY</b> &mdash; every venue has its own overlay URL. Yours:</div>
  <div style="font-size:0.85rem;word-break:break-all;margin-top:6px;">
    OBS browser source: <b>https://noderunnersradio.com/overlay?venue=__VENUE__</b><br>
    Things shifted at your place? <a href="/overlay?venue=__VENUE__&amp;edit=1" target="_blank">Open the drag editor</a> &mdash; drag the pieces, press SAVE. Your host login is enough; OBS picks it up in ~10&nbsp;s without touching the source.
  </div>
</div>
<script>
(function(){
  var row = document.getElementById('ondeckRow');
  function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }
  function draw(d){
    if (!d.live){ row.textContent = 'No session open right now -- this card wakes up during a live sesh.'; return; }
    var now = d.now_djs || [];
    row.innerHTML = (d.djs || []).map(function(n){
      var on = now.indexOf(n) >= 0;
      return '<button type="button" class="odbtn" data-dj="' + esc(n) + '" style="margin:3px 6px 3px 0;padding:6px 12px;border:2px ' +
        (on ? 'solid' : 'dashed') + ' #f5a623;background:' + (on ? '#f5a623' : 'transparent') +
        ';color:' + (on ? '#10151a' : '#e6eff0') + ';font-weight:' + (on ? '700' : '400') + ';">' +
        (on ? '&#9654; ' : '') + esc(n) + '</button>';
    }).join('') || 'Session open, but the pool is empty.';
  }
  function poll(){ fetch('/api/livesplit', {cache:'no-store'}).then(function(r){ return r.json(); }).then(draw).catch(function(){}); }
  document.addEventListener('click', function(e){
    var b = e.target.closest('.odbtn'); if (!b) return;
    b.disabled = true;
    fetch('/dj/portal/ondeck', { method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'dj=' + encodeURIComponent(b.getAttribute('data-dj')) })
      .then(function(r){ return r.json(); })
      .then(function(d){ if (d.error){ row.textContent = d.error; setTimeout(poll, 1500); } else { poll(); } })
      .catch(function(){ b.disabled = false; });
  });
  poll(); setInterval(poll, 10000);
})();
</script>"""


def _ondeck_card_html(venue):
    """Host-door cards: ON DECK highlight + the venue's own overlay links."""
    return ONDECK_CARD.replace("__VENUE__", _venue_slug(venue))


def _record_slot_request(dj_name, telegram_handle, nostr_npub, slot_date, hour_cet):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO slot_requests (dj_name, telegram_handle, nostr_npub, slot_date, hour_cet, status, requested_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (dj_name, telegram_handle, nostr_npub, slot_date, hour_cet, int(time.time())),
    )
    con.commit()
    con.close()


def _list_registrations():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT dj_name, lightning_address, lnbits_wallet_id, claim_code, avatar, payout_address, COALESCE(is_host, 0) FROM dj_registrations WHERE COALESCE(removed, 0) = 0 ORDER BY registered_at DESC"
    ).fetchall()
    con.close()
    return rows


def _list_removed():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT dj_name, removed_at FROM dj_registrations WHERE COALESCE(removed, 0) = 1 ORDER BY removed_at DESC"
    ).fetchall()
    con.close()
    return rows


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


def create_dj_wallet(dj_name: str, pass_hash=None) -> dict:
    """Creates an LNbits user + wallet + Lightning Address for a new DJ.
    Raises RuntimeError with a plain-language message on any failure -- one bad
    registration must not crash the server for the next DJ."""
    slug = _slugify(dj_name)

    # LNbits v1.4's /users/ admin API wants a login Bearer token, not a wallet
    # key (the old two-call flow here 401'd). This instance allows open account
    # creation, so one unauthenticated call mints user + wallet in one go.
    # NOTE: if "allow new accounts" is ever disabled in LNbits settings, this
    # breaks and needs an ACL-token rework -- see project memory.
    resp = requests.post(
        f"{LNBITS_BASE_URL}/api/v1/account",
        json={"name": f"{dj_name} -- DJ wallet"},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Could not create LNbits wallet: {resp.status_code} {resp.text}")
    wallet = resp.json()
    user_id = wallet.get("user")
    wallet_id = wallet.get("id")
    wallet_api_key = wallet.get("adminkey") or wallet.get("inkey")

    # a fresh user has no extensions enabled -- switch on lnurlp for them,
    # otherwise the pay-link call below 403s ("Extension 'lnurlp' not enabled")
    resp = requests.put(
        f"{LNBITS_BASE_URL}/api/v1/extension/lnurlp/enable",
        params={"usr": user_id},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Could not enable lnurlp for the new wallet: {resp.status_code} {resp.text}")

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
    ln_address = f"{slug}@{LN_ADDRESS_DOMAIN}"

    claim_code = _new_code()
    _record_registration(dj_name, user_id, wallet_id, ln_address, claim_code,
                         pass_hash=pass_hash, wallet_inkey=wallet.get("inkey"))
    return {"dj_name": dj_name, "wallet_id": wallet_id, "lightning_address": ln_address,
            "claim_code": claim_code}


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


def _tg_tag(dj_name):
    """'@handle ' for a DJ who saved one on their portal, else ''. Used in GROUP
    pings only -- a tag notifies without DM permission (Silly Goose, 2026-08-27).
    Telegram handles are [A-Za-z0-9_], and ours are sanitised on save; strip
    anything else so a stray value can never break a broadcast message."""
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT COALESCE(telegram_handle, '') FROM dj_registrations "
            "WHERE dj_name = ? AND COALESCE(removed, 0) = 0", (dj_name,)).fetchone()
        con.close()
        h = "".join(c for c in ((row[0] if row else "") or "") if c.isalnum() or c == "_")[:32]
        return ("@" + h + " ") if h else ""
    except Exception:
        return ""


def _notify_new_host_sheet(host_name, date, venue, slots, start="20:00"):
    # public doorbell (no captain-only detail) + the captain's own DM with the link
    _telegram_send(
        TELEGRAM_CHAT_ID,
        f"{_tg_tag(host_name)}{host_name} offered to host a session: {date} from {start} at {venue}, "
        f"{slots} DJ slot(s) -- awaiting the captain's approval. @noderunnersfm @plebroyale",
    )
    _telegram_send(
        TELEGRAM_ADMIN_CHAT_ID,
        f"New session sheet: {host_name}, {date} {start}, {venue}, {slots} slot(s). Approve: {ADMIN_URL}",
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

    def _session_cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _check_admin(self, qs):
        if _session_valid(self._session_cookie()):
            return True
        token = (qs.get("token") or [""])[0]
        if not DJ_ADMIN_TOKEN or token != DJ_ADMIN_TOKEN:
            self._html(403, "forbidden")
            return False
        return True

    def _serve_portal(self, reg_id):
        row = _dj_row_full(reg_id)
        if not row:
            return self._html(200, PORTAL_LOGIN_HTML.replace("__RESULT__", '<p class="err">Unknown DJ -- log in again.</p>'))
        _id, name, ln_addr, avatar, pass_hash, blurb, links, user_id, wallet_id, payout, is_host, venue_label, tg_handle = row
        src = ("/assets/avatars/" + avatar) if avatar else "/assets/avatar.webp"
        bal = _wallet_balance_sats(reg_id)
        bal_txt = (f"{bal:,} sats" if bal is not None
                   else "could not read just now -- open the wallet to see it")
        user_id, wallet_id = _heal_wallet_ids(reg_id)
        if user_id and wallet_id:
            wallet_url = f"https://{LN_ADDRESS_DOMAIN}/wallet?usr={user_id}&wal={wallet_id}"
        elif user_id:
            wallet_url = f"https://{LN_ADDRESS_DOMAIN}/wallet?usr={user_id}"
        else:
            wallet_url = "#"
        pass_head = ("Set a password -- from then on you log in with name + password (your recovery code keeps working as backup):"
                     if not pass_hash else "Change your password:")
        link_vals = (links or "").split("\n")
        link_vals += [""] * (3 - len(link_vals))
        payout_state = ("Payouts currently go to <b>your own wallet</b> &#9889;" if payout
                        else "Payouts currently go to your station wallet.")
        html = (PORTAL_HTML.replace("__DJ_NAME_URL__", quote(name))
                .replace("__DJ_NAME__", _esc_attr(name))
                .replace("__LN_ADDRESS__", _esc_attr(ln_addr or ""))
                .replace("__AVATAR_SRC__", src)
                .replace("__BALANCE__", _esc_attr(bal_txt))
                .replace("__WALLET_URL__", _esc_attr(wallet_url))
                .replace("__PASS_HEAD__", pass_head)
                .replace("__BLURB__", _esc_attr(blurb or ""))
                .replace("__LINK1__", _esc_attr(link_vals[0]))
                .replace("__LINK2__", _esc_attr(link_vals[1]))
                .replace("__LINK3__", _esc_attr(link_vals[2]))
                .replace("__TG__", _esc_attr(tg_handle or ""))
                .replace("__PAYOUT_VALUE__", _esc_attr(payout or ""))
                .replace("__PAYOUT_STATE__", payout_state)
                .replace("__HOST_CARD__", _hosting_toggle_html(is_host, venue_label)
                         + (_host_card_html(name, venue_label) if is_host else ""))
                .replace("__LIVE_TAB__", ('<button type="button" data-pane="live">Live</button>' if is_host else ""))
                .replace("__LIVE_CARDS__", (_ondeck_card_html(venue_label) if is_host else "")))
        return self._html(200, html)

    def do_GET(self):
        if not REGISTRATION_OPEN and not self.path.startswith(("/admin", "/portal", "/avatars", "/u/")):
            return self._html(200, CLOSED_HTML)
        if self.path == "/" or self.path.startswith("/?"):
            return self._html(200, FORM_HTML.replace("__RESULT__", ""))

        if self.path == "/request-slot" or self.path.startswith("/request-slot?"):
            # ?dj_name=... carries the name over from a fresh registration so
            # the DJ doesn't have to type it twice
            _, _, query = self.path.partition("?")
            prefill = _esc_attr((parse_qs(query).get("dj_name") or [""])[0][:40])
            html = (SLOT_FORM_HTML.replace("__HOUR_OPTIONS__", _hour_options())
                    .replace("__RESULT__", "").replace("__DJ_NAME__", prefill))
            return self._html(200, html)

        if self.path == "/portal" or self.path.startswith("/portal?"):
            reg_id = _portal_auth(self.headers.get("Cookie"))
            if not reg_id:
                return self._html(200, PORTAL_LOGIN_HTML.replace("__RESULT__", ""))
            return self._serve_portal(reg_id)

        if self.path == "/portal/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", f"{PORTAL_COOKIE}=gone; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if self.path == "/avatars" or self.path.startswith("/avatars?"):
            # public name -> avatar-url map for the overlay and the site
            data = json.dumps(_avatar_map()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path.startswith("/u/"):
            raw = unquote(self.path[3:].partition("?")[0])
            slug = _slugify(raw)
            con = sqlite3.connect(DB_PATH)
            rows = con.execute("SELECT id, dj_name FROM dj_registrations WHERE COALESCE(removed, 0) = 0").fetchall()
            con.close()
            hit = next((r for r in rows if _slugify(r[1]) == slug), None)
            if not hit:
                return self._html(404, '<p style="font-family:monospace;color:#e6eff0;background:#0a1a1f;padding:30px;">No DJ by that name aboard. <a href="/" style="color:#f7931a;">Back to the radio</a></p>')
            viewer = _portal_auth(self.headers.get("Cookie"))
            if viewer == hit[0]:
                return self._serve_portal(hit[0])
            row = _dj_row_full(hit[0])
            _id, name, ln_addr, avatar, _ph, blurb, links, _u, _w, _p, _ih, _venue, tg_handle = row
            src = ("/assets/avatars/" + avatar) if avatar else "/assets/avatar.webp"
            blurb_html = ("<p>" + _esc_attr(blurb) + "</p>") if blurb else ""
            link_html = "".join(
                '<p><a href="' + _esc_attr(u) + '" target="_blank" rel="noopener nofollow">' + _esc_attr(u) + "</a></p>"
                for u in (links or "").split("\n") if u
            )
            tg = re.sub(r"[^A-Za-z0-9_]", "", (tg_handle or ""))
            tg_line = (('<p class="muted">Find ' + _esc_attr(name) + ' on Telegram: '
                        '<a href="https://t.me/' + tg + '" target="_blank" rel="noopener nofollow">@' + tg + '</a></p>')
                       if tg else "")
            html = (PUBLIC_DJ_HTML.replace("__DJ_NAME__", _esc_attr(name))
                    .replace("__AVATAR_SRC__", src)
                    .replace("__BLURB__", blurb_html)
                    .replace("__LINKS__", link_html)
                    .replace("__LN_ADDRESS__", _esc_attr(_p or ln_addr or ""))
                    .replace("__TG_LINE__", tg_line))
            return self._html(200, html)

        if self.path.startswith("/admin/lnbits-audit"):
            path, _, query = self.path.partition("?")
            qs = parse_qs(query)
            if not self._check_admin(qs):
                return
            keep = {}
            con = sqlite3.connect(DB_PATH)
            for name, uid, removed in con.execute("SELECT dj_name, lnbits_user_id, COALESCE(removed, 0) FROM dj_registrations"):
                if uid:
                    keep[uid] = (name, removed)
            con.close()
            rows = []
            try:
                lcon = sqlite3.connect(f"file:{LNBITS_DB_PATH}?mode=ro", uri=True, timeout=3)
                try:
                    raw = lcon.execute('SELECT a.id, COUNT(w.id), GROUP_CONCAT(w.name, " | ") FROM accounts a LEFT JOIN wallets w ON w.user = a.id GROUP BY a.id').fetchall()
                except sqlite3.Error:
                    raw = lcon.execute('SELECT user, COUNT(id), GROUP_CONCAT(name, " | ") FROM wallets GROUP BY user').fetchall()
                lcon.close()
            except sqlite3.Error as e:
                return self._json_resp(500, {"error": f"could not read LNbits db: {e}"})
            order = {"DELETE": 0, "CHECK": 1, "KEEP-DJ": 2, "KEEP-MAIN": 3}
            for uid, wc, wnames in raw:
                if uid in keep:
                    name, removed = keep[uid]
                    verdict = "DELETE" if removed else "KEEP-DJ"
                    label = f"registry: {name}" + (" (removed DJ)" if removed else " (active DJ)")
                elif (wc or 0) > 1:
                    verdict = "KEEP-MAIN"
                    label = "owns multiple wallets — almost certainly the captain's own account"
                else:
                    verdict = "CHECK"
                    label = "unknown to the DJ registry — delete only if you recognise it as junk"
                rows.append({"verdict": verdict, "label": label, "user_id": uid,
                             "wallets": wc or 0, "wallet_names": (wnames or "")[:120]})
            rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["label"]))
            return self._json_resp(200, {"accounts": rows})

        if self.path.startswith("/admin/api"):
            # JSON twin of the /admin HTML page, consumed by the captain's
            # console (admin.html on the main site). Same auth, same data.
            path, _, query = self.path.partition("?")
            qs = parse_qs(query)
            if not self._check_admin(qs):
                return
            rows = _list_slot_requests()
            reqs = [
                {"id": rid, "dj_name": name, "telegram": tg or "", "npub": npub or "",
                 "slot": f"{date} {hour:02d}:00 CET", "status": status}
                for rid, name, tg, npub, date, hour, status, _req_at in rows
            ]
            djs = [
                {"dj_name": n, "lightning_address": a or "", "wallet_id": w or "",
                 "claim_code": c or "", "avatar": ("/assets/avatars/" + av) if av else "",
                 "payout_address": pa or "", "is_host": int(ih or 0)}
                for n, a, w, c, av, pa, ih in _list_registrations()
            ]
            removed = [{"dj_name": n, "removed_at": ra or 0} for n, ra in _list_removed()]
            con = sqlite3.connect(DB_PATH)
            srows = con.execute(
                "SELECT id, host_name, session_date, venue_label, slots, status, COALESCE(note,''), COALESCE(start_time,'20:00') "
                "FROM host_sessions ORDER BY session_date DESC, id DESC LIMIT 60").fetchall()
            slotmap = {}
            for ssid, pos, ln, dj in con.execute(
                    "SELECT session_id, position, length_min, COALESCE(dj_name,'') FROM session_slots ORDER BY session_id, position"):
                slotmap.setdefault(ssid, []).append({"position": pos, "length": ln, "dj": dj})
            con.close()
            sheets = []
            for sid, h, d, v, s, st, n, stt in srows:
                sl = slotmap.get(sid, [])
                times = _slot_times(stt, [x["length"] for x in sl]) if sl else []
                for t, x in zip(times, sl):
                    x["at"] = t
                sheets.append({"id": sid, "host": h, "date": d, "venue": v, "slots": s,
                               "status": st, "note": n, "start": stt, "slotlist": sl})
            con = sqlite3.connect(DB_PATH)
            invites = [{"id": i, "name": nm, "contact": ct, "message": ms,
                        "created_at": ca or 0, "handled": int(hd or 0)}
                       for i, nm, ct, ms, ca, hd in con.execute(
                           "SELECT id, name, COALESCE(contact,''), COALESCE(message,''), created_at, "
                           "COALESCE(handled,0) FROM invite_requests ORDER BY id DESC LIMIT 100")]
            con.close()
            data = json.dumps({"requests": reqs, "djs": djs, "removed": removed,
                               "sheets": sheets, "invites": invites}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

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

    def _json_resp(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_portal_avatar(self):
        reg_id = _portal_auth(self.headers.get("Cookie"))
        if not reg_id:
            return self._json_resp(403, {"error": "not logged in"})
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > AVATAR_MAX_BYTES:
            return self._json_resp(400, {"error": "image must be under %d KB" % (AVATAR_MAX_BYTES // 1000)})
        body = self.rfile.read(length)
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            ext = "webp"
        elif body[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif body[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        else:
            return self._json_resp(400, {"error": "only webp/png/jpeg accepted"})
        row = _dj_row(reg_id)
        if not row:
            return self._json_resp(403, {"error": "unknown DJ"})
        slug = _slugify(row[1])
        fname = slug + "." + ext
        for old_ext in ("webp", "png", "jpg"):
            if old_ext != ext:
                try:
                    os.remove(os.path.join(AVATAR_DIR, slug + "." + old_ext))
                except FileNotFoundError:
                    pass
        with open(os.path.join(AVATAR_DIR, fname), "wb") as f:
            f.write(body)
        _set_avatar(reg_id, fname)
        self._json_resp(200, {"url": "/assets/avatars/" + fname})

    def do_POST(self):
        # binary route first -- the avatar body is raw image bytes, not a form
        if self.path == "/portal/avatar":
            return self._handle_portal_avatar()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)

        if self.path in ("/portal/login", "/portal"):
            name = (qs.get("dj_name") or [""])[0]
            code = (qs.get("code") or [""])[0]
            pw = (qs.get("pass") or [""])[0]
            if pw:
                reg_id = _find_dj_by_pass(name, pw)
            elif code:
                reg_id = _find_dj_by_login(name, code)
            else:
                reg_id = None
            if not reg_id:
                time.sleep(0.6)  # cheap brute-force damper
                return self._html(403, PORTAL_LOGIN_HTML.replace("__RESULT__", '<p class="err">No match -- check the name and password/code.</p>'))
            row = _dj_row(reg_id)
            dest = ("/" + quote(row[1])) if row else "/login"
            self.send_response(303)
            self.send_header("Set-Cookie", f"{PORTAL_COOKIE}={_mint_portal_cookie(reg_id)}; Path=/; Max-Age={PORTAL_TTL_S}; HttpOnly; Secure; SameSite=Strict")
            self.send_header("Location", dest)
            self.end_headers()
            return

        if self.path == "/portal/setpass":
            reg_id = _portal_auth(self.headers.get("Cookie"))
            if not reg_id:
                return self._html(403, "not logged in")
            pw = (qs.get("pass") or [""])[0]
            pw2 = (qs.get("pass2") or [""])[0]
            if len(pw) < 12 or pw != pw2:
                return self._html(400, '<p style="font-family:monospace;color:#ff6b6b;">Passwords must match and be at least 12 characters. <a href="/login">Back to your portal</a></p>')
            _set_pass(reg_id, pw)
            self.send_response(303)
            self.send_header("Location", _portal_dest(reg_id))
            self.end_headers()
            return

        if self.path == "/portal/profile":
            reg_id = _portal_auth(self.headers.get("Cookie"))
            if not reg_id:
                return self._html(403, "not logged in")
            blurb = (qs.get("blurb") or [""])[0].strip()[:280]
            links = []
            for k in ("link1", "link2", "link3"):
                u = (qs.get(k) or [""])[0].strip()[:200]
                if u and not u.startswith(("http://", "https://")):
                    u = "https://" + u
                if u.startswith(("http://", "https://")) and "." in u[8:]:
                    links.append(u)
            tg = (qs.get("telegram") or [""])[0].strip()
            tg = tg.split("t.me/")[-1].lstrip("@")
            tg = re.sub(r"[^A-Za-z0-9_]", "", tg)[:32]
            _set_profile(reg_id, blurb, "\n".join(links), tg)
            self.send_response(303)
            self.send_header("Location", _portal_dest(reg_id))
            self.end_headers()
            return

        if self.path == "/portal/payout":
            reg_id = _portal_auth(self.headers.get("Cookie"))
            if not reg_id:
                return self._html(403, "not logged in")
            addr = (qs.get("payout") or [""])[0].strip().lower()[:100]
            if addr and not re.match(r"^[a-z0-9._+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", addr):
                return self._html(400, '<p style="font-family:monospace;color:#ff6b6b;">That does not look like a Lightning Address (name@domain). <a href="/login">Back to your portal</a></p>')
            _set_payout(reg_id, addr)
            self.send_response(303)
            self.send_header("Location", _portal_dest(reg_id))
            self.end_headers()
            return

        if self.path == "/portal/hostmode":
            # self-service hosting (SF's ruling 2026-07-30): the DJ flips it on
            # their own page. The captain still approves every sheet.
            reg_id = _portal_auth(self.headers.get("Cookie"))
            if not reg_id:
                return self._html(403, "not logged in")
            on = (qs.get("on") or ["0"])[0] == "1"
            venue = (qs.get("venue") or [""])[0].strip()[:60]
            con = sqlite3.connect(DB_PATH)
            if on and venue:
                con.execute("UPDATE dj_registrations SET is_host = 1, venue_label = ? WHERE id = ?", (venue, reg_id))
            elif on:
                con.execute("UPDATE dj_registrations SET is_host = 1 WHERE id = ?", (reg_id,))
            else:
                # switching off keeps the stored venue -- flipping back on is cheap
                con.execute("UPDATE dj_registrations SET is_host = 0 WHERE id = ?", (reg_id,))
            con.commit()
            con.close()
            self.send_response(303)
            self.send_header("Location", _portal_dest(reg_id))
            self.end_headers()
            return

        if self.path == "/portal/ondeck":
            # HOST DOOR (SF 2026-08-13): the host marks who is on deck without
            # the captain and without ever seeing an admin token. We verify the
            # host here, then relay over localhost with a short-lived console
            # session minted from the shared secret. Cosmetic only -- it drives
            # the overlay highlight; settlement math is untouched.
            reg_id = _portal_auth(self.headers.get("Cookie"))
            host = _host_of(reg_id) if reg_id else None
            if not host:
                return self._json_resp(403, {"error": "host login required -- flip HOST MODE on in your portal first"})
            dj = (qs.get("dj") or [""])[0].strip()[:80]
            try:
                r = requests.post(LIBRARY_API_URL + "/api/admin/livesplit/nowplaying",
                                  data={"dj": dj},
                                  headers={"Cookie": "nr_admin_session=" + _mint_console_session()},
                                  timeout=10)
                logging.info("host door: %s set on-deck %r -> %s", host[0], dj, r.status_code)
                return self._json_resp(r.status_code, r.json())
            except Exception as exc:
                logging.warning("ondeck relay failed: %s", exc)
                return self._json_resp(502, {"error": "station API unreachable -- tell the captain"})

        if self.path == "/portal/overlaysave":
            # HOST DOOR for the overlay drag editor. 2026-08-25 ruling (SF):
            # personalised -- each host saves ONLY their own venue's overlay. The
            # slug comes from their REGISTERED label, never from the form (the same
            # rule the sheet endpoint always had). An empty label refuses instead
            # of falling to "default", which is the captain's own overlay.
            reg_id = _portal_auth(self.headers.get("Cookie"))
            host = _host_of(reg_id) if reg_id else None
            if not host:
                return self._json_resp(403, {"error": "host login required -- flip HOST MODE on in your portal first"})
            if not (host[1] or "").strip():
                return self._json_resp(400, {"error": "Set your venue label on the Hosting card first -- your overlay carries it."})
            venue = _venue_slug(host[1])   # the SAME slug the portal's overlay URL shows
            layout = (qs.get("layout") or ["{}"])[0]
            if len(layout) >= 4000:
                return self._json_resp(400, {"error": "layout too big"})
            try:
                r = requests.post(LIBRARY_API_URL + "/api/admin/overlay/layout",
                                  data={"venue": venue, "layout": layout},
                                  headers={"Cookie": "nr_admin_session=" + _mint_console_session()},
                                  timeout=10)
                logging.info("host door: %s saved overlay %r -> %s", host[0], venue, r.status_code)
                return self._json_resp(r.status_code, r.json())
            except Exception as exc:
                logging.warning("overlaysave relay failed: %s", exc)
                return self._json_resp(502, {"error": "station API unreachable -- tell the captain"})

        if self.path == "/portal/hostsheet":
            reg_id = _portal_auth(self.headers.get("Cookie"))
            if not reg_id:
                return self._html(403, "not logged in")
            con = sqlite3.connect(DB_PATH)
            hrow = con.execute("SELECT dj_name, COALESCE(is_host, 0), COALESCE(venue_label, '') FROM dj_registrations WHERE id = ?", (reg_id,)).fetchone()
            con.close()
            if not hrow or not hrow[1]:
                return self._html(403, '<p style="font-family:monospace;">Host mode is not switched on for your account -- flip it on your portal. <a href="/login">Back to your portal</a></p>')
            date = (qs.get("date") or [""])[0].strip()
            # the venue comes from the host's OWN registered label, never from
            # the form -- a host plans sessions at their own place (SF's ruling)
            venue = hrow[2].strip()[:60]
            start = (qs.get("start") or [""])[0].strip()
            lengths = []
            for raw in (qs.get("slotlen") or []):
                try:
                    lengths.append(max(10, min(240, int(raw))))
                except ValueError:
                    pass
            lengths = lengths[:12] or [60, 60, 60]
            slot_djs = [(d or "").strip() for d in (qs.get("slotdj") or [])][:12]
            if not venue:
                return self._html(400, '<p style="font-family:monospace;color:#ff6b6b;">Set your venue label on the Hosting card first -- your sheets carry it. <a href="/login">Back to your portal</a></p>')
            if date < time.strftime("%Y-%m-%d"):
                return self._html(400, '<p style="font-family:monospace;color:#ff6b6b;">That date is in the past -- pick today or later. <a href="/login">Back to your portal</a></p>')
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date) or not re.match(r"^\d{1,2}:\d{2}$", start):
                return self._html(400, '<p style="font-family:monospace;color:#ff6b6b;">The date or the start time did not come through whole -- use the pickers and send again. Nothing was lost on our side. <a href="/login">Back to your portal</a></p>')
            _add_host_sheet(hrow[0], date, venue, start, lengths, slot_djs)
            try:
                _notify_new_host_sheet(hrow[0], date, venue, len(lengths), start)
            except Exception:
                pass          # a silent chat is never a reason to lose the sheet
            # 2026-08-25: the silent 303 back to the portal looked like "the page
            # reset" and the host resent the sheet. Say plainly that it worked.
            return self._html(200, (
                '<div style="font-family:ui-monospace,monospace;max-width:520px;margin:70px auto;'
                'padding:0 20px;color:#e6eff0;">'
                '<div style="background:#0f262d;border:2px solid #f7931a;border-radius:8px;padding:22px 24px;">'
                '<p style="font-size:1.1rem;"><b>&#10004; Sheet sent &mdash; the captain has it.</b></p>'
                '<p>' + _esc_attr(date) + ' at ' + _esc_attr(venue) + ' &middot; ' + str(len(lengths))
                + ' slot(s), from ' + _esc_attr(start) + '.</p>'
                '<p>It appears on the public calendar after the captain approves it. Its status shows '
                'under <b>Your session sheets</b> on your page.</p>'
                '<p><a href="' + _portal_dest(reg_id) + '" style="color:#f7931a;">&larr; Back to your page</a></p>'
                '</div></div>'))

        if self.path == "/invite-request":
            # the beg box on the closed page (SF 2026-08-02): a stranger may
            # leave word. No account is created, nothing is published. This is
            # free text from the open internet, so: capped lengths, a honeypot
            # field, a station-wide hourly brake, and escaping on display.
            name = (qs.get("name") or [""])[0].strip()[:40]
            contact = (qs.get("contact") or [""])[0].strip()[:80]
            message = (qs.get("message") or [""])[0].strip()[:500]
            if (qs.get("website") or [""])[0].strip():
                return self._html(200, INVITE_THANKS_HTML)   # a bot filled the hidden field
            if not name:
                return self._html(400, CLOSED_HTML)
            con = sqlite3.connect(DB_PATH)
            recent = con.execute(
                "SELECT COUNT(*) FROM invite_requests WHERE created_at > ?",
                (int(time.time()) - 3600,)).fetchone()[0]
            if recent < 30:
                con.execute(
                    "INSERT INTO invite_requests (name, contact, message, created_at) VALUES (?, ?, ?, ?)",
                    (name, contact, message, int(time.time())))
                con.commit()
            con.close()
            try:
                _telegram_send(TELEGRAM_ADMIN_CHAT_ID,
                               "Invite request from " + name + " (" + (contact or "no contact given") + "): " + message[:200])
            except Exception:
                pass
            return self._html(200, INVITE_THANKS_HTML)

        if not REGISTRATION_OPEN and not self.path.startswith(("/admin", "/portal", "/avatars", "/u/")):
            return self._html(200, CLOSED_HTML)

        if self.path in ("/register", "/"):
            name = (qs.get("name") or [""])[0].strip()
            pw = (qs.get("pass") or [""])[0]
            pw2 = (qs.get("pass2") or [""])[0]
            if not name:
                return self._html(400, FORM_HTML.replace("__RESULT__", '<p class="err">Enter a name.</p>'))
            if _name_taken(name):
                return self._html(400, FORM_HTML.replace("__RESULT__", '<p class="err">That DJ name is already taken -- one DJ, one name, set in stone.</p>'))
            if len(pw) < 12 or pw != pw2:
                return self._html(400, FORM_HTML.replace("__RESULT__", '<p class="err">Passwords must match and be at least 12 characters -- try 4 random words.</p>'))
            if name.lower() in pw.lower():
                return self._html(400, FORM_HTML.replace("__RESULT__", '<p class="err">Do not put your DJ name inside your password.</p>'))
            try:
                result = create_dj_wallet(name, pass_hash=_hash_pass(pw))
                try:
                    _telegram_send(TELEGRAM_ADMIN_CHAT_ID,
                                   f"New DJ registered themselves: {result['dj_name']} (the gate is OPEN)")
                except Exception:
                    pass
                result_html = f"""
                <div class="result">
                  <p>Welcome aboard, {result['dj_name']}!</p>
                  <p>Your Lightning Address:<br><b>{result['lightning_address']}</b></p>
                  <p>Your RECOVERY CODE (write it down somewhere safe -- it's your way back in if you ever lose your password):<br><b>{result['claim_code']}</b></p>
                  <p><a href="/login">Log in to your DJ portal</a> -- your sats, your avatar, your profile.</p>
                  <p><a href="/dj/request-slot?dj_name={quote(result['dj_name'])}">Request a DJ slot &rarr;</a></p>
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
            html = SLOT_FORM_HTML.replace("__HOUR_OPTIONS__", _hour_options(hour)).replace("__DJ_NAME__", _esc_attr(name))
            if not name or not date or not telegram_handle:
                return self._html(400, html.replace("__RESULT__", '<p class="err">Fill in your DJ name, Telegram handle, and a date.</p>'))
            _record_slot_request(name, telegram_handle, nostr_npub, date, hour)
            _notify_new_slot_request(name, date, hour)
            result_html = f'<div class="result"><p>Request submitted for {date} {hour:02d}:00 (studio time). We\'ll reach out on Telegram once it\'s reviewed.</p></div>'
            return self._html(200, html.replace("__RESULT__", result_html))

        if self.path == "/admin/remove":
            if not self._check_admin(qs):
                return
            name = (qs.get("name") or [""])[0].strip()
            if name:
                con = sqlite3.connect(DB_PATH)
                con.execute("UPDATE dj_registrations SET removed = 1, removed_at = ? WHERE lower(dj_name) = lower(?)", (int(time.time()), name))
                con.commit()
                con.close()
            return self._json_resp(200, {"removed": name})

        if self.path == "/admin/reinstate":
            if not self._check_admin(qs):
                return
            name = (qs.get("name") or [""])[0].strip()
            if name:
                con = sqlite3.connect(DB_PATH)
                con.execute("UPDATE dj_registrations SET removed = 0, removed_at = NULL WHERE lower(dj_name) = lower(?)", (name,))
                con.commit()
                con.close()
            return self._json_resp(200, {"reinstated": name})

        if self.path == "/admin/regencode":
            # Recovery codes are the way back in when a passphrase is lost. If one is
            # ever seen by anyone but its owner (a screenshot, a shared screen), it is
            # burnt -- this replaces it. Passphrases are untouched, so a DJ who still
            # knows theirs never notices.
            if not self._check_admin(qs):
                return
            name = (qs.get("name") or [""])[0].strip()
            everyone = (qs.get("all") or [""])[0] == "1"
            con = sqlite3.connect(DB_PATH)
            if everyone:
                rows = con.execute("SELECT id FROM dj_registrations WHERE COALESCE(removed, 0) = 0").fetchall()
            elif name:
                rows = con.execute("SELECT id FROM dj_registrations WHERE lower(dj_name) = lower(?)", (name,)).fetchall()
            else:
                con.close()
                return self._json_resp(400, {"error": "need a name, or all=1"})
            for (rid,) in rows:
                con.execute("UPDATE dj_registrations SET claim_code = ? WHERE id = ?", (_new_code(), rid))
            con.commit()
            con.close()
            # the new codes are never returned here -- SF reads them in the crew list
            return self._json_resp(200, {"regenerated": len(rows)})

        if self.path == "/admin/hostgrant":
            if not self._check_admin(qs):
                return
            name = (qs.get("name") or [""])[0].strip()
            grant = 1 if (qs.get("grant") or ["1"])[0] == "1" else 0
            if name:
                con = sqlite3.connect(DB_PATH)
                con.execute("UPDATE dj_registrations SET is_host = ? WHERE lower(dj_name) = lower(?)", (grant, name))
                con.commit()
                con.close()
            return self._json_resp(200, {"dj_name": name, "is_host": grant})

        if self.path == "/admin/sheetdecide":
            if not self._check_admin(qs):
                return
            try:
                sid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                sid = 0
            action = (qs.get("action") or [""])[0]
            note = (qs.get("note") or [""])[0].strip()[:200]
            if action not in ("approve", "reject", "remove") or not sid:
                return self._json_resp(400, {"error": "need id and action approve/reject/remove"})
            status = {"approve": "published", "reject": "rejected", "remove": "removed"}[action]
            was = "published" if action == "remove" else "pending"
            con = sqlite3.connect(DB_PATH)
            cur = con.execute(
                "UPDATE host_sessions SET status = ?, note = ?, decided_at = ? WHERE id = ? AND status = ?",
                (status, note or None, int(time.time()), sid, was))
            con.commit()
            changed = cur.rowcount
            row = con.execute("SELECT host_name, session_date, venue_label, COALESCE(start_time,'') FROM host_sessions WHERE id = ?", (sid,)).fetchone()
            con.close()
            if not changed:
                return self._json_resp(409, {"error": "sheet already decided or unknown id"})
            # decision pings (SF 2026-07-30): approve + remove ring the GROUP,
            # a reject stays between captain and host (admin channel + note)
            try:
                hn, dt, vn, st_t = row
                if action == "approve":
                    _telegram_send(TELEGRAM_CHAT_ID, f"SESSION CONFIRMED: {dt} from {st_t} at {vn}, hosted by {_tg_tag(hn)}{hn} -- the calendar is live. @noderunnersfm")
                elif action == "remove":
                    _telegram_send(TELEGRAM_CHAT_ID, f"SESSION CANCELLED: {dt} at {vn} (hosted by {_tg_tag(hn)}{hn}) is off the calendar." + (f" Reason: {note}" if note else ""))
                _telegram_send(TELEGRAM_ADMIN_CHAT_ID, f"Sheet {sid} {status}: {hn}, {dt}, {vn}." + (f" Note: {note}" if note else ""))
            except Exception:
                pass
            return self._json_resp(200, {"id": sid, "status": status})

        if self.path == "/admin/invite-handled":
            if not self._check_admin(qs):
                return
            try:
                iid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                iid = 0
            act = (qs.get("action") or ["handled"])[0]
            if iid:
                con = sqlite3.connect(DB_PATH)
                if act == "delete":
                    con.execute("DELETE FROM invite_requests WHERE id = ?", (iid,))
                else:
                    con.execute("UPDATE invite_requests SET handled = 1 WHERE id = ?", (iid,))
                con.commit()
                con.close()
            return self._json_resp(200, {"id": iid, "action": act})

        if self.path == "/admin/request-delete":
            if not self._check_admin(qs):
                return
            try:
                rid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                rid = 0
            if rid:
                try:
                    bak = DB_PATH + time.strftime(".bak-purge-%Y%m%d")
                    shutil.copy2(DB_PATH, bak)
                    os.chmod(bak, 0o600)   # DB holds codes + wallet keys; never world-readable
                except OSError:
                    pass
                con = sqlite3.connect(DB_PATH)
                con.execute("DELETE FROM slot_requests WHERE id = ?", (rid,))
                con.commit()
                con.close()
            return self._json_resp(200, {"deleted": rid})

        if self.path == "/admin/purge":
            if not self._check_admin(qs):
                return
            name = (qs.get("name") or [""])[0].strip()
            if name:
                # only already-removed rows can be purged; dated DB backup first
                try:
                    bak = DB_PATH + time.strftime(".bak-purge-%Y%m%d")
                    shutil.copy2(DB_PATH, bak)
                    os.chmod(bak, 0o600)   # DB holds codes + wallet keys; never world-readable
                except OSError:
                    pass
                con = sqlite3.connect(DB_PATH)
                con.execute("DELETE FROM dj_registrations WHERE lower(dj_name) = lower(?) AND COALESCE(removed, 0) = 1", (name,))
                con.commit()
                con.close()
            return self._json_resp(200, {"purged": name})

        if self.path == "/admin/approve" or self.path == "/admin/reject":
            if not self._check_admin(qs):
                return
            try:
                rid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                rid = 0
            status = "approved" if self.path == "/admin/approve" else "rejected"
            note = (qs.get("note") or [""])[0].strip()[:280]
            if rid:
                _set_slot_status(rid, status)
                # public ping in the radio group: decision + captain's note, never secrets
                try:
                    con = sqlite3.connect(DB_PATH)
                    row = con.execute("SELECT dj_name, telegram_handle, slot_date, hour_cet FROM slot_requests WHERE id = ?", (rid,)).fetchone()
                    con.close()
                    if row and TELEGRAM_CHAT_ID:
                        name, tg, date, hour = row
                        mark = "\u2705" if status == "approved" else "\u274c"
                        msg = f"{mark} Slot request {status}: {name} \u2014 {date} {hour:02d}:00 CET"
                        if tg:
                            msg += f" ({tg})"
                        if note:
                            msg += f"\nCaptain's note: {note}"
                        _telegram_send(TELEGRAM_CHAT_ID, msg)
                except Exception:
                    pass
            self.send_response(303)
            # no token in the redirect -- the session cookie carries the auth
            self.send_header("Location", "/dj/admin")
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
