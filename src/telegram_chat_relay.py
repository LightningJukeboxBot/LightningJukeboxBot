"""
telegram_chat_relay.py -- mirrors the public Telegram group chat onto the site.

Deliberately separate from telegramhelper.py / jukeboxbot.py -- that's the
python-telegram-bot async request/payment bot (not currently deployed). This
is a small, independent long-poller using plain HTTP, sitting alongside
library_api.py's own synchronous ThreadingHTTPServer.

Bot token lives in the TELEGRAM_CHAT_BOT_TOKEN env var (same pattern as
JUKEBOX_ADMIN_TOKEN) -- never hardcoded, never sent to the browser. Media
(GIFs/photos/stickers) is proxied through our own /api/chat/media endpoint
so the token never appears in a client-facing URL.

History is Redis-backed (same instance the wanted-list already uses) so a
service restart doesn't wipe the chat -- an in-memory-only deque was the v1
bug: every `systemctl restart library-api` silently reset visible history
to empty. Falls back to an in-memory deque only when no Redis client is
given (standalone/test mode), same pattern as resolvers/wanted.py.
"""

import json
import logging
import threading
import time
from collections import deque
from typing import Optional

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_TIMEOUT_S = 25
MAX_HISTORY = 100
REDIS_KEY = "telegram_chat:history"


def _display_name(frm: dict) -> str:
    if frm.get("username"):
        return frm["username"]
    name = frm.get("first_name", "")
    if frm.get("last_name"):
        name += f" {frm['last_name']}"
    return name or "Anonymous"


def _extract_media(msg: dict) -> Optional[dict]:
    """Telegram's 'GIFs' are actually mp4 animations. Stickers are webp
    (static) or tgs (animated, Lottie -- not rendered here, v1 limitation)."""
    if "animation" in msg:
        return {"kind": "animation", "file_id": msg["animation"]["file_id"]}
    if "photo" in msg and msg["photo"]:
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        return {"kind": "photo", "file_id": largest["file_id"]}
    if "sticker" in msg:
        sticker = msg["sticker"]
        if sticker.get("is_animated") or sticker.get("is_video"):
            return {"kind": "sticker_unsupported", "file_id": None}
        return {"kind": "sticker", "file_id": sticker["file_id"]}
    return None


class TelegramChatRelay:
    def __init__(self, token: str, chat_id: Optional[int] = None, rds=None):
        self.token = token
        self.chat_id = chat_id  # None = accept from whatever chat the bot is in
        self.rds = rds
        self._history = deque(maxlen=MAX_HISTORY)  # fallback only, when rds is None
        self._lock = threading.Lock()
        self._offset = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _api(self, method: str, **params) -> dict:
        url = API_BASE.format(token=self.token, method=method)
        resp = requests.get(url, params=params, timeout=LONG_POLL_TIMEOUT_S + 10)
        resp.raise_for_status()
        return resp.json()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                data = self._api("getUpdates", offset=self._offset, timeout=LONG_POLL_TIMEOUT_S)
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message") or update.get("edited_message")
                    if not msg or "from" not in msg:
                        continue
                    if self.chat_id is not None and msg["chat"]["id"] != self.chat_id:
                        continue
                    if msg["from"].get("is_bot"):
                        continue  # don't mirror our own bot's messages back

                    entry = {
                        "id": msg["message_id"],
                        "username": _display_name(msg["from"]),
                        "text": msg.get("text") or msg.get("caption") or "",
                        "date": msg["date"],
                        "media": _extract_media(msg),
                    }
                    self._store(entry)
            except Exception as e:
                logging.warning("telegram_chat_relay poll failed: %s", e)
                time.sleep(5)  # back off before retrying; never let a network hiccup kill the thread

    def _store(self, entry: dict):
        if self.rds is not None:
            self.rds.rpush(REDIS_KEY, json.dumps(entry))
            self.rds.ltrim(REDIS_KEY, -MAX_HISTORY, -1)
            return
        with self._lock:
            self._history.append(entry)

    def recent(self, n: int = 50) -> list:
        if self.rds is not None:
            raw = self.rds.lrange(REDIS_KEY, -n, -1)
            return [json.loads(r) for r in raw]
        with self._lock:
            return list(self._history)[-n:]

    def file_path(self, file_id: str) -> Optional[str]:
        """Resolve a file_id to Telegram's file_path, for the media proxy."""
        try:
            data = self._api("getFile", file_id=file_id)
            return data.get("result", {}).get("file_path")
        except Exception as e:
            logging.warning("telegram_chat_relay getFile failed: %s", e)
            return None

    def file_bytes(self, file_path: str):
        """Streams the actual file bytes from Telegram -- token stays server-side."""
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        return requests.get(url, timeout=15, stream=True)
