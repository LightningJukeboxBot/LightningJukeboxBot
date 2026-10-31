#!/usr/bin/env bash
# Captain's direct push: put one exact file on the jukebox queue.
# Usage: bash nr-push.sh /full/path/to/track
[ -z "$1" ] && { echo "usage: bash nr-push.sh /path/to/track"; exit 1; }
printf 'jukebox.push %s\nquit\n' "$1" | python3 -c '
import socket,sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.connect("/var/run/liquidsoap/noderunners.sock")
s.sendall(sys.stdin.buffer.read())
print(s.recv(4096).decode(errors="replace"))'
