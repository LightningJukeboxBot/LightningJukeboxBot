#!/usr/bin/env python3
"""
library_api.py -- minimal HTTP API backing the site's search box.

    GET  /api/search?q=...   -> JSON list of playable local tracks
    POST /api/request        -> body: q=... ; records a miss in the wanted list

Talks only to the local SQLite library index and the shared Redis wanted-list.
No Telegram, no LNbits, no bot dependency -- safe to run standalone, ahead of
the real bot deployment.
"""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import redis
from resolvers.local import LocalResolver
from resolvers.wanted import WantedQueue
from resolvers.chain import ResolverChain

DB_PATH = "/var/lib/jukebox/library.db"
PORT = 7100

rds = redis.Redis(db=2)
wanted = WantedQueue(rds=rds)
chain = ResolverChain(resolvers=[LocalResolver(DB_PATH)], wanted=wanted, base_price=21)


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
        if parsed.path != "/api/search":
            return self._json(404, {"error": "not found"})
        qs = parse_qs(parsed.query)
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(200, {"results": []})

        local = chain.by_name["local"]
        tracks = asyncio.run(local.search(q, limit=10))
        results = [{"artist": t.artist, "title": t.title, "album": t.album} for t in tracks]
        self._json(200, {"results": results})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/request":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        qs = parse_qs(raw)
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(400, {"error": "missing q"})

        votes = asyncio.run(wanted.add(q))
        self._json(200, {"votes": votes, "message": "Added to the wanted list"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"library_api listening on 127.0.0.1:{PORT}")
    server.serve_forever()
