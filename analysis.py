# -*- coding: utf-8 -*-
"""Zugbewertung, Fehlerklassifikation und deutschsprachige Erklärungen.

Kernidee: Für jede Stellung liefert die Engine (Bewertung, Hauptvariante).
Der Qualitätsverlust eines Zuges wird als Differenz der Gewinnwahrscheinlichkeit
(Win-%-Modell wie bei Lichess) gemessen, nicht als roher Centipawn-Wert —
das verhält sich in klar gewonnenen/verlorenen Stellungen deutlich robuster.
"""

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import chess
import chess.engine

MATE_CP = 10000

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                chess.ROOK: 5, chess.QUEEN: 9}

PIECE_NAMES_DE = {chess.PAWN: "Bauer", chess.KNIGHT: "Springer",
                  chess.BISHOP: "Läufer", chess.ROOK: "Turm",
                  chess.QUEEN: "Dame", chess.KING: "König"}

JUDGE_NAMES = {"??": "Patzer", "?": "Fehler", "?!": "Ungenauigkeit"}


# ---------------------------------------------------------------- Score

@dataclass
class ScoreLite:
    """Engine-Bewertung aus Weiß-Sicht, serialisierbar (für den Cache)."""
    cp_white: int = 0
    mate_white: Optional[int] = None   # +n: Weiß setzt matt in n; -n: Schwarz

    @classmethod
    def from_pov_score(cls, pov: "chess.engine.PovScore") -> "ScoreLite":
        w = pov.white()
        return cls(cp_white=int(w.score(mate_score=MATE_CP)), mate_white=w.mate())

    def cp(self, color: bool) -> int:
        return self.cp_white if color == chess.WHITE else -self.cp_white

    def mate(self, color: bool) -> Optional[int]:
        if self.mate_white is None:
            return None
        return self.mate_white if color == chess.WHITE else -self.mate_white

    def fmt(self, color: bool = chess.WHITE) -> str:
        m = self.mate(color)
        if m is not None:
            return f"#{m}" if m > 0 else f"#-{-m}"
        cp = self.cp(color)
        if abs(cp) >= MATE_CP - 100:
            return "Matt" if cp > 0 else "Matt gegen sich"
        return f"{cp / 100:+.2f}"


@dataclass
class NormInfo:
    """Normalisiertes Analyse-Ergebnis einer Stellung."""
    score: ScoreLite
    pv: List[chess.Move] = field(default_factory=list)


def normalize_info(info: Dict[str, Any]) -> NormInfo:
    raw = info.get("score")
    score = ScoreLite.from_pov_score(raw) if raw is not None else ScoreLite(0)
    return NormInfo(score=score, pv=list(info.get("pv", [])))


def terminal_score(board: chess.Board) -> Optional[ScoreLite]:
    """Bewertung für Endstellungen ohne Engine (Matt/Patt usw.)."""
    if not board.is_game_over():
        return None
    if board.is_checkmate():
        # Die Seite am Zug ist matt gesetzt.
        cp = -MATE_CP if board.turn == chess.WHITE else MATE_CP
        return ScoreLite(cp_white=cp)
    return ScoreLite(cp_white=0)


# ---------------------------------------------------------------- Win-%

def win_pct(cp: int) -> float:
    cp = max(-1200, min(1200, cp))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def classify(drop_wp: float) -> Optional[str]:
    if drop_wp >= 30.0:
        return "??"
    if drop_wp >= 20.0:
        return "?"
    if drop_wp >= 10.0:
        return "?!"
    return None


# ---------------------------------------------------------------- Helfer

def san_line(board: chess.Board, pv: List[chess.Move], max_plies: int = 8) -> str:
    """Robuste SAN-Darstellung einer Variante (bricht bei illegalen Zügen ab)."""
    b = board.copy()
    parts: List[str] = []
    for mv in pv[:max_plies]:
        if not b.is_legal(mv):
            break
        if b.turn == chess.WHITE:
            parts.append(f"{b.fullmove_number}.")
        elif not parts:
            parts.append(f"{b.fullmove_number}...")
        parts.append(b.san(mv))
        b.push(mv)
    return " ".join(parts)


def phase_of(board: chess.Board) -> str:
    """Partiephase der Stellung *vor* dem Zug."""
    if board.ply() < 16:
        return "Eröffnung"
    pieces = 0
    for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        pieces += len(board.pieces(pt, chess.WHITE))
        pieces += len(board.pieces(pt, chess.BLACK))
    return "Endspiel" if pieces <= 6 else "Mittelspiel"


def outcome_text(board: chess.Board) -> str:
    oc = board.outcome(claim_draw=True)
    if oc is None:
        return ""
    if oc.winner is None:
        reasons = {
            chess.Termination.STALEMATE: "Patt",
            chess.Termination.INSUFFICIENT_MATERIAL: "ungenügendes Material",
            chess.Termination.THREEFOLD_REPETITION: "dreifache Stellungswiederholung",
            chess.Termination.FIVEFOLD_REPETITION: "fünffache Stellungswiederholung",
            chess.Termination.FIFTY_MOVES: "50-Züge-Regel",
            chess.Termination.SEVENTYFIVE_MOVES: "75-Züge-Regel",
        }
        return f"Remis ({reasons.get(oc.termination, 'Remis')})"
    winner = "Weiß" if oc.winner == chess.WHITE else "Schwarz"
    return f"{winner} gewinnt durch Schachmatt"


# ---------------------------------------------------------------- Urteil

@dataclass
class MoveJudgement:
    ply: int
    mover_white: bool
    san: str
    uci: str
    phase: str
    score_before: ScoreLite
    score_after: ScoreLite
    drop_wp: float
    judgement: Optional[str]
    best_san: str = ""
    best_line: str = ""
    refut_line: str = ""
    motifs: List[Dict[str, str]] = field(default_factory=list)

    def cp_loss(self) -> int:
        pov = chess.WHITE if self.mover_white else chess.BLACK
        return max(0, min(1000, self.score_before.cp(pov) - self.score_after.cp(pov)))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MoveJudgement":
        d = dict(d)
        d["score_before"] = ScoreLite(**d["score_before"])
        d["score_after"] = ScoreLite(**d["score_after"])
        return cls(**d)


def judge_move(board_before: chess.Board, move: chess.Move,
               info_before: NormInfo, info_after: NormInfo) -> MoveJudgement:
    """Beurteilt einen gespielten Zug anhand der Analysen davor/danach."""
    mover = board_before.turn
    san = board_before.san(move)
    board_after = board_before.copy()
    board_after.push(move)

    cp_b = info_before.score.cp(mover)
    cp_a = info_after.score.cp(mover)
    drop = max(0.0, win_pct(cp_b) - win_pct(cp_a))

    best_pv = info_before.pv
    best_move = best_pv[0] if best_pv else None
    judgement = classify(drop)
    if best_move is not None and move == best_move:
        judgement = None   # der beste Zug wird nie bestraft (Analyse-Rauschen)

    best_san = ""
    best_line = ""
    if best_move is not None and board_before.is_legal(best_move):
        best_san = board_before.san(best_move)
        best_line = san_line(board_before, best_pv, 8)
    refut_line = san_line(board_after, info_after.pv, 6) if info_after.pv else ""

    motifs: List[Dict[str, str]] = []
    if judgement:
        motifs = detect_motifs(board_before, move, board_after,
                               info_before, info_after, mover)

    return MoveJudgement(
        ply=board_before.ply(), mover_white=(mover == chess.WHITE),
        san=san, uci=move.uci(), phase=phase_of(board_before),
        score_before=info_before.score, score_after=info_after.score,
        drop_wp=drop, judgement=judgement,
        best_san=best_san, best_line=best_line,
        refut_line=refut_line, motifs=motifs)


def detect_motifs(board_before: chess.Board, move: chess.Move,
                  board_after: chess.Board,
                  info_before: NormInfo, info_after: NormInfo,
                  mover: bool) -> List[Dict[str, str]]:
    """Heuristische Erklärung, *warum* der Zug schlecht war."""
    out: List[Dict[str, str]] = []
    best_move = info_before.pv[0] if info_before.pv else None

    # Matt übersehen
    m_best = info_before.score.mate(mover)
    if m_best is not None and m_best > 0 and (best_move is None or move != best_move):
        out.append({
            "code": "matt_verpasst",
            "text": (f"Du hattest Matt in {m_best}: "
                     f"{san_line(board_before, info_before.pv, 2 * m_best)}"),
        })

    # Matt zugelassen
    m_after = info_after.score.mate(mover)
    if m_after is not None and m_after < 0:
        out.append({
            "code": "matt_zugelassen",
            "text": (f"Der Zug lässt Matt in {-m_after} zu: "
                     f"{san_line(board_after, info_after.pv, 2 * (-m_after))}"),
        })

    loss_cp = info_before.score.cp(mover) - info_after.score.cp(mover)
    ref_pv = info_after.pv

    if ref_pv and board_after.is_legal(ref_pv[0]):
        r0 = ref_pv[0]
        r0_san = board_after.san(r0)
        is_cap = board_after.is_capture(r0)

        if m_after is None and is_cap and loss_cp >= 150:
            if r0.to_square == move.to_square:
                piece = board_after.piece_at(move.to_square)
                pname = PIECE_NAMES_DE.get(piece.piece_type, "Figur") if piece else "Figur"
                out.append({
                    "code": "figur_haengt",
                    "text": (f"Die gezogene Figur ({pname} auf "
                             f"{chess.square_name(move.to_square)}) kann geschlagen "
                             f"werden: {r0_san}."),
                })
            else:
                if loss_cp >= 1500:
                    txt = (f"Nach {r0_san} geht entscheidend Material "
                           f"verloren (oder es droht Matt).")
                else:
                    txt = (f"Nach {r0_san} geht Material verloren "
                           f"(≈ {loss_cp / 100:.1f} Bauerneinheiten).")
                out.append({"code": "material_verlust", "text": txt})

        # Gabel / Doppelangriff durch die Widerlegung
        b2 = board_after.copy()
        b2.push(r0)
        if b2.piece_at(r0.to_square) is not None:
            targets = []
            for sq in b2.attacks(r0.to_square):
                p = b2.piece_at(sq)
                if (p and p.color == mover and p.piece_type != chess.KING
                        and PIECE_VALUES.get(p.piece_type, 0) >= 3):
                    targets.append(p)
            gives_check = b2.is_check()
            if len(targets) + (1 if gives_check else 0) >= 2 and loss_cp >= 120:
                names = " und ".join(
                    PIECE_NAMES_DE[p.piece_type] for p in targets[:2])
                extra = " (mit Schach)" if gives_check else ""
                out.append({
                    "code": "gabel",
                    "text": (f"{r0_san} ist ein Doppelangriff{extra} – "
                             f"bedroht {names}."),
                })

    # Verpasste Taktik
    if (best_move is not None and move != best_move and m_best is None
            and board_before.is_legal(best_move)):
        if info_before.score.cp(mover) >= 200 and info_after.score.cp(mover) <= 60:
            if board_before.is_capture(best_move):
                hint = "Schlagzug"
            elif board_before.gives_check(best_move):
                hint = "Schachgebot"
            else:
                hint = "starken Zug"
            out.append({
                "code": "taktik_verpasst",
                "text": (f"Es gab einen {hint}: "
                         f"{san_line(board_before, info_before.pv, 6)}"),
            })

    return out


def feedback_text(mj: MoveJudgement) -> str:
    """Kompakter deutschsprachiger Feedback-Text zu einem Zug."""
    pov = chess.WHITE if mj.mover_white else chess.BLACK
    lines: List[str] = []
    if mj.judgement:
        lines.append(f"{mj.san}{mj.judgement} – {JUDGE_NAMES[mj.judgement]}   "
                     f"({mj.score_before.fmt(pov)} → {mj.score_after.fmt(pov)})")
        for m in mj.motifs:
            lines.append("• " + m["text"])
        if mj.best_san:
            lines.append(f"Besser war {mj.best_san}: {mj.best_line}")
        if mj.refut_line:
            lines.append(f"Mögliche Fortsetzung des Gegners: {mj.refut_line}")
    else:
        if mj.best_san and mj.san == mj.best_san:
            lines.append(f"{mj.san} ✓ bester Zug   ({mj.score_after.fmt(pov)})")
        elif mj.drop_wp < 4.0:
            lines.append(f"{mj.san} – guter Zug   ({mj.score_after.fmt(pov)})")
        else:
            alt = f" Am stärksten war {mj.best_san}." if mj.best_san else ""
            lines.append(f"{mj.san} – okay ({mj.score_after.fmt(pov)}).{alt}")
    return "\n".join(lines)
