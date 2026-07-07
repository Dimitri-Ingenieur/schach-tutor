# -*- coding: utf-8 -*-
"""Partien-Export von Lichess (öffentliche API, ohne Token).

GET https://lichess.org/api/games/user/<name> liefert alle öffentlichen
Partien eines Nutzers als PGN-Stream. Ohne API-Token drosselt Lichess auf
etwa 20 Partien pro Sekunde — 1400 Partien dauern also gut eine Minute.
Der Abruf gehört deshalb in einen Hintergrund-Thread (macht der Tab).
"""

import re
import urllib.request
from typing import Callable, Optional

API = "https://lichess.org/api/games/user/{user}"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")


def download_games(user: str, dest_path: str, max_games: int = 2000,
                   perf_types: str = "blitz,rapid,classical",
                   rated: bool = True,
                   opener: Optional[Callable] = None,
                   progress: Optional[Callable[[int], None]] = None,
                   chunk: int = 1 << 16) -> int:
    """Lädt die Partien eines Lichess-Nutzers als PGN nach dest_path.

    Rückgabe: Anzahl geschriebener Bytes. `opener` ist für Tests
    injizierbar (Default: urllib.request.urlopen).
    """
    if not _NAME_RE.match(user or ""):
        raise ValueError(f"Ungültiger Lichess-Nutzername: {user!r}")
    params = [f"max={int(max_games)}", "clocks=false", "evals=false",
              "opening=false"]
    if rated:
        params.append("rated=true")
    if perf_types:
        params.append("perfType=" + perf_types)
    url = API.format(user=user) + "?" + "&".join(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "SchachTutor/1.0 (persönliches Trainingstool)",
        "Accept": "application/x-chess-pgn",
    })
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=600))
    total = 0
    with open_fn(req) as resp, open(dest_path, "wb") as out:
        while True:
            block = resp.read(chunk)
            if not block:
                break
            out.write(block)
            total += len(block)
            if progress:
                progress(total)
    return total
