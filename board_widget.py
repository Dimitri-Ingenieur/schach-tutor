# -*- coding: utf-8 -*-
"""Tkinter-Schachbrett mit Maussteuerung (Klick–Klick) und Bewertungsbalken.

Figuren: eingebettete PNG-Bilder des Cburnett-Satzes (pieces.py) – gerendert
über Pillow in beliebiger Feldgröße. Ist Pillow nicht installiert, fällt das
Brett automatisch auf Unicode-Glyphen mit Kontur-Trick zurück.
"""

import base64
import io
import tkinter as tk
from typing import Callable, List, Optional, Tuple

import chess

try:
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except ImportError:              # Fallback: Unicode-Glyphen
    _HAVE_PIL = False

try:
    from pieces import PIECE_PNG_B64
except ImportError:
    PIECE_PNG_B64 = {}

LIGHT = "#f0d9b5"
DARK = "#b58863"
LAST_LIGHT = "#f7ec74"
LAST_DARK = "#dcc34b"
CHECK_COLOR = "#e2514c"
SELECT_OUTLINE = "#2f6ea5"
DOT_COLOR = "#6e6e6e"
COORD_COLOR = "#d8d8d8"
BG = "#3a3a3a"

GLYPH = {chess.PAWN: "\u265f", chess.KNIGHT: "\u265e", chess.BISHOP: "\u265d",
         chess.ROOK: "\u265c", chess.QUEEN: "\u265b", chess.KING: "\u265a"}

ARROW_BEST = "#15781b"
ARROW_BAD = "#b02a1e"
ARROW_HINT = "#1c6fb0"


class BoardWidget(tk.Canvas):
    """Anzeige + Eingabe. Die Anwendung hält den Spielzustand; das Widget
    bekommt Stellungen über set_position() und meldet Züge über on_move()."""

    def __init__(self, master, square: int = 64, interactive: bool = True,
                 on_move: Optional[Callable[[chess.Move], None]] = None, **kw):
        self.square = square
        self.margin = max(18, square // 3)
        size = self.margin * 2 + square * 8
        super().__init__(master, width=size, height=size, bg=BG,
                         highlightthickness=0, **kw)
        self.board = chess.Board()
        self.flipped = False
        self.interactive = interactive
        self.on_move = on_move

        self.selected: Optional[int] = None
        self.legal_targets: List[int] = []
        self.last_move: Optional[chess.Move] = None
        self.arrows: List[Tuple[int, int, str]] = []

        self._piece_font = ("DejaVu Sans", int(square * 0.62))
        self._coord_font = ("DejaVu Sans", max(8, square // 6))

        self.bind("<Button-1>", self._on_click)
        self.redraw()

    # ------------------------------------------------------------ API

    def set_position(self, board: chess.Board,
                     last_move: Optional[chess.Move] = None) -> None:
        self.board = board.copy()
        self.last_move = last_move
        self.selected = None
        self.legal_targets = []
        self.redraw()

    def set_interactive(self, value: bool) -> None:
        self.interactive = value
        if not value:
            self.selected = None
            self.legal_targets = []
        self.redraw()

    def set_flipped(self, value: bool) -> None:
        if self.flipped != value:
            self.flipped = value
            self.redraw()

    def set_arrows(self, arrows: List[Tuple[int, int, str]]) -> None:
        """arrows: Liste (von_feld, nach_feld, farbe)."""
        self.arrows = list(arrows)
        self.redraw()

    def clear_arrows(self) -> None:
        if self.arrows:
            self.arrows = []
            self.redraw()

    # ------------------------------------------------------------ Geometrie

    def _square_xy(self, sq: int) -> Tuple[int, int]:
        f, r = chess.square_file(sq), chess.square_rank(sq)
        col = 7 - f if self.flipped else f
        row = r if self.flipped else 7 - r
        return (self.margin + col * self.square,
                self.margin + row * self.square)

    def _xy_square(self, x: int, y: int) -> Optional[int]:
        col = (x - self.margin) // self.square
        row = (y - self.margin) // self.square
        if not (0 <= col <= 7 and 0 <= row <= 7):
            return None
        f = 7 - col if self.flipped else col
        r = row if self.flipped else 7 - row
        return chess.square(int(f), int(r))

    def _center(self, sq: int) -> Tuple[int, int]:
        x, y = self._square_xy(sq)
        return x + self.square // 2, y + self.square // 2

    # ------------------------------------------------------------ Zeichnen

    def redraw(self) -> None:
        self.delete("all")
        s = self.square

        last_from = self.last_move.from_square if self.last_move else None
        last_to = self.last_move.to_square if self.last_move else None
        check_sq = None
        if self.board.is_check():
            check_sq = self.board.king(self.board.turn)

        for sq in chess.SQUARES:
            x, y = self._square_xy(sq)
            light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
            color = LIGHT if light else DARK
            if sq in (last_from, last_to):
                color = LAST_LIGHT if light else LAST_DARK
            if sq == check_sq:
                color = CHECK_COLOR
            self.create_rectangle(x, y, x + s, y + s, fill=color, width=0)

        # Auswahl-Rahmen
        if self.selected is not None:
            x, y = self._square_xy(self.selected)
            self.create_rectangle(x + 1, y + 1, x + s - 1, y + s - 1,
                                  outline=SELECT_OUTLINE, width=3)

        # Zielfeld-Markierungen
        for sq in self.legal_targets:
            cx, cy = self._center(sq)
            if self.board.piece_at(sq) is not None:
                r = int(s * 0.44)
                self.create_oval(cx - r, cy - r, cx + r, cy + r,
                                 outline=DOT_COLOR, width=3)
            else:
                r = int(s * 0.13)
                self.create_oval(cx - r, cy - r, cx + r, cy + r,
                                 fill=DOT_COLOR, width=0)

        # Figuren
        img_size = int(s * 0.96)
        for sq, piece in self.board.piece_map().items():
            cx, cy = self._center(sq)
            img = self._piece_image(piece, img_size)
            if img is not None:
                self.create_image(cx, cy, image=img)
                continue
            glyph = GLYPH[piece.piece_type]
            if piece.color == chess.WHITE:
                main, outline = "#fafafa", "#1c1c1c"
            else:
                main, outline = "#141414", "#b5b5b5"
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                self.create_text(cx + dx, cy + dy, text=glyph,
                                 font=self._piece_font, fill=outline)
            self.create_text(cx, cy, text=glyph,
                             font=self._piece_font, fill=main)

        # Koordinaten
        for i in range(8):
            file_char = chr(ord("a") + (7 - i if self.flipped else i))
            rank_char = str(i + 1 if self.flipped else 8 - i)
            x = self.margin + i * s + s // 2
            y_bottom = self.margin + 8 * s + self.margin // 2
            self.create_text(x, y_bottom, text=file_char,
                             font=self._coord_font, fill=COORD_COLOR)
            yr = self.margin + i * s + s // 2
            self.create_text(self.margin // 2, yr, text=rank_char,
                             font=self._coord_font, fill=COORD_COLOR)

        # Pfeile
        for from_sq, to_sq, color in self.arrows:
            x1, y1 = self._center(from_sq)
            x2, y2 = self._center(to_sq)
            self.create_line(x1, y1, x2, y2, fill=color,
                             width=max(5, s // 10), capstyle=tk.ROUND,
                             arrow=tk.LAST,
                             arrowshape=(s // 3, s // 3 + 6, s // 6))

    # ------------------------------------------------------------ Eingabe

    def _on_click(self, event) -> None:
        if not self.interactive:
            return
        sq = self._xy_square(event.x, event.y)
        if sq is None:
            self.selected = None
            self.legal_targets = []
            self.redraw()
            return

        if self.selected is not None and sq in self.legal_targets:
            move = self._build_move(self.selected, sq)
            self.selected = None
            self.legal_targets = []
            self.redraw()
            if move is not None and self.on_move:
                self.on_move(move)
            return

        piece = self.board.piece_at(sq)
        if piece is not None and piece.color == self.board.turn:
            self.selected = sq
            self.legal_targets = [m.to_square for m in self.board.legal_moves
                                  if m.from_square == sq]
        else:
            self.selected = None
            self.legal_targets = []
        self.redraw()

    def _build_move(self, from_sq: int, to_sq: int) -> Optional[chess.Move]:
        move = chess.Move(from_sq, to_sq)
        if self.board.is_legal(move):
            return move
        # Umwandlung nötig?
        promo = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)
        if self.board.is_legal(promo):
            piece_type = self._ask_promotion()
            return chess.Move(from_sq, to_sq, promotion=piece_type)
        return None

    def _piece_image(self, piece: chess.Piece, size: int):
        """Skaliertes Figurenbild aus dem eingebetteten PNG-Satz (o. None)."""
        if not (_HAVE_PIL and PIECE_PNG_B64) or size < 8:
            return None
        if not hasattr(self, "_piece_imgs"):
            self._piece_imgs = {}
            self._piece_src = {}
        key = ("w" if piece.color == chess.WHITE else "b") + \
            piece.symbol().upper()
        ck = (key, size)
        img = self._piece_imgs.get(ck)
        if img is None:
            src = self._piece_src.get(key)
            if src is None:
                b64 = PIECE_PNG_B64.get(key)
                if not b64:
                    return None
                raw = base64.b64decode(b64)
                src = Image.open(io.BytesIO(raw)).convert("RGBA")
                self._piece_src[key] = src
            scaled = src.resize((size, size), Image.LANCZOS)
            img = ImageTk.PhotoImage(scaled, master=self)
            self._piece_imgs[ck] = img
        return img

    def _ask_promotion(self) -> int:
        win = tk.Toplevel(self)
        win.title("Umwandlung")
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        result = {"pt": chess.QUEEN}
        choices = [(chess.QUEEN, "\u265b"), (chess.ROOK, "\u265c"),
                   (chess.BISHOP, "\u265d"), (chess.KNIGHT, "\u265e")]
        win._imgs = []
        for pt, glyph in choices:
            img = self._piece_image(chess.Piece(pt, self.board.turn), 52)
            if img is not None:
                win._imgs.append(img)
                btn = tk.Button(win, image=img,
                                command=lambda p=pt: (result.update(pt=p),
                                                      win.destroy()))
            else:
                btn = tk.Button(win, text=glyph, font=("DejaVu Sans", 26),
                                width=2,
                                command=lambda p=pt: (result.update(pt=p),
                                                      win.destroy()))
            btn.pack(side=tk.LEFT, padx=4, pady=4)
        win.grab_set()
        win.wait_window()
        return result["pt"]


class EvalBar(tk.Canvas):
    """Vertikaler Bewertungsbalken (weißer Anteil = Gewinnchance Weiß)."""

    def __init__(self, master, height: int, width: int = 26, **kw):
        super().__init__(master, width=width, height=height, bg="#222",
                         highlightthickness=0, **kw)
        self._bar_h = height
        self._bar_w = width
        self._frac = 0.5
        self._label = "0.0"
        self._draw()

    def set_eval(self, win_pct_white: float, label: str) -> None:
        self._frac = max(0.02, min(0.98, win_pct_white / 100.0))
        self._label = label
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        split = int(self._bar_h * (1.0 - self._frac))
        self.create_rectangle(0, 0, self._bar_w, split, fill="#333333", width=0)
        self.create_rectangle(0, split, self._bar_w, self._bar_h,
                              fill="#e8e8e8", width=0)
        y = self._bar_h - 12 if self._frac >= 0.5 else 12
        color = "#333333" if self._frac >= 0.5 else "#e8e8e8"
        self.create_text(self._bar_w // 2, y, text=self._label,
                         font=("DejaVu Sans", 8, "bold"), fill=color)
