# -*- coding: utf-8 -*-
"""Partien-Export von Chess.com (öffentliche Published-Data-API, ohne Token).

Chess.com liefert Partien nicht als einen Stream, sondern als
Monatsarchive:

  GET /pub/player/<name>/games/archives   → Liste der Monats-URLs
  GET /pub/player/<name>/games/YYYY/MM    → JSON mit allen Partien (inkl. PGN)

Diese Funktion geht die Archive vom neuesten Monat rückwärts durch, bis
`max_games` erreicht ist, filtert auf normales Schach (keine Varianten wie
Chess960) sowie gewünschte Bedenkzeiten, und schreibt alles chronologisch
in eine PGN-Datei. Die API verlangt einen aussagekräftigen User-Agent und
serielle Zugriffe — beides wird hier eingehalten. Der Abruf gehört in
einen Hintergrund-Thread (macht der Tab).
"""

import json
import re
import urllib.request
from typing import Callable, List, Optional

ARCHIVES = "https://api.chess.com/pub/player/{user}/games/archives"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,25}$")
_UA = "SchachTutor/1.0 (persönliches Trainingstool)"


def _fetch_json(url: str, open_fn) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "application/json"})
    with open_fn(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_games(user: str, dest_path: str, max_games: int = 2000,
                   time_classes: str = "blitz,rapid,daily",
                   rated: bool = True,
                   opener: Optional[Callable] = None,
                   progress: Optional[Callable[[int, str], None]] = None
                   ) -> int:
    """Lädt Partien eines Chess.com-Nutzers als PGN nach dest_path.

    Rückgabe: Anzahl geschriebener Partien. `opener` ist für Tests
    injizierbar (Default: urllib.request.urlopen). `progress` erhält
    (Partien bisher, gerade geladener Monat).
    """
    if not _NAME_RE.match(user or ""):
        raise ValueError(f"Ungültiger Chess.com-Nutzername: {user!r}")
    wanted = {t.strip() for t in time_classes.split(",") if t.strip()}
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=120))

    archives = _fetch_json(ARCHIVES.format(user=user.lower()),
                           open_fn).get("archives", [])
    collected: List[str] = []          # neueste zuerst
    for month_url in reversed(archives):
        if len(collected) >= max_games:
            break
        month = "/".join(month_url.rsplit("/", 2)[-2:])   # "YYYY/MM"
        data = _fetch_json(month_url, open_fn)
        games = data.get("games", [])
        for g in reversed(games):                          # neueste zuerst
            if len(collected) >= max_games:
                break
            if g.get("rules", "chess") != "chess":
                continue                                   # keine Varianten
            if rated and not g.get("rated", False):
                continue
            if wanted and g.get("time_class") not in wanted:
                continue
            pgn = (g.get("pgn") or "").strip()
            if pgn:
                collected.append(pgn)
        if progress:
            progress(len(collected), month)

    collected.reverse()                                    # chronologisch
    with open(dest_path, "w", encoding="utf-8") as out:
        out.write("\n\n".join(collected) + ("\n" if collected else ""))
    return len(collected)
