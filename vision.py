# -*- coding: utf-8 -*-
"""Zugerkennung aus Videobildern digitaler Schachbretter.

Kernidee: Statt jede Figur in jedem Frame zu klassifizieren (fehleranfällig,
figurensatz-abhängig), wird pro Feld nur **Belegung und Figurfarbe**
bestimmt — leer / weiße Figur / schwarze Figur. Der gespielte Zug ergibt
sich dann per Legalitäts-Abgleich: Welcher legale Zug der bekannten
Ausgangsstellung erzeugt exakt das beobachtete Belegungsmuster? Das erkennt
Rochade und en passant automatisch mit und braucht keinerlei Wissen über
den Figurensatz des Streams. Umwandlungen sind belegungsgleich für alle
Umwandlungsfiguren — es wird die Dame angenommen (Log-Hinweis macht der Tab).

Kalibrierung: Ein einziges Standbild einer **bekannten Stellung** (meist die
Ausgangsstellung) genügt. Daraus lernt der Klassifikator die Schwellwerte
für "Figur vorhanden" und "weiß vs. schwarz" — angepasst an Brettfarben,
Figurensatz und Helligkeit genau dieses Streams. Die Brett-Orientierung
(Weiß unten oder oben) wird dabei automatisch erkannt.

Geltungsbereich (bewusst): digitale 2D-Bretter (Streams, Videos,
Bildschirm). Physische Bretter über eine Kamera (Perspektive, 3D-Figuren,
Verdeckung durch Hände) sind ein eigenes Forschungsthema und hier kein Ziel.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import chess
import numpy as np

EMPTY, WHITE_PC, BLACK_PC = 0, 1, 2

# So viele aufeinanderfolgende Frames müssen dasselbe Belegungsmuster
# zeigen, bevor ein Zug abgeleitet wird (überbrückt Zug-Animationen,
# Mauszeiger, Pfeil-Overlays).
STABLE_FRAMES = 3

Grid = Tuple[Tuple[int, ...], ...]          # 8x8, [rank8..rank1][fileA..H]


# ---------------------------------------------------------------- Belegung

def occupancy_from_board(board: chess.Board) -> Grid:
    """Soll-Belegung einer Stellung, Blickrichtung: Weiß unten."""
    rows = []
    for rank in range(7, -1, -1):
        row = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            if piece is None:
                row.append(EMPTY)
            else:
                row.append(WHITE_PC if piece.color == chess.WHITE
                           else BLACK_PC)
        rows.append(tuple(row))
    return tuple(rows)


def flip_grid(grid: Grid) -> Grid:
    """Belegung aus Sicht des gedrehten Bretts (Schwarz unten)."""
    return tuple(tuple(reversed(row)) for row in reversed(grid))


# ---------------------------------------------------------------- Zellen

def split_cells(frame: np.ndarray, rect: Tuple[int, int, int, int]
                ) -> List[List[np.ndarray]]:
    """Schneidet das Brett-Rechteck (x, y, w, h) in 8x8 Zell-Bilder."""
    x, y, w, h = rect
    frame_h, frame_w = frame.shape[:2]
    x = max(0, min(x, frame_w - 8))
    y = max(0, min(y, frame_h - 8))
    w = max(8, min(w, frame_w - x))
    h = max(8, min(h, frame_h - y))
    cells: List[List[np.ndarray]] = []
    for r in range(8):
        row = []
        y0 = y + r * h // 8
        y1 = y + (r + 1) * h // 8
        for c in range(8):
            x0 = x + c * w // 8
            x1 = x + (c + 1) * w // 8
            row.append(frame[y0:y1, x0:x1])
        cells.append(row)
    return cells


def _cell_features(cell: np.ndarray) -> Tuple[float, float]:
    """(Figur-Pixelanteil, Hell-Anteil der Figur-Pixel 0..1).

    Hintergrund = Median des Zellrands (Figuren berühren den Rand auf
    2D-Brettern praktisch nie). Figur-Pixel = deutliche Abweichung davon
    im Zellzentrum, getrennt in heller/dunkler als der Hintergrund.
    Der Hell-Anteil (hell / (hell+dunkel)) trennt weiße von schwarzen
    Figuren robust: Weiße Figuren haben große reinweiße Flächen, schwarze
    große reindunkle — die Linienzeichnung der jeweils anderen Farbe
    (Konturen, Details) kann das Mengenverhältnis kaum kippen, den Median
    dagegen schon (deshalb kein Median).
    """
    if cell.ndim == 3:
        gray = cell.astype(np.float32).mean(axis=2)
    else:
        gray = cell.astype(np.float32)
    h, w = gray.shape
    m = max(1, int(min(h, w) * 0.12))
    border = np.concatenate([gray[:m].ravel(), gray[-m:].ravel(),
                             gray[:, :m].ravel(), gray[:, -m:].ravel()])
    bg = float(np.median(border))
    core = gray[m:h - m, m:w - m]
    if core.size == 0:
        return 0.0, 0.5
    # Zwei Toleranzbänder: streng (±28) für "Figur vorhanden" — hält
    # leere Felder auch bei Videokompression sauber leer. Locker (±14)
    # für die Farbentscheidung — sonst verschwindet der antialiaste
    # Korpus einer weißen Figur auf hellem Feld (bzw. einer schwarzen
    # auf dunklem) im Band, und die Linienzeichnung der Gegenfarbe
    # dominiert das Verhältnis.
    strict = np.abs(core - bg) > 28.0
    ratio = float(strict.mean())
    bright = int((core > bg + 14.0).sum())
    dark = int((core < bg - 14.0).sum())
    if bright + dark == 0:
        return ratio, 0.5
    return ratio, bright / float(bright + dark)


# ---------------------------------------------------------- Klassifikator

@dataclass
class CellClassifier:
    """Lernt aus EINEM Bild einer bekannten Stellung die Schwellwerte."""
    presence_thr: float = 0.05
    color_thr: float = 127.0
    flipped: bool = False
    calibrated: bool = False

    def calibrate(self, cells: List[List[np.ndarray]],
                  expected: chess.Board) -> bool:
        """Versucht beide Orientierungen; True bei Erfolg."""
        want = occupancy_from_board(expected)
        feats = [[_cell_features(cells[r][c]) for c in range(8)]
                 for r in range(8)]
        for flipped, grid in ((False, want), (True, flip_grid(want))):
            occ_ratios, empty_ratios = [], []
            white_vals, black_vals = [], []
            for r in range(8):
                for c in range(8):
                    ratio, val = feats[r][c]
                    if grid[r][c] == EMPTY:
                        empty_ratios.append(ratio)
                    else:
                        occ_ratios.append(ratio)
                        (white_vals if grid[r][c] == WHITE_PC
                         else black_vals).append(val)
            if not occ_ratios or not white_vals or not black_vals:
                continue
            # Perzentile statt Extrema: eine einzelne "verschmutzte"
            # Zelle (Figurkante ragt hinein, Koordinaten-Beschriftung)
            # darf die Kalibrierung nicht allein kippen.
            if empty_ratios:
                lo = float(np.percentile(empty_ratios, 95))
            else:
                lo = 0.0
            hi = float(np.percentile(occ_ratios, 5))
            if hi <= lo * 1.5 + 0.01:
                continue                      # Orientierung passt nicht
            # Schwelle mittig zwischen die KLASSENRÄNDER legen (min der
            # helleren Klasse vs. max der dunkleren), nicht zwischen die
            # Mediane: Detailreiche Figuren (v. a. die Damen) liegen weit
            # vom Median ihrer Klasse entfernt und würden sonst genau am
            # Median-Mittelwert abgeschnitten.
            # Absoluter Anker: Weiße Figuren sind auf 2D-Brettern die
            # helleren. Ohne diese Annahme wäre bei farbsymmetrischen
            # Stellungen (v. a. der Ausgangsstellung!) "gedrehtes Brett"
            # von "ungedrehtes Brett mit vertauschten Farben" prinzipiell
            # nicht unterscheidbar — beide Deutungen wären in sich
            # konsistent, und die Orientierung würde geraten.
            if (float(np.median(white_vals))
                    <= float(np.median(black_vals))):
                continue
            w_min, b_max = min(white_vals), max(black_vals)
            if w_min <= b_max + 0.05:
                continue                      # Farben nicht trennbar
            self.presence_thr = (lo + hi) / 2.0
            # Schwelle bewusst asymmetrisch näher an die schwarze Klasse:
            # Die Detailzeichnung drückt weiße Figuren nach unten (viel
            # dunkle Binnenlinien, v. a. bei der Dame), während die dünne
            # helle Kontur schwarzer Figuren deren Wert kaum anhebt. Bei
            # spärlichen Kalibrier-Stellungen (wenige Figurtypen als
            # Beispiele) fängt das später auftauchende, zeichnungsreiche
            # weiße Figuren ab, die unter dem w_min der Kalibrierung
            # liegen würden.
            self.color_thr = b_max + 0.35 * (w_min - b_max)
            self._white_is_bright = True
            self.flipped = flipped
            self.calibrated = True
            # Trennschärfe (für die automatische Rechteck-Suche):
            # je größer, desto sauberer sitzt das Raster auf dem Brett.
            self.margin = (hi - lo) + 2.0 * (w_min - b_max)
            return True
        return False

    def calibrate_auto(self, cells: List[List[np.ndarray]]) -> bool:
        """Kalibrieren OHNE bekannte Stellung — funktioniert genau dann,
        wenn das Bild die Ausgangsstellung zeigt (beliebige Orientierung).

        Schwellwerte kommen aus unüberwachtem Clustering: Der Figur-
        Pixelanteil der 64 Zellen ist bimodal (leer vs. besetzt) — die
        größte Lücke in der sortierten Folge trennt die Klassen. Ebenso
        der Hell-Anteil der besetzten Zellen (weiß vs. schwarz), mit
        Weiß = hellere Klasse als Anker. Ergibt das entstehende Muster
        die Ausgangsstellung (normal oder gedreht), ist damit auch die
        Orientierung bekannt. Mittelspiel-Stellungen brauchen weiterhin
        einen FEN (ohne Figurtypen keine Stellung).
        """
        feats = [[_cell_features(cells[r][c]) for c in range(8)]
                 for r in range(8)]
        ratios = sorted(f[0] for row in feats for f in row)
        gaps = [(ratios[i + 1] - ratios[i], i) for i in range(63)]
        gap, i = max(gaps)
        n_occupied = 63 - i
        # Untergrenze 2 (beide Könige), nicht 16: Die Erkennung deckt
        # längst auch Mittel- und Endspiele ab, nicht nur die
        # Ausgangsstellung — die Klassentrennung selbst leistet das
        # Lücken-Kriterium.
        if gap < 0.03 or not 2 <= n_occupied <= 62:
            return False
        presence_thr = (ratios[i] + ratios[i + 1]) / 2.0
        vals = sorted(f[1] for row in feats for f in row
                      if f[0] >= presence_thr)
        vgaps = [(vals[j + 1] - vals[j], j) for j in range(len(vals) - 1)]
        vgap, j = max(vgaps)
        if vgap < 0.08:
            return False
        self.presence_thr = presence_thr
        self.color_thr = vals[j] + 0.35 * (vals[j + 1] - vals[j])
        self._white_is_bright = True
        self.flipped = False
        self.detected_board: Optional[chess.Board] = None
        self.turn_uncertain = False
        raw = self.classify(cells)           # noch ungedreht interpretiert
        want = occupancy_from_board(chess.Board())
        if raw == want:
            self.detected_board = chess.Board()
        elif raw == flip_grid(want):
            self.flipped = True
            self.detected_board = chess.Board()
        else:
            # Keine Ausgangsstellung → Figurtypen erkennen. Beide
            # Orientierungen probieren. Wichtig: Die Pixel selbst tragen
            # KEINE Orientierungsinformation (Figuren stehen auf 2D-
            # Brettern immer aufrecht, egal wie das Brett gedreht ist) —
            # entscheiden muss die Schach-Semantik: In der falschen
            # Deutung erscheinen die Bauern absurd weit vorgerückt
            # (weiße auf Reihe 7 statt nahe Reihe 2). Die Deutung mit
            # der kleineren Bauern-Vorrück-Summe ist die richtige.
            def pawn_advance(b: chess.Board) -> int:
                total = 0
                for sq in b.pieces(chess.PAWN, chess.WHITE):
                    total += chess.square_rank(sq) - 1
                for sq in b.pieces(chess.PAWN, chess.BLACK):
                    total += 6 - chess.square_rank(sq)
                return total

            best = None
            for flipped in (False, True):
                self.flipped = flipped
                res = recognize_position(cells, self)
                if res is None:
                    continue
                board, uncertain = res
                plaus = pawn_advance(board)
                if best is None or plaus < best[0]:
                    best = (plaus, flipped, board, uncertain)
            if best is None:
                return False
            _plaus, self.flipped, self.detected_board, \
                self.turn_uncertain = best
        self.calibrated = True
        self.margin = gap + 2.0 * vgap
        return True

    def classify(self, cells: List[List[np.ndarray]]) -> Grid:
        """Belegung des aktuellen Frames (normalisiert: Weiß unten)."""
        rows = []
        for r in range(8):
            row = []
            for c in range(8):
                ratio, val = _cell_features(cells[r][c])
                if ratio < self.presence_thr:
                    row.append(EMPTY)
                elif (val > self.color_thr) == getattr(
                        self, "_white_is_bright", True):
                    row.append(WHITE_PC)
                else:
                    row.append(BLACK_PC)
            rows.append(tuple(row))
        grid: Grid = tuple(rows)
        return flip_grid(grid) if self.flipped else grid


# ------------------------------------------------------------ Zug-Ableitung

def find_move(board: chess.Board, occ: Grid) -> Optional[chess.Move]:
    """Der legale Zug, der exakt dieses Belegungsmuster erzeugt (o. None).

    Umwandlungen sind belegungsgleich → Dame wird bevorzugt.
    """
    matches: List[chess.Move] = []
    for mv in board.legal_moves:
        b = board.copy(stack=False)
        b.push(mv)
        if occupancy_from_board(b) == occ:
            matches.append(mv)
    if not matches:
        return None
    for mv in matches:
        if mv.promotion in (None, chess.QUEEN):
            return mv
    return matches[0]


def find_line_multi(roots: List[chess.Board], occ: Grid,
                    max_plies: int = 3
                    ) -> Optional[Tuple[int, List[chess.Move]]]:
    """Wie find_line, aber über mehrere Start-Hypothesen gleichzeitig.

    Alle Wurzeln werden TIEFENWEISE GEMEINSAM expandiert — die kürzeste
    Erklärung gewinnt, egal aus welcher Hypothese sie stammt. Das ist
    entscheidend bei unsicherer Zugseite: Aus der falschen Seite lässt
    sich fast jede Stellung per Figurenpendel (Springer raus und zurück
    = Null-Zug) in Tiefe 3 "wegerklären" — der echte Einzelzug aus der
    richtigen Seite hat aber Tiefe 1 und wird deshalb zuerst gefunden.
    Rückgabe: (Wurzel-Index, Zugfolge) oder None (nichts/mehrdeutig).
    """
    def moves_of(b: chess.Board):
        for mv in b.legal_moves:
            if mv.promotion in (None, chess.QUEEN):
                yield mv

    frontier = [(i, root, []) for i, root in enumerate(roots)]
    for _depth in range(max_plies):
        nxt = []
        hits = []
        for i, b, line in frontier:
            for mv in moves_of(b):
                nb = b.copy(stack=False)
                nb.push(mv)
                nl = line + [mv]
                if occupancy_from_board(nb) == occ:
                    hits.append((i, nl, nb.board_fen() + " " +
                                 ("w" if nb.turn else "b")))
                else:
                    nxt.append((i, nb, nl))
        if hits:
            fens = {f for _i, _l, f in hits}
            if len(fens) == 1:
                return hits[0][0], hits[0][1]
            return None                     # mehrdeutig → lieber nichts tun
        frontier = nxt
    return None


def find_line(board: chess.Board, occ: Grid, max_plies: int = 3
              ) -> Optional[List[chess.Move]]:
    """Kürzeste legale Zugfolge (bis max_plies), die zur Belegung führt.

    Rettet verpasste Züge (z. B. zwei schnelle Blitz-Züge innerhalb der
    Stabilitäts-Frist oder ein kurzer Overlay über dem Brett): Statt
    aufzugeben wird gesucht, welche Zugfolge von der bekannten Stellung
    zum beobachteten Muster führt. Eindeutig ist das Ergebnis nur, wenn
    alle Treffer derselben Tiefe in derselben Stellung münden
    (Zugumstellungen sind dann egal); sonst None. Umwandlungen: nur die
    Dame wird betrachtet (konsistent zur Einzelzug-Erkennung).
    """
    res = find_line_multi([board], occ, max_plies=max_plies)
    return res[1] if res is not None else None


@dataclass
class Recognizer:
    """Stabilitäts-Gate + Zustand: Frames rein, erkannte Züge raus.

    turn_uncertain: Bei automatisch erkannten Mittelspiel-Stellungen ist
    aus dem Einzelbild nicht ablesbar, wer am Zug ist. Dann wird beim
    ersten erkannten Zug notfalls die Zugseite umgeschaltet (wenn nur die
    andere Seite eine passende legale Fortsetzung hat) — danach ist die
    Seite sicher.
    """
    board: chess.Board = field(default_factory=chess.Board)
    stable_needed: int = STABLE_FRAMES
    turn_uncertain: bool = False
    _last: Optional[Grid] = None
    _count: int = 0
    desync: bool = False

    def feed(self, grid: Grid) -> List[chess.Move]:
        """Liefert die neu erkannten Züge (meist 0 oder 1; nach einer
        Aufhol-Suche auch mehrere auf einmal). Leere Liste = nichts."""
        if grid == self._last:
            self._count += 1
        else:
            self._last = grid
            self._count = 1
        if self._count != self.stable_needed:
            return []                        # noch nicht stabil / schon verarbeitet
        if grid == occupancy_from_board(self.board):
            self.desync = False
            return []                        # unverändert
        roots = [self.board]
        if self.turn_uncertain:
            other = self.board.copy(stack=False)
            other.turn = not other.turn
            other.ep_square = None
            roots.append(other)
        res = find_line_multi(roots, grid, max_plies=3)
        if res is None:
            # Bei unsicherer Zugseite kann ein einzelner Frame auch
            # schlicht (noch) mehrdeutig sein — dann still weiter
            # warten statt Alarm zu schlagen.
            self.desync = not self.turn_uncertain
            return []
        idx, line = res
        if idx == 1:
            self.board = roots[1]            # Zugseite war falsch geraten
        self.turn_uncertain = False
        for mv in line:
            self.board.push(mv)
        self.desync = False
        return line


def _grid_edge_score(gx, gy, rect):
    """Kantenenergie entlang der 7+7 inneren Gitterlinien des Kandidaten.

    Das Schachbrettmuster erzeugt an jeder inneren Feldgrenze eine
    durchgehende Kante — liegt das Raster richtig, ist die mittlere
    Gradientenstärke entlang dieser Linien maximal. Figuren erzeugen
    zwar auch Kanten, aber keine über die ganze Brettbreite
    durchgehenden.
    """
    x, y, w, h = rect
    H, W = gx.shape
    if x < 1 or y < 1 or x + w >= W or y + h >= H:
        return -1.0
    xs = np.array([x + k * w // 8 for k in range(1, 8)])
    ys = np.array([y + k * h // 8 for k in range(1, 8)])
    v = float(gx[y:y + h, xs - 1].mean())
    hz = float(gy[ys - 1, x:x + w].mean())
    return v + hz


def calibrate_search(frame, rect, expected=None, max_shift=8,
                     max_scale=8):
    """Kalibrieren mit automatischer Rechteck-Nachjustierung.

    Handgezogene Rechtecke liegen fast nie pixelgenau auf dem Brett —
    schon wenige Pixel Versatz lassen Figurkanten in die Randzonen der
    Nachbarzellen bluten, was die (bewusst strenge) Kalibrierung kippt.
    Deshalb: Alle kleinen Verschiebungs-/Größenvarianten werden zuerst
    über die Kantenenergie entlang der Gitterlinien bewertet (billig,
    Gradienten werden nur einmal berechnet); nur die besten Kandidaten
    durchlaufen die volle Kalibrierung.

    Rückgabe: (Klassifikator, nachjustiertes Rechteck) oder (None, None).
    """
    if frame.ndim == 3:
        gray = frame.astype(np.float32).mean(axis=2)
    else:
        gray = frame.astype(np.float32)
    gx = np.pad(np.abs(np.diff(gray, axis=1)), ((0, 0), (0, 1)))
    gy = np.pad(np.abs(np.diff(gray, axis=0)), ((0, 1), (0, 0)))

    x, y, w, h = rect
    scored = []
    for ds in range(-max_scale, max_scale + 1):
        for dx in range(-max_shift, max_shift + 1):
            for dy in range(-max_shift, max_shift + 1):
                cand = (x + dx, y + dy, w + ds, h + ds)
                scored.append((_grid_edge_score(gx, gy, cand), cand))
    scored.sort(key=lambda t: -t[0])

    for _score, cand in scored[:8]:
        clf = CellClassifier()
        cells = split_cells(frame, cand)
        if expected is not None:
            if clf.calibrate(cells, expected.copy()):
                return clf, cand
        elif clf.calibrate_auto(cells):
            return clf, cand
    return None, None


# ------------------------------------------------- Figurtyp-Erkennung

_TEMPLATE_SIZE = 32
_templates = None


def _resize_mask(mask: np.ndarray, size: int) -> np.ndarray:
    """Nearest-Neighbor-Resize einer Binärmaske (reines numpy)."""
    h, w = mask.shape
    if h == 0 or w == 0:
        return np.zeros((size, size), dtype=bool)
    ys = (np.arange(size) * h // size).clip(0, h - 1)
    xs = (np.arange(size) * w // size).clip(0, w - 1)
    return mask[np.ix_(ys, xs)]


def _bbox_norm(mask: np.ndarray) -> np.ndarray:
    """Maske auf ihre Bounding-Box beschneiden und normieren."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return np.zeros((_TEMPLATE_SIZE, _TEMPLATE_SIZE), dtype=bool)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return _resize_mask(crop, _TEMPLATE_SIZE)


def _piece_templates():
    """Silhouetten-Schablonen der 6 Figurtypen aus den eingebetteten
    Cburnett-PNGs (Alpha-Kanal). Silhouetten sind über Figurensätze
    hinweg konventionell — deshalb funktioniert der Abgleich auch bei
    fremden (2D-)Sätzen, solange sie klassische Formen verwenden.
    """
    global _templates
    if _templates is None:
        import base64
        import io
        from PIL import Image
        from pieces import PIECE_PNG_B64
        _templates = {}
        for sym in "PNBRQK":
            img = Image.open(io.BytesIO(
                base64.b64decode(PIECE_PNG_B64["w" + sym])))
            alpha = np.array(img.convert("RGBA"))[:, :, 3] > 128
            _templates[sym] = _bbox_norm(alpha)
    return _templates


def _cell_piece_mask(cell: np.ndarray) -> np.ndarray:
    """Silhouette der Figur in einer Zelle (Vorder- vs. Hintergrund)."""
    if cell.ndim == 3:
        gray = cell.astype(np.float32).mean(axis=2)
    else:
        gray = cell.astype(np.float32)
    h, w = gray.shape
    m = max(1, int(min(h, w) * 0.08))
    border = np.concatenate([gray[:m].ravel(), gray[-m:].ravel(),
                             gray[:, :m].ravel(), gray[:, -m:].ravel()])
    bg = float(np.median(border))
    return np.abs(gray - bg) > 14.0


def classify_piece_type(cell: np.ndarray) -> Tuple[str, float]:
    """Bester Figurtyp (Symbol) + IoU-Score für eine besetzte Zelle."""
    mask = _bbox_norm(_cell_piece_mask(cell))
    best_sym, best_iou = "P", -1.0
    for sym, tpl in _piece_templates().items():
        inter = np.logical_and(mask, tpl).sum()
        union = np.logical_or(mask, tpl).sum()
        iou = inter / union if union else 0.0
        if iou > best_iou:
            best_sym, best_iou = sym, float(iou)
    return best_sym, best_iou


_color_refs = None


def _color_reference_table():
    """Typbewusste Farb-Referenzen aus den eingebetteten Sprites.

    Jede Figurtype hat einen charakteristischen Hell-Anteil — die weiße
    Dame ist wegen ihrer vielen Binnenlinien z. B. deutlich "dunkler"
    als ein weißer Bauer. Ein globaler Schwellwert schneidet solche
    Ausreißer ab; der Vergleich gegen die Type-eigene Weiß- und
    Schwarz-Referenz nicht. Referenzen werden für helle und dunkle
    Feldfarben getrennt berechnet (der Hell-Anteil hängt vom
    Hintergrund ab, weil Figurkörper nahe der Feldfarbe aus dem
    Toleranzband fallen).
    """
    global _color_refs
    if _color_refs is None:
        import base64
        import io
        from PIL import Image
        from pieces import PIECE_PNG_B64
        _color_refs = {}
        for sym in "PNBRQK":
            for color_key in "wb":
                img = Image.open(io.BytesIO(base64.b64decode(
                    PIECE_PNG_B64[color_key + sym]))).convert("RGBA")
                img = img.resize((48, 48), Image.LANCZOS)
                for bg_level in (140, 210):
                    bg = Image.new("RGB", (48, 48),
                                   (bg_level,) * 3)
                    bg.paste(img, (0, 0), img)
                    _ratio, val = _cell_features(np.array(bg))
                    _color_refs[(color_key, sym, bg_level)] = val
    return _color_refs


def classify_color_by_type(cell: np.ndarray, sym: str) -> bool:
    """Figurfarbe (True = Weiß) über die Type-eigene Referenz."""
    refs = _color_reference_table()
    if cell.ndim == 3:
        gray = cell.astype(np.float32).mean(axis=2)
    else:
        gray = cell.astype(np.float32)
    h, w = gray.shape
    m = max(1, int(min(h, w) * 0.12))
    border = np.concatenate([gray[:m].ravel(), gray[-m:].ravel(),
                             gray[:, :m].ravel(), gray[:, -m:].ravel()])
    bg = float(np.median(border))
    bg_level = 140 if abs(bg - 140) < abs(bg - 210) else 210
    _ratio, val = _cell_features(cell)
    dw = abs(val - refs[("w", sym, bg_level)])
    db = abs(val - refs[("b", sym, bg_level)])
    return dw <= db


def _guess_castling(board: chess.Board) -> None:
    """Rochaderechte großzügig raten: erlaubt, wo König und Turm auf den
    Grundfeldern stehen (Standard-Heuristik jeder GUI beim FEN-Import)."""
    fen = ""
    if board.piece_at(chess.E1) == chess.Piece(chess.KING, chess.WHITE):
        if board.piece_at(chess.H1) == chess.Piece(chess.ROOK, chess.WHITE):
            fen += "K"
        if board.piece_at(chess.A1) == chess.Piece(chess.ROOK, chess.WHITE):
            fen += "Q"
    if board.piece_at(chess.E8) == chess.Piece(chess.KING, chess.BLACK):
        if board.piece_at(chess.H8) == chess.Piece(chess.ROOK, chess.BLACK):
            fen += "k"
        if board.piece_at(chess.A8) == chess.Piece(chess.ROOK, chess.BLACK):
            fen += "q"
    board.set_castling_fen(fen or "-")


def recognize_position(cells, classifier: "CellClassifier"
                       ) -> Optional[Tuple[chess.Board, bool]]:
    """Komplette Stellung (inkl. Figurtypen) aus einem Standbild.

    Voraussetzung: Der Klassifikator hat bereits Schwellwerte (Belegung/
    Farbe). Rückgabe: (Stellung, zugseite_unsicher) oder None. Die
    Zugseite ist aus einem Einzelbild nicht ablesbar — sie wird über die
    Stellungs-Gültigkeit bestimmt (steht die Nicht-Zugseite im Schach,
    scheidet diese Deutung aus); bleiben beide möglich, ist sie
    "unsicher" und wird vom Recognizer beim ersten Zug korrigiert.
    Rochaderechte: Grundstellungs-Heuristik. En passant: keines.
    """
    grid = classifier.classify(cells)
    board = chess.Board.empty()
    scores = {}
    for r in range(8):
        for c in range(8):
            if grid[r][c] == EMPTY:
                continue
            # Zellkoordinaten im BILD aus Brettkoordinaten zurückrechnen
            br, bc = (7 - r, 7 - c) if classifier.flipped else (r, c)
            sym, iou = classify_piece_type(cells[br][bc])
            # Farbentscheidung typbewusst: der grobe Cluster-Schwellwert
            # kann zeichnungsreiche weiße Figuren (v. a. die Dame) in
            # spärlichen Stellungen falsch einfärben.
            is_white = classify_color_by_type(cells[br][bc], sym)
            color = chess.WHITE if is_white else chess.BLACK
            sq = chess.square(c, 7 - r)
            board.set_piece_at(sq, chess.Piece(
                chess.Piece.from_symbol(sym).piece_type, color))
            scores[sq] = (sym, iou)
    # Aufräumen: genau ein König je Farbe; Bauern nicht auf Grundreihen.
    for color in (chess.WHITE, chess.BLACK):
        kings = [sq for sq in board.pieces(chess.KING, color)]
        if len(kings) == 0:
            cands = [(scores[sq][1], sq) for sq in scores
                     if board.piece_at(sq).color == color]
            if not cands:
                return None
            # Zelle mit der schlechtesten Passung zum bisherigen Typ
            # ist der beste König-Kandidat.
            sq = min(cands)[1]
            board.set_piece_at(sq, chess.Piece(chess.KING, color))
        elif len(kings) > 1:
            keep = max(kings, key=lambda sq: scores[sq][1])
            for sq in kings:
                if sq != keep:
                    board.set_piece_at(sq, chess.Piece(chess.QUEEN, color))
    for sq in list(board.piece_map()):
        p = board.piece_at(sq)
        if p.piece_type == chess.PAWN and chess.square_rank(sq) in (0, 7):
            board.set_piece_at(sq, chess.Piece(chess.QUEEN, p.color))
    _guess_castling(board)
    valid = []
    for turn in (chess.WHITE, chess.BLACK):
        b = board.copy(stack=False)
        b.turn = turn
        if b.is_valid():
            valid.append(b)
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0], False
    return valid[0], True                    # beide möglich → Weiß raten
