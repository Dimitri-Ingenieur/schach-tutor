# -*- coding: utf-8 -*-
"""Synthetischer Brett-Renderer für Tests der Vision-Pipeline.

Rendert Stellungen mit den eingebetteten Cburnett-Figuren (pieces.py) auf
ein 2D-Brett — wir kontrollieren also die Bild-Wahrheit vollständig und
können die Erkennung headless und deterministisch testen, inklusive eines
echten Video-Roundtrips über OpenCV (VideoWriter → VideoCapture).
"""

import base64
import io
from typing import Dict, Tuple

import chess
import numpy as np
from PIL import Image

from pieces import PIECE_PNG_B64

SQ = 48
LIGHT = (240, 217, 181)
DARK = (181, 136, 99)
OFFSET = (37, 21)
CANVAS = (520, 460)
BOARD_RECT = (OFFSET[0], OFFSET[1], 8 * SQ, 8 * SQ)

_sprites: Dict[str, Image.Image] = {}


def _sprite(symbol: str) -> Image.Image:
    if symbol not in _sprites:
        key = ("w" if symbol.isupper() else "b") + symbol.upper()
        img = Image.open(io.BytesIO(base64.b64decode(PIECE_PNG_B64[key])))
        _sprites[symbol] = img.convert("RGBA").resize((SQ, SQ),
                                                      Image.LANCZOS)
    return _sprites[symbol]


def render_frame(board: chess.Board, flipped: bool = False,
                 offset: Tuple[int, int] = OFFSET,
                 canvas: Tuple[int, int] = CANVAS) -> np.ndarray:
    """Stellung als RGB-Frame (H, W, 3) — wie ein Stream-Standbild."""
    im = Image.new("RGB", canvas, (40, 40, 40))
    ox, oy = offset
    for r in range(8):
        for c in range(8):
            file, rank = (7 - c, r) if flipped else (c, 7 - r)
            color = LIGHT if (file + rank) % 2 else DARK
            x, y = ox + c * SQ, oy + r * SQ
            im.paste(Image.new("RGB", (SQ, SQ), color), (x, y))
            piece = board.piece_at(chess.square(file, rank))
            if piece:
                s = _sprite(piece.symbol())
                im.paste(s, (x, y), s)
    return np.array(im)
