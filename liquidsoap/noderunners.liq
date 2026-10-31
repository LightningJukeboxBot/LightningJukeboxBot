#!/usr/bin/liquidsoap

# ---- Instellingen ----
settings.log.file.path := "/var/log/liquidsoap/noderunners.log"
settings.log.level := 3

settings.server.socket := true
settings.server.socket.path := "/var/run/liquidsoap/noderunners.sock"
settings.server.socket.permissions := 432

# ---- Bronnen ----
radio = playlist(
  # id gives the socket a handle: nr-shuffle calls "rotation.reload" after each
  # deal. reload_mode="watch" alone went STALE after mv replaced the inode --
  # found 2026-08-20: the air looped one 800-track window every ~53 h while the
  # deals only changed the file (618 repeat gaps, median 53.1 h, in the playlog).
  id = "rotation",
  mode = "normal",
  # nr_mood (2026-09-05): NO automatic reload. The deal (nr-shuffle) pokes
  # "rotation.reload" over the socket after every window it installs, and the
  # deck-resync guard re-deals after a restart. The "watch" watcher fired a
  # SECOND reload at the deal instant (line-1 doubles, e.g. Nero 2026-09-04),
  # and "reload=3600" was inert under "watch" anyway.
  reload_mode = "never",
  "/var/lib/jukebox/rotation.m3u"
)

# nr_mood (2026-09-05): stamp the source too. Some files carry a "source" tag
# (CD, WEB, FLAC, "CD (LP)") that beat the playlist id in the play-log; the deal
# counts only rows labelled "rotation", so every such play made the next window
# start one card early -- the daily "same song twice in a row" (Fault B).
radio = map_metadata(fun (_) -> [("rights_class", "collecting_society"), ("source", "rotation")], update=true, strip=false, radio)

requests = request.queue(id="jukebox")

# nr_mood (2026-09-05): the captain's mood queue. library_api feeds it ONE track
# at a time from a chosen playlist (redis mood:*). Strict priority at every
# track end: a paid request first, then the mood queue, then the rotation.
# Nothing is ever cut: fallback with track_sensitive=true switches only at
# track boundaries. The rotation window and the deck cursor are never touched.
mood = request.queue(id="mood")

# nr_jingle (2026-09-09): the station's own IDs. nr-jingle hands ONE to this queue about once an hour and
# liquidsoap plays it at the next track boundary -- below a paid request and below the captain's mood queue,
# so it never outranks a payer and, with track_sensitive=true, never cuts a song short.
# While a MOOD SESSION runs there are no jingles at all: library_api keeps the mood queue loaded across every
# boundary, so this lane never wins one. That is on purpose -- a curated set is not interrupted -- and the
# waiting jingle goes out at the first boundary after the session ends.
# The stamp lives HERE and not at the push, so anything that ever reaches this queue is stamped: the deal
# counts only "rotation" rows, so the deck window and cursor cannot drift, and both royalty rollups keep
# "station" rows out of every pool, so a station ID stays in the record forever and enters no royalty file.
jingle = request.queue(id="jingle")
jingle = map_metadata(fun (_) -> [("rights_class", "station"), ("source", "jingle")], update=true, strip=false, jingle)

radio = fallback(track_sensitive=true, [requests, mood, jingle, radio])

# nr_gain (2026-08-31): obey the per-track replay_gain note that the deal lane
# (nr-shuffle -> nr-gain-annotate) and the request lane (library_api push) write
# from gains.db. Attenuate-only BY CONSTRUCTION at the writers (target -14 LUFS,
# clamp -18 dB, never boost); a track without a note plays at unity (factor 1.).
# The live DJ joins the chain further down and is never touched by this.
radio = amplify(1., override="replay_gain", radio)

# ---- Play logging (royalty pools) ----
playlog = "/var/log/liquidsoap/playlog.tsv"

def log_play(m) =
  # nr_native_clock (2026-08-16): the clock used to come from spawning `date`
  # on every track (process.read). Under load that spawn returned "" and the
  # row was written WITHOUT a timestamp -- 5 of the last 8 rows on 2026-08-16;
  # the BUMA generator drops undated rows. Native clock instead: cannot be
  # empty, no fork per track. Same format as before: 2026-08-16T00:42:56+00:00
  # (%z gives +0000, so the colon is inserted; if %z were ever unsupported the
  # raw string is kept -- never empty).
  ts_raw = time.string("%Y-%m-%dT%H:%M:%S%z")
  ts = if string.length(ts_raw) == 24 then string.sub(ts_raw, start=0, length=22) ^ ":" ^ string.sub(ts_raw, start=22, length=2) else ts_raw end
  title = m["title"]
  artist = m["artist"]
  # nr_ghost_fallback: untagged files used to log a BLANK row -- the tickers
  # kept showing the previous song and the royalty record had a hole. Read the
  # filename instead: strip any track number ("02. ", "04 - ", "A2. "), turn
  # underscores into spaces, then split on the first SPACED " - ".
  # Artist is filled ONLY on that spaced separator: a wrong artist here is a
  # wrong royalty attribution, so ambiguous names keep the whole string as the
  # title. (2026-07-29, every case proven in liquidsoap before deploying.)
  raw = if artist == "" and title == "" then path.remove_extension(path.basename(m["filename"])) else "" end
  unders = string.replace(pattern="_", fun (_) -> " ", raw)
  noleading =
    if string.match(pattern="^[0-9]{1,3}[\\.\\)]?[ _-]+", unders) then
      string.replace(pattern="^[0-9]{1,3}[\\.\\)]?[ _-]+", fun (_) -> "", unders)
    elsif string.match(pattern="^[A-Da-d][0-9]{1,2}[\\.\\)]?[ _-]+", unders) then
      string.replace(pattern="^[A-Da-d][0-9]{1,2}[\\.\\)]?[ _-]+", fun (_) -> "", unders)
    else
      unders
    end
  fname = string.trim(noleading)
  has_dash = string.contains(substring=" - ", fname)
  artist = if fname != "" and has_dash then string.trim(list.hd(default="", string.split(separator=" - ", fname))) else artist end
  title =
    if fname == "" then title
    elsif has_dash then string.trim(string.concat(separator=" - ", list.tl(string.split(separator=" - ", fname))))
    else fname
    end
  rights = if m["rights_class"] == "" then "unknown" else m["rights_class"] end
  src = if m["source"] == "" then "playlist" else m["source"] end
  line = "#{ts}\tplay\t#{src}\t#{rights}\t#{artist}\t#{title}\n"
  # nr_blank_filter: the 2-second blank() joint between tracks has no file
  # behind it -- the filename fallback resolves it to "." and wrote junk rows
  # into the royalty record. Silence is not a play: write nothing at all.
  if artist != "" or (title != "" and title != ".") then
    file.write(append=true, data=line, playlog)
  end
end

radio = source.on_track(radio, log_play)

# Blend AFTER the logger sees the raw track marks (2026-08-14: the
# crossfade upstream of on_track silenced the play-log for 14 min).
radio = crossfade(fade_in=1., fade_out=3., duration=4., radio)

# ---- Live DJ (reverse bridge from Owncast) ----
# The harbor listens only on localhost; the reverse-bridge service feeds it
# the DJ's audio during a live session. track_sensitive=false: the DJ takes
# over mid-track and hands back mid-track -- live means NOW.
settings.harbor.bind_addrs := ["127.0.0.1"]
live_dj = input.harbor("live", port=8005, password=environment.get("ICECAST_SOURCE_PW"))
radio = fallback(track_sensitive=false, [live_dj, radio])
radio = fallback(track_sensitive=false, [radio, blank(duration=2.0)])



# ---- Uitzenden naar Icecast ----
output.icecast(
  %mp3(bitrate=192),
  host="127.0.0.1", port=8000,
  password=environment.get("ICECAST_SOURCE_PW"),
  mount="/stream",
  name="Noderunners Radio",
  description="The first bitcoin-powered radio - The crowd is the DJ",
  genre="Various",
  url="https://noderunnersradio.com",
  radio
)

output.icecast(
  %vorbis(quality=0.6),
  host="127.0.0.1", port=8000,
  password=environment.get("ICECAST_SOURCE_PW"),
  mount="/stream.ogg",
  name="Noderunners Radio (Ogg)",
  description="The first bitcoin-powered radio - The crowd is the DJ",
  genre="Various",
  url="https://noderunnersradio.com",
  radio
)

# nr_bitrates (2026-10-03, SF: "I def need 320 for my own listening experience" + "Why can't we do all three
# options?"): High = MP3 320 kbps, Data saver = AAC 64 kbps, beside the 192 kbps MP3 everyone already uses.
# Both blocks are copies of the Ogg block above; only the encoder, the mount and the name differ.
output.icecast(
  %mp3(bitrate=320),
  host="127.0.0.1", port=8000,
  password=environment.get("ICECAST_SOURCE_PW"),
  mount="/stream-320",
  name="Noderunners Radio (320 kbps)",
  description="The first bitcoin-powered radio - The crowd is the DJ",
  genre="Various",
  url="https://noderunnersradio.com",
  radio
)

output.icecast(
  %ffmpeg(format="adts", %audio(codec="aac", b="64k", ar=44100, channels=2)),
  host="127.0.0.1", port=8000,
  format="audio/aac",
  password=environment.get("ICECAST_SOURCE_PW"),
  mount="/stream-64",
  name="Noderunners Radio (data saver)",
  description="The first bitcoin-powered radio - The crowd is the DJ",
  genre="Various",
  url="https://noderunnersradio.com",
  radio
)
