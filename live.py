# -*- coding: utf-8 -*-
"""Laufende Partien beobachten: Lichess (Stream) und Chess.com (Daily).

Lichess bietet für jede laufende Partie einen echten Live-Stream:

  GET /api/user/<name>/current-game   → Snapshot der laufenden Partie (JSON)
  GET /api/stream/game/<id>           → ND-JSON-Stream: erste Zeile ist die
                                        volle Partie, danach je Zug eine
                                        Zeile {fen, lm, wc, bc}

Chess.com stellt Live-Partien (Blitz/Rapid) über die öffentliche API nicht
bereit — nur laufende *Daily*-Partien (Fernschach) als Snapshot:

  GET /pub/player/<name>/games        → {"games": [{fen, pgn, …}, …]}

Der Tab pollt diese deshalb in Abständen, statt zu streamen. Alle
Funktionen akzeptieren einen injizierbaren `opener` für Offline-Tests.
"""

import json
import socket
import urllib.request
from typing import Callable, List, Optional, Tuple

import chess

import chesscom
import lichess

_UA = "SchachTutor/1.0 (persönliches Trainingstool)"

LICHESS_CURRENT = "https://lichess.org/api/user/{user}/current-game"
LICHESS_STREAM = "https://lichess.org/api/stream/game/{gid}"
CHESSCOM_DAILY = "https://api.chess.com/pub/player/{user}/games"


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": accept})


# ------------------------------------------------------------- Lichess

def lichess_current_game(user: str,
                         opener: Optional[Callable] = None) -> dict:
    """Snapshot der laufenden Partie eines Lichess-Nutzers (404 = keine)."""
    if not lichess._NAME_RE.match(user or ""):
        raise ValueError(f"Ungültiger Lichess-Nutzername: {user!r}")
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=30))
    req = _request(LICHESS_CURRENT.format(user=user), "application/json")
    with open_fn(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stream_game(game_id: str, on_event: Callable[[dict], None],
                stop, opener: Optional[Callable] = None,
                timeout: float = 25.0) -> int:
    """Liest den Live-Stream einer Partie und ruft on_event je Zeile auf.

    Blockiert, bis der Stream endet, `stop` gesetzt wird oder die
    Verbindung `timeout` Sekunden lang still ist (der Aufrufer verbindet
    dann einfach neu — die erste Stream-Zeile ist immer der volle Stand).
    Rückgabe: Anzahl verarbeiteter Ereignisse.
    """
    if not game_id or not game_id.replace("-", "").isalnum():
        raise ValueError(f"Ungültige Partie-ID: {game_id!r}")
    open_fn = opener or (lambda r: urllib.request.urlopen(r,
                                                          timeout=timeout))
    req = _request(LICHESS_STREAM.format(gid=game_id),
                   "application/x-ndjson")
    count = 0
    with open_fn(req) as resp:
        while not stop.is_set():
            try:
                line = resp.readline()
            except (socket.timeout, TimeoutError):
                break                      # still → Aufrufer reconnectet
            if not line:
                break                      # Stream zu Ende (Partie vorbei?)
            line = line.strip()
            if not line:
                continue                   # Keepalive-Leerzeile
            try:
                on_event(json.loads(line.decode("utf-8")))
                count += 1
            except ValueError:
                continue
    return count


# ------------------------------------------------------------ Chess.com

def chesscom_daily_games(user: str,
                         opener: Optional[Callable] = None) -> List[dict]:
    """Laufende Daily-Partien eines Chess.com-Nutzers (Snapshot)."""
    if not chesscom._NAME_RE.match(user or ""):
        raise ValueError(f"Ungültiger Chess.com-Nutzername: {user!r}")
    open_fn = opener or (lambda r: urllib.request.urlopen(r, timeout=30))
    req = _request(CHESSCOM_DAILY.format(user=user.lower()),
                   "application/json")
    with open_fn(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return list(data.get("games", []))


# --------------------------------------------------------------- Helfer

def board_from_san(moves: str) -> Tuple[chess.Board, List[str]]:
    """Baut ein Brett aus einer SAN-Zugliste („e4 e5 Nf3 …")."""
    board = chess.Board()
    sans: List[str] = []
    for token in (moves or "").split():
        try:
            move = board.parse_san(token)
        except ValueError:
            break
        sans.append(board.san(move))
        board.push(move)
    return board, sans


def fmt_clock(value) -> str:
    """Sekunden (oder Millisekunden) → m:ss / h:mm:ss."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return "–"
    if v >= 36000:                # unplausibel groß → vermutlich ms
        v //= 1000
    h, rest = divmod(v, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
