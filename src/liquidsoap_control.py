#!/usr/bin/env python3
"""
liquidsoap_control.py -- push a track onto Liquidsoap's live request queue.

Talks to the request.queue(id="jukebox") source in noderunners.liq over its
unix socket (settings.server.socket.path in the .liq config). This is the
missing link between "found in the library" and "actually plays on air."

    from liquidsoap_control import push_track
    push_track("/home/sf/music-test/some_song.mp3")

Not wired into library_api.py yet on purpose -- test this by hand against the
live socket with SF present before any endpoint can trigger it, since a typo
here talks directly to the on-air source.
"""

import re
import socket

DEFAULT_SOCKET = "/var/run/liquidsoap/noderunners.sock"


def _send(sock_path: str, command: str, timeout: float = 5.0) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(command.strip().encode() + b"\n")
        s.sendall(b"quit\n")
        chunks = []
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks).decode(errors="replace")


def _annotate(meta: dict) -> str:
    """Build a Liquidsoap annotate: prefix from metadata, or '' if none.

    Values are double-quoted with quotes/backslashes escaped so a title with a
    quote in it can't break out of the annotation. Keys are simple identifiers.
    """
    parts = []
    for k, v in meta.items():
        if v is None or v == "":
            continue
        # newlines are COMMAND SEPARATORS on the liquidsoap control socket:
        # a title containing one could smuggle a second command through.
        # Nothing user-facing reaches here today (pushes use DB values), but
        # this is the boundary, so it defends itself. (2026-07-29 audit)
        safe = str(v).replace("\\", "\\\\").replace('"', '\\"')
        safe = "".join(ch for ch in safe if ord(ch) >= 32 and ord(ch) != 127)[:200]
        parts.append(f'{k}="{safe}"')
    return f"annotate:{','.join(parts)}:" if parts else ""


def push_track(local_path: str, rights_class: str = "", source: str = "",
               artist: str = "", title: str = "",
               socket_path: str = DEFAULT_SOCKET) -> str:
    """Queue a track for near-immediate playback. Returns the request ID Liquidsoap assigns.

    rights_class/source ride along as annotation metadata so the play-log hook
    can attribute the play to the right royalty pool. Omit them and behaviour is
    identical to before (a bare push) -- fully backward compatible.
    """
    # artist/title matter for URL plays (Wavlake): a remote URI carries no tags,
    # so without these the overlay, queue and history all show "? - ?"
    # (first V4V test, 2026-07-29).
    uri = f"{_annotate({'rights_class': rights_class, 'source': source, 'artist': artist, 'title': title})}{local_path}"
    return _send(socket_path, f"jukebox.push {uri}")


def queue_status(socket_path: str = DEFAULT_SOCKET) -> str:
    """What's currently queued/playing in the jukebox request source."""
    return _send(socket_path, "jukebox.queue")


def _parse_metadata(raw: str) -> dict:
    """Parse a request.metadata block (key="value" lines, escaped quotes) into a dict."""
    out = {}
    for line in raw.splitlines():
        m = re.match(r'^(\w+)="((?:[^"\\]|\\.)*)"$', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def request_metadata(rid: str, socket_path: str = DEFAULT_SOCKET) -> dict:
    """Track info (artist/title/album/status/...) for one request ID."""
    return _parse_metadata(_send(socket_path, f"request.metadata {rid}"))


def on_air_rid(socket_path: str = DEFAULT_SOCKET) -> str | None:
    """The RID currently playing on air, if any."""
    raw = _send(socket_path, "request.on_air").strip().splitlines()
    rid = raw[0].strip() if raw and raw[0].strip() and raw[0].strip() != "END" else None
    return rid or None


def queue_rids(socket_path: str = DEFAULT_SOCKET) -> list[str]:
    """RIDs currently sitting in the jukebox queue, in order."""
    raw = _send(socket_path, "jukebox.queue").strip().splitlines()
    line = raw[0].strip() if raw else ""
    return line.split() if line and line != "END" else []


def skip(socket_path: str = DEFAULT_SOCKET) -> str:
    """Skip whatever is currently on air.

    Noderunners_Radio.skip (the icecast output wrapper) is a no-op in practice --
    confirmed by testing, on_air stayed the same RID before/after. The actual
    active leaf source (jukebox or music-test, per request.metadata's "source"
    field) is what needs the .skip call.
    """
    rid = on_air_rid(socket_path)
    if not rid:
        return "nothing on air"
    source = request_metadata(rid, socket_path).get("source", "music-test")
    return _send(socket_path, f"{source}.skip")


def flush_and_skip(socket_path: str = DEFAULT_SOCKET) -> str:
    """Clear the entire jukebox request backlog and skip to the next source."""
    return _send(socket_path, "jukebox.flush_and_skip")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: liquidsoap_control.py <path-to-audio-file>")
    print(push_track(sys.argv[1]))
