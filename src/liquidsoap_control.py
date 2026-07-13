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


def push_track(local_path: str, socket_path: str = DEFAULT_SOCKET) -> str:
    """Queue a track for near-immediate playback. Returns the request ID Liquidsoap assigns."""
    return _send(socket_path, f"jukebox.push {local_path}")


def queue_status(socket_path: str = DEFAULT_SOCKET) -> str:
    """What's currently queued/playing in the jukebox request source."""
    return _send(socket_path, "jukebox.queue")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: liquidsoap_control.py <path-to-audio-file>")
    print(push_track(sys.argv[1]))
