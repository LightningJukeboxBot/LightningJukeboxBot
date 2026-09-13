"""
buma_reports.py -- turn the station's play-log into BUMA/STEMRA-ready data.

Read-only over /var/log/liquidsoap/playlog.tsv. Used by library_api.py for the
captain's console BUMA tab (status + CSV downloads) and importable standalone
for testing. Mirrors the PC-side make-reports.py rules exactly:

  - NUL bytes stripped (the logger's first days wrote a corrupted source
    field; artist/title/date in those rows are intact and are recovered)
  - rows without a timestamp (the 21-22 July shakedown) are counted and
    reported, never silently mixed into a dated list
  - CSVs are semicolon-separated with a UTF-8 BOM so Dutch Excel opens them
"""

import io
import csv
import os
from collections import Counter, defaultdict

PLAYLOG = "/var/log/liquidsoap/playlog.tsv"


_REGELING = {
    # per-row label so the society can see, not guess, which rows are outside
    # their lane. We LABEL rather than omit: a V4V artist could still be a
    # society member somewhere, so the society decides -- we never cherry-pick.
    "collecting_society": "licentie",
    "v4v": "V4V — rechtstreeks aan artiest betaald",
    "unknown": "onbekend (testfase)",
}


def _rows():
    """Yield (month, date, time, artist, title, blank, regeling) per play."""
    if not os.path.exists(PLAYLOG):
        return
    with open(PLAYLOG, "rb") as fh:
        for bline in fh:
            line = bline.replace(b"\x00", b"").decode("utf-8", "replace")
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6 or parts[1] != "play":
                continue
            ts, artist, title = parts[0], parts[4].strip(), parts[5].strip()
            reg = _REGELING.get(parts[3].strip(), parts[3].strip() or "onbekend")
            if not ts.startswith("20"):
                yield (None, None, None, artist, title, False, reg)
                continue
            blank = not artist and not title
            yield (ts[:7], ts[:10], ts[11:19], artist, title, blank, reg)


def status() -> dict:
    """Per-month totals + log health, for the console tab."""
    months = defaultdict(lambda: {"plays": 0, "unique": set(), "blank": 0,
                                  "v4v": 0})
    undated = 0
    first = last = None
    for month, date, time_, artist, title, blank, reg in _rows():
        if month is None:
            undated += 1
            continue
        stamp = date + "T" + time_
        first = stamp if first is None else min(first, stamp)
        last = stamp if last is None else max(last, stamp)
        m = months[month]
        if blank:
            m["blank"] += 1
        else:
            m["plays"] += 1
            m["unique"].add((artist, title))
            if reg.startswith("V4V"):
                m["v4v"] += 1
    return {
        "log_path": PLAYLOG,
        "log_exists": os.path.exists(PLAYLOG),
        "first_entry": first,
        "last_entry": last,
        "undated_test_rows": undated,
        "months": [
            {"month": k, "plays": v["plays"], "unique": len(v["unique"]),
             "blank": v["blank"], "v4v": v["v4v"]}
            for k, v in sorted(months.items())
        ],
    }


def csv_speellijst(month: str) -> bytes:
    """Every dated play of one month: Datum;Tijd (UTC);Artiest;Titel."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Datum", "Tijd (UTC)", "Artiest", "Titel", "Regeling"])
    for m, date, time_, artist, title, blank, reg in _rows():
        if m == month and not blank:
            w.writerow([date, time_, artist, title, reg])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def csv_overzicht(month: str) -> bytes:
    """One month condensed: Artiest;Titel;Aantal keer gespeeld."""
    counts = Counter()
    for m, _d, _t, artist, title, blank, reg in _rows():
        if m == month and not blank:
            counts[(artist, title, reg)] += 1
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Artiest", "Titel", "Aantal keer gespeeld", "Regeling"])
    for (artist, title, reg), n in counts.most_common():
        w.writerow([artist, title, n, reg])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def rss(limit: int = 50) -> bytes:
    """The most recent plays as RSS 2.0 -- a live feed a society can poll."""
    recent = [r for r in _rows() if r[0] is not None and not r[5]]
    recent = recent[-limit:][::-1]
    items = []
    for _m, date, time_, artist, title, _b, _reg in recent:
        label = _xml_escape("%s — %s" % (artist or "?", title or "?"))
        items.append(
            "<item><title>%s</title>"
            "<guid isPermaLink=\"false\">%s</guid>"
            "<pubDate>%s %s UTC</pubDate></item>"
            % (label, _xml_escape(date + "T" + time_ + "|" + label),
               date, time_))
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Noderunners Radio — aired tracks</title>"
        "<link>https://noderunnersradio.com</link>"
        "<description>Every track as it airs, for playlist logging"
        " (BUMA/STEMRA/Sena welcome).</description>"
        + "".join(items) + "</channel></rss>")
    return doc.encode("utf-8")
