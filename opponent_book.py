# -*- coding: utf-8 -*-
"""Polyglot-Eröffnungsbuch aus den Partien eines Spielers erzeugen.

Das Buch enthält genau die Züge, die der Spieler selbst gespielt hat,
gewichtet nach Häufigkeit. Als Engine-Buch eingetragen (Einstellungen →
Polyglot-Buch), eröffnet die Trainings-Engine dann exakt mit seinem
Repertoire und geht erst danach in die eigene Berechnung über —
zusammen mit einem Maia-Netz ergibt das eine brauchbare Gegner-Simulation.
"""

import struct
from typing import Dict, List, Tuple

import chess
import chess.pgn
import chess.polyglot

_PROMO = {chess.KNIGHT: 1, chess.BISHOP: 2, chess.ROOK: 3, chess.QUEEN: 4}


def _poly_raw_move(board: chess.Board, move: chess.Move) -> int:
    """Polyglot-Kodierung eines Zuges.

    Rochade wird — wie im Polyglot-Standard — als „König schlägt eigenen
    Turm" kodiert (e1h1 statt e1g1); python-chess konvertiert das beim
    Lesen automatisch zurück.
    """
    to_sq = move.to_square
    if board.is_castling(move):
        rank = chess.square_rank(move.from_square)
        kingside = chess.square_file(move.to_square) > chess.square_file(
            move.from_square)
        to_sq = chess.square(7 if kingside else 0, rank)
    promo = _PROMO.get(move.promotion, 0) if move.promotion else 0
    return (promo << 12) | (move.from_square << 6) | to_sq


def build_book(games: List["chess.pgn.Game"], player: str, path: str,
               max_ply: int = 30) -> Tuple[int, int]:
    """Schreibt ein Polyglot-Buch (.bin) mit den Zügen des Spielers.

    Rückgabe: (Anzahl Stellungen, Anzahl Bucheinträge).
    """
    counter: Dict[Tuple[int, int], int] = {}
    for game in games:
        if game.headers.get("White", "") == player:
            color = chess.WHITE
        elif game.headers.get("Black", "") == player:
            color = chess.BLACK
        else:
            continue
        board = game.board()
        for ply, move in enumerate(game.mainline_moves()):
            if ply >= max_ply:
                break
            if not board.is_legal(move):
                break
            if board.turn == color:
                key = chess.polyglot.zobrist_hash(board)
                raw = _poly_raw_move(board, move)
                counter[(key, raw)] = counter.get((key, raw), 0) + 1
            board.push(move)

    entries = sorted(counter.items(), key=lambda kv: (kv[0][0], -kv[1]))
    with open(path, "wb") as f:
        for (key, raw), n in entries:
            f.write(struct.pack(">QHHI", key, raw, min(n, 65535), 0))
    positions = len({key for (key, _raw) in counter})
    return positions, len(entries)
