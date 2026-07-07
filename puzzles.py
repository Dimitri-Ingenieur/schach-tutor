# -*- coding: utf-8 -*-
"""Rätsel aus eigenen Fehlern: Erzeugung, Deck-Verwaltung und Wiederholung.

Idee (wie die Taktikrätsel auf chess.com/Lichess, aber aus *deinen* Partien):
  * Jede Stellung, in der du einen Fehler (?/??) gemacht oder eine Taktik
    verpasst hast, ist ein Rätsel-Kandidat: „Finde den Zug, den du damals
    nicht gesehen hast."
  * Ein Kandidat wird nur zum Rätsel, wenn die Lösung *eindeutig* ist:
    Die Stellung wird mit multipv=2 nachgerechnet; der beste Zug muss klar
    besser sein als der zweitbeste (Win-%-Abstand), sonst wäre das Rätsel
    mehrdeutig. Mattführungen werden über mehrere Züge verfolgt.
  * Gelöste Rätsel wandern in einer Leitner-Box nach oben (Wiederholung nach
    1/3/7/21 Tagen), Fehlversuche setzen sie zurück – so verschwinden die
    Schwächen erst aus dem Deck, wenn du sie wirklich beherrschst.
"""

import hashlib
import json
import os
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import chess
import chess.engine

from analysis import NormInfo, normalize_info, win_pct
from opponent import MOTIF_LABELS, GameRecord

# Wiederholungsintervalle je Leitner-Box (in Tagen)
LEITNER_DAYS = [0, 1, 3, 7, 21]

THEME_LABELS = dict(MOTIF_LABELS)
THEME_LABELS.update({
    "mattfuehrung": "Mattführung",
    "verteidigung": "Beste Verteidigung finden",
    "sonstiges": "Sonstige Taktik",
})

# Erst ab diesem Win-%-Abstand zwischen bestem und zweitbestem Zug gilt die
# Lösung als eindeutig.
UNIQUENESS_GAP_WP = 12.0
# Toleranz, mit der eine Alternativlösung des Nutzers akzeptiert wird.
ALT_TOLERANCE_WP = 6.0


# ---------------------------------------------------------------- Datentyp

@dataclass
class Puzzle:
    pid: str
    fen: str
    solution: List[str]          # UCI; Nutzerzüge und Engine-Antworten im Wechsel
    targets: List[float]         # erwartetes Win-% (Löser-Sicht) nach jedem Nutzerzug
    themes: List[str]
    source: Dict[str, str]      # white/black/date/move_no/played_san/judgement
    swing: float                 # Win-%-Verlust des Original-Fehlers
    mate: bool = False           # Lösung endet mit Matt
    created: str = ""
    # Leitner / Statistik
    box: int = 0
    due: str = ""
    attempts: int = 0
    correct: int = 0
    streak: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Puzzle":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)

    def user_move_count(self) -> int:
        return (len(self.solution) + 1) // 2

    def solver_color(self) -> bool:
        return chess.Board(self.fen).turn


def _pid(fen: str, first_move: str) -> str:
    return hashlib.md5(f"{fen}|{first_move}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- Deck

class PuzzleDB:
    """JSON-basiertes Rätsel-Deck mit Leitner-Wiederholung."""

    def __init__(self, path: str):
        self.path = path
        self.puzzles: List[Puzzle] = []
        self.load()

    # ------------------------------------------------ Persistenz

    def load(self) -> None:
        self.puzzles = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("puzzles", []):
                try:
                    self.puzzles.append(Puzzle.from_dict(d))
                except TypeError:
                    continue
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"puzzles": [p.to_dict() for p in self.puzzles]},
                          f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ------------------------------------------------ Verwaltung

    def add(self, new: List[Puzzle]) -> int:
        known = {p.pid for p in self.puzzles}
        added = 0
        for p in new:
            if p.pid in known:
                continue
            known.add(p.pid)
            self.puzzles.append(p)
            added += 1
        return added

    def themes_in_deck(self) -> List[str]:
        seen: Dict[str, int] = {}
        for p in self.puzzles:
            for t in p.themes:
                seen[t] = seen.get(t, 0) + 1
        return sorted(seen, key=lambda t: -seen[t])

    def due(self, theme: Optional[str] = None,
            today: Optional[date] = None,
            include_not_due: bool = False,
            kind: Optional[str] = None) -> List[Puzzle]:
        today = today or date.today()
        pool = []
        for p in self.puzzles:
            if theme and theme not in p.themes:
                continue
            if kind and p.source.get("kind", "eigener_fehler") != kind:
                continue
            if not include_not_due:
                due_day = _parse_day(p.due) or today
                if due_day > today:
                    continue
            pool.append(p)
        # Schwächste zuerst (niedrige Box), innerhalb der Box gemischt.
        random.shuffle(pool)
        pool.sort(key=lambda p: (p.box, p.due))
        return pool

    def next_due_date(self, theme: Optional[str] = None,
                      kind: Optional[str] = None) -> Optional[date]:
        days = [_parse_day(p.due) for p in self.puzzles
                if (not theme or theme in p.themes)
                and (not kind or p.source.get("kind",
                                              "eigener_fehler") == kind)]
        days = [d for d in days if d is not None]
        return min(days) if days else None

    def apply_result(self, puzzle: Puzzle, solved: bool,
                     today: Optional[date] = None) -> None:
        today = today or date.today()
        puzzle.attempts += 1
        if solved:
            puzzle.correct += 1
            puzzle.streak += 1
            puzzle.box = min(puzzle.box + 1, len(LEITNER_DAYS) - 1)
        else:
            puzzle.streak = 0
            puzzle.box = 0
        interval = LEITNER_DAYS[puzzle.box]
        puzzle.due = (today + timedelta(days=interval)).isoformat()
        self.save()

    def theme_stats(self) -> List[Tuple[str, int, int, int]]:
        """[(theme, deck_n, attempts, correct)], schwächste Themen zuerst."""
        agg: Dict[str, List[int]] = {}
        for p in self.puzzles:
            for t in p.themes:
                a = agg.setdefault(t, [0, 0, 0])
                a[0] += 1
                a[1] += p.attempts
                a[2] += p.correct
        rows = [(t, n, at, co) for t, (n, at, co) in agg.items()]
        rows.sort(key=lambda r: (r[3] / r[2] if r[2] else 0.0, -r[1]))
        return rows

    def mastered_count(self) -> int:
        return sum(1 for p in self.puzzles
                   if p.box >= len(LEITNER_DAYS) - 1 and p.streak >= 1)


def _parse_day(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Erzeugung

def _analyse2(engine: chess.engine.SimpleEngine, board: chess.Board,
              nodes: int, multipv: int = 2) -> List[NormInfo]:
    raw = engine.analyse(board, chess.engine.Limit(nodes=nodes),
                         multipv=multipv)
    if isinstance(raw, dict):
        raw = [raw]
    return [normalize_info(i) for i in raw]


def _is_unique(best: NormInfo, second: Optional[NormInfo],
               solver: bool) -> bool:
    if second is None or not second.pv:
        return True
    wp_gap = (win_pct(best.score.cp(solver))
              - win_pct(second.score.cp(solver)))
    if wp_gap >= UNIQUENESS_GAP_WP:
        return True
    # Mattführung: eindeutig, wenn nur der beste Zug (schnellstes) Matt hält.
    m1, m2 = best.score.mate(solver), second.score.mate(solver)
    if m1 is not None and m1 > 0 and (m2 is None or m2 <= 0 or m2 > m1):
        return True
    return False


def build_solution(engine: chess.engine.SimpleEngine, board0: chess.Board,
                   verify_nodes: int, max_user_moves: int = 3
                   ) -> Optional[Tuple[List[chess.Move], List[float], bool]]:
    """Verifiziert einen Kandidaten und baut die Lösungsvariante.

    Gibt (Zugfolge, Ziel-Win-% je Nutzerzug, endet_mit_Matt) zurück oder None,
    wenn die Stellung kein sauberes (eindeutiges) Rätsel hergibt.
    """
    b = board0.copy()
    solver = b.turn
    solution: List[chess.Move] = []
    targets: List[float] = []

    for k in range(max_user_moves):
        infos = _analyse2(engine, b, verify_nodes, multipv=2)
        best = infos[0]
        second = infos[1] if len(infos) > 1 else None
        if not best.pv or not b.is_legal(best.pv[0]):
            break
        best_wp = win_pct(best.score.cp(solver))
        unique = _is_unique(best, second, solver)

        if k == 0:
            if not unique:
                return None                    # mehrdeutig → kein Rätsel
            if best_wp < 40.0:
                return None                    # selbst die Lösung steht schlecht
        elif not unique:
            break                              # Fortsetzung mehrdeutig → hier enden

        move = best.pv[0]
        solution.append(move)
        targets.append(best_wp)
        b.push(move)
        if b.is_game_over():
            break

        # Weiterspielen nur bei Mattführung oder erzwingendem Schach,
        # sonst ist die Pointe mit diesem Zug erreicht.
        mate = best.score.mate(solver)
        forcing = (mate is not None and 0 < mate <= max_user_moves) or b.is_check()
        if not forcing:
            break

        reply = None
        if len(best.pv) > 1 and b.is_legal(best.pv[1]):
            reply = best.pv[1]
        else:
            r = _analyse2(engine, b, max(64, verify_nodes // 3), multipv=1)
            if r and r[0].pv and b.is_legal(r[0].pv[0]):
                reply = r[0].pv[0]
        if reply is None:
            break
        solution.append(reply)
        b.push(reply)
        if b.is_game_over():
            break

    if not solution:
        return None
    if len(solution) % 2 == 0:                 # muss mit Nutzerzug enden
        solution.pop()
        b.pop()
    return solution, targets, b.is_checkmate()


def generate_puzzles(engine: chess.engine.SimpleEngine,
                     records: List[GameRecord], player: str,
                     verify_nodes: int, cancel=None,
                     progress: Optional[Callable[[int, int, str], None]] = None
                     ) -> Optional[List[Puzzle]]:
    """Erzeugt Rätsel aus allen Fehlern des Spielers in den Partien.

    Gibt None zurück, wenn `cancel` gesetzt wurde.
    """
    puzzles: List[Puzzle] = []
    seen: set = set()
    today = date.today().isoformat()

    for gi, rec in enumerate(records):
        as_white = rec.headers.get("White", "") == player
        color = chess.WHITE if as_white else chess.BLACK
        opponent = rec.headers.get("Black" if as_white else "White", "?")

        board = rec.start_board()
        for i, uci in enumerate(rec.moves_uci):
            if cancel is not None and cancel.is_set():
                return None
            move = chess.Move.from_uci(uci)
            mj = rec.judgements[i] if i < len(rec.judgements) else None
            if mj is None or not board.is_legal(move):
                break

            wants = (mj.mover_white == (color == chess.WHITE)
                     and (mj.judgement in ("?", "??")
                          or any(m.get("code") in ("matt_verpasst",
                                                   "taktik_verpasst")
                                 for m in mj.motifs)))
            if wants:
                key = board.fen()
                if key not in seen:
                    seen.add(key)
                    if progress:
                        progress(gi + 1, len(records),
                                 f"prüfe Kandidat (Zug {mj.ply // 2 + 1})")
                    built = build_solution(engine, board, verify_nodes)
                    if built is not None:
                        sol, targets, mate = built
                        themes = []
                        for m in mj.motifs:
                            code = m.get("code")
                            if code and code not in themes:
                                themes.append(code)
                        if mate and "mattfuehrung" not in themes:
                            themes.insert(0, "mattfuehrung")
                        if targets and targets[0] < 55.0:
                            themes.append("verteidigung")
                        if not themes:
                            themes.append("sonstiges")
                        dots = "." if mj.mover_white else "…"
                        puzzles.append(Puzzle(
                            pid=_pid(key, sol[0].uci()),
                            fen=key,
                            solution=[m.uci() for m in sol],
                            targets=[round(t, 1) for t in targets],
                            themes=themes,
                            source={
                                "white": rec.headers.get("White", "?"),
                                "black": rec.headers.get("Black", "?"),
                                "date": rec.headers.get("Date", "?"),
                                "opponent": opponent,
                                "move_no": f"{mj.ply // 2 + 1}{dots}",
                                "played_san": mj.san,
                                "played_uci": mj.uci,
                                "judgement": mj.judgement or "",
                                "kind": "eigener_fehler",
                            },
                            swing=round(mj.drop_wp, 1),
                            mate=mate,
                            created=today,
                            due=today,
                        ))
            board.push(move)
        if progress:
            progress(gi + 1, len(records), "fertig")
    return puzzles


# ---------------------------------------------------------------- Lösen

def expected_reply(puzzle: Puzzle, step: int) -> Optional[chess.Move]:
    """Engine-Antwort nach dem step-ten Nutzerzug (falls vorhanden)."""
    idx = step * 2 + 1
    if idx < len(puzzle.solution):
        return chess.Move.from_uci(puzzle.solution[idx])
    return None


def solution_move(puzzle: Puzzle, step: int) -> Optional[chess.Move]:
    idx = step * 2
    if idx < len(puzzle.solution):
        return chess.Move.from_uci(puzzle.solution[idx])
    return None


def alt_move_ok(info_after: NormInfo, target_wp: float, solver: bool,
                tolerance: float = ALT_TOLERANCE_WP) -> bool:
    """Akzeptiert einen abweichenden Nutzerzug, wenn er (fast) gleich gut ist."""
    return win_pct(info_after.score.cp(solver)) >= target_wp - tolerance


def check_move_sync(engine: chess.engine.SimpleEngine, board: chess.Board,
                    move: chess.Move, puzzle: Puzzle, step: int,
                    nodes: int) -> str:
    """Synchrone Prüfung (für Tests/CLI): 'correct' | 'alt' | 'wrong'."""
    sol = solution_move(puzzle, step)
    if sol is not None and move == sol:
        return "correct"
    after = board.copy()
    after.push(move)
    if after.is_checkmate():
        return "alt"
    infos = _analyse2(engine, after, nodes, multipv=1)
    if infos and alt_move_ok(infos[0], puzzle.targets[step], board.turn):
        return "alt"
    return "wrong"


def generate_punish_puzzles(engine: chess.engine.SimpleEngine,
                            records: List[GameRecord], opponent: str,
                            verify_nodes: int, cancel=None,
                            progress: Optional[Callable[[int, int, str],
                                                        None]] = None
                            ) -> Optional[List[Puzzle]]:
    """Widerlegungs-Rätsel aus den Fehlern des *Gegners*.

    Für jeden ?/??-Zug des Gegners entsteht die Stellung *nach* seinem
    Fehlzug – der Löser (also du) muss die Bestrafung finden. Dieselbe
    Eindeutigkeitsprüfung wie bei den eigenen Rätseln.
    """
    puzzles: List[Puzzle] = []
    seen: set = set()
    today = date.today().isoformat()

    for gi, rec in enumerate(records):
        opp_white = rec.headers.get("White", "") == opponent
        board = rec.start_board()
        for i, uci in enumerate(rec.moves_uci):
            if cancel is not None and cancel.is_set():
                return None
            move = chess.Move.from_uci(uci)
            mj = rec.judgements[i] if i < len(rec.judgements) else None
            if mj is None or not board.is_legal(move):
                break
            is_opp_mistake = (mj.mover_white == opp_white
                              and mj.judgement in ("?", "??"))
            board.push(move)
            if not is_opp_mistake:
                continue
            key = board.fen()
            if key in seen:
                continue
            seen.add(key)
            if progress:
                progress(gi + 1, len(records),
                         f"prüfe Widerlegung (Zug {mj.ply // 2 + 1})")
            built = build_solution(engine, board, verify_nodes)
            if built is None:
                continue
            sol, targets, mate = built
            themes = []
            for m in mj.motifs:
                code = m.get("code")
                if code and code not in themes:
                    themes.append(code)
            if mate and "mattfuehrung" not in themes:
                themes.insert(0, "mattfuehrung")
            if not themes:
                themes.append("sonstiges")
            dots = "." if mj.mover_white else "…"
            puzzles.append(Puzzle(
                pid=_pid(key, sol[0].uci()),
                fen=key,
                solution=[m.uci() for m in sol],
                targets=[round(t, 1) for t in targets],
                themes=themes,
                source={
                    "white": rec.headers.get("White", "?"),
                    "black": rec.headers.get("Black", "?"),
                    "date": rec.headers.get("Date", "?"),
                    "opponent": opponent,
                    "move_no": f"{mj.ply // 2 + 1}{dots}",
                    "played_san": mj.san,
                    "played_uci": mj.uci,
                    "judgement": mj.judgement or "",
                    "kind": "widerlegung",
                },
                swing=round(mj.drop_wp, 1),
                mate=mate,
                created=today,
                due=today,
            ))
    return puzzles
