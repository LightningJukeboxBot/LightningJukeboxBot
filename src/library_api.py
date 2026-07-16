#!/usr/bin/env python3
"""
library_api.py -- minimal HTTP API backing the site's search box.

    GET  /api/search?q=...        -> JSON list of playable local tracks
    POST /api/request             -> body: q=... ; records a miss in the wanted list
    POST /api/play                -> body: artist=...&title=... ; queues it live on air

    GET  /api/admin/status?token=... -> now-playing + queue, needs JUKEBOX_ADMIN_TOKEN
    POST /api/admin/skip           -> body: token=... ; skips the current on-air track
    POST /api/admin/flush          -> body: token=... ; clears the whole jukebox queue

Talks only to the local SQLite library index, the shared Redis wanted-list, and
Liquidsoap's request socket. No Telegram, no LNbits, no bot dependency -- safe
to run standalone, ahead of the real bot deployment.
"""

import asyncio
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import redis
from resolvers.local import LocalResolver
from resolvers.musicbrainz import MusicBrainzResolver
from resolvers.wanted import WantedQueue
from resolvers.chain import ResolverChain
from liquidsoap_control import push_track, on_air_rid, queue_rids, request_metadata, skip, flush_and_skip
from telegram_chat_relay import TelegramChatRelay

DB_PATH = "/var/lib/jukebox/library.db"
PORT = 7100
PLAY_COOLDOWN_S = 10  # basic anti-spam: don't let requests hammer the live queue
ADMIN_TOKEN = os.environ.get("JUKEBOX_ADMIN_TOKEN")

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
        return self._json(404, {"error": "not found"})

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

    def _admin_authed(self, token):
        return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN

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
        if parsed.path == "/api/admin/skip":
            return self._handle_admin_action(skip, "Skipped current track")
        if parsed.path == "/api/admin/flush":
            return self._handle_admin_action(flush_and_skip, "Flushed queue and skipped")
        return self._json(404, {"error": "not found"})

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
        global _last_play_at

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        artist = (qs.get("artist") or [""])[0].strip()
        title = (qs.get("title") or [""])[0].strip()
        if not artist or not title:
            return self._json(400, {"error": "missing artist/title"})

        now = time.time()
        if now - _last_play_at < PLAY_COOLDOWN_S:
            wait = round(PLAY_COOLDOWN_S - (now - _last_play_at))
            return self._json(429, {"error": f"Too many requests, try again in {wait}s"})

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
            push_track(track.local_path)
            _last_play_at = now
        except Exception as e:
            return self._json(500, {"error": f"Could not queue track: {e}"})

        self._json(200, {"message": f"Queued '{track.display()}' -- should play shortly"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"library_api listening on 127.0.0.1:{PORT}")
    server.serve_forever()
