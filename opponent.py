# -*- coding: utf-8 -*-
"""Gegner-Analyse: PGN-Partien auswerten, Schwächenprofil und Dossier erzeugen."""

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import chess
import chess.engine
import chess.pgn

import config
import openings
from analysis import (MoveJudgement, NormInfo, judge_move, normalize_info,
                      terminal_score)

JUDGE_KEYS = ("??", "?", "?!")
PHASES = ("Eröffnung", "Mittelspiel", "Endspiel")

MOTIF_LABELS = {
    "figur_haengt": "Figuren eingestellt / hängen lassen",
    "material_verlust": "Material durch Übersehen verloren",
    "gabel": "Gabeln/Doppelangriffe zugelassen",
    "matt_zugelassen": "Mattdrohungen übersehen (Königssicherheit)",
    "matt_verpasst": "Eigene Mattführung verpasst",
    "taktik_verpasst": "Starke taktische Möglichkeiten ausgelassen",
}


# ---------------------------------------------------------------- Datentypen

@dataclass
class GameRecord:
    headers: Dict[str, str]
    judgements: List[MoveJudgement]
    moves_uci: List[str]

    def to_dict(self) -> dict:
        return {
            "headers": self.headers,
            "judgements": [j.to_dict() for j in self.judgements],
            "moves_uci": self.moves_uci,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameRecord":
        return cls(
            headers=dict(d.get("headers", {})),
            judgements=[MoveJudgement.from_dict(j)
                        for j in d.get("judgements", [])],
            moves_uci=list(d.get("moves_uci", [])),
        )

    def start_board(self) -> chess.Board:
        fen = self.headers.get("FEN")
        if fen and self.headers.get("SetUp", "1") != "0":
            try:
                return chess.Board(fen)
            except ValueError:
                pass
        return chess.Board()


# ---------------------------------------------------------------- Cache

def game_key(raw_pgn: str, nodes: int) -> str:
    h = hashlib.md5()
    h.update(raw_pgn.encode("utf-8", errors="replace"))
    h.update(str(nodes).encode())
    return h.hexdigest()


def load_cached(key: str) -> Optional[GameRecord]:
    path = os.path.join(config.CACHE_DIR, key + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return GameRecord.from_dict(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cached(key: str, rec: GameRecord) -> None:
    config.ensure_cache_dir()
    path = os.path.join(config.CACHE_DIR, key + ".json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec.to_dict(), f, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------- Analyse

def analyze_game(engine: chess.engine.SimpleEngine, game: chess.pgn.Game,
                 nodes: int, cancel=None,
                 progress: Optional[Callable[[int, int], None]] = None
                 ) -> Optional[GameRecord]:
    """Bewertet jede Stellung der Partie genau einmal (beide Spieler).

    Gibt None zurück, wenn `cancel` gesetzt wurde.
    """
    moves = list(game.mainline_moves())
    positions: List[chess.Board] = [game.board()]
    b = game.board()
    for mv in moves:
        b.push(mv)
        positions.append(b.copy())

    infos: List[NormInfo] = []
    total = len(positions)
    for i, pos in enumerate(positions):
        if cancel is not None and cancel.is_set():
            return None
        ts = terminal_score(pos)
        if ts is not None:
            infos.append(NormInfo(score=ts, pv=[]))
        else:
            raw = engine.analyse(pos, chess.engine.Limit(nodes=nodes),
                                 multipv=1)
            if isinstance(raw, list):
                raw = raw[0]
            infos.append(normalize_info(raw))
        if progress:
            progress(i + 1, total)

    judgements = [judge_move(positions[i], mv, infos[i], infos[i + 1])
                  for i, mv in enumerate(moves)]
    return GameRecord(headers=dict(game.headers), judgements=judgements,
                      moves_uci=[m.uci() for m in moves])


# ---------------------------------------------------------------- Profil

def _result_for(headers: Dict[str, str], as_white: bool) -> Optional[float]:
    res = headers.get("Result", "*")
    if res == "1-0":
        return 1.0 if as_white else 0.0
    if res == "0-1":
        return 0.0 if as_white else 1.0
    if res == "1/2-1/2":
        return 0.5
    return None


def build_profile(name: str, records: List[GameRecord]) -> dict:
    prof = {
        "name": name,
        "games": 0,
        "white": {"n": 0, "score": 0.0},
        "black": {"n": 0, "score": 0.0},
        "acpl": {p: [0, 0] for p in PHASES},            # [Summe, Anzahl Züge]
        "counts": {p: Counter() for p in PHASES},        # ?? / ? / ?! je Phase
        "motifs": Counter(),
        "openings": {},   # (farbe, name) -> {n, score_sum, score_n, acpl...}
        "vs_first": {},   # als Schwarz: 1. Zug von Weiß -> {n, score...}
        "examples": [],   # markante Patzer für den Bericht
    }

    for gi, rec in enumerate(records):
        white = rec.headers.get("White", "?")
        as_white = (white == name)
        color = chess.WHITE if as_white else chess.BLACK
        res = _result_for(rec.headers, as_white)

        prof["games"] += 1
        side = prof["white" if as_white else "black"]
        if res is not None:
            side["n"] += 1
            side["score"] += res

        sans = [j.san for j in rec.judgements]
        op_name = openings.name_for(sans) or (
            f"1.{sans[0]}" if sans else "unbekannt")

        # Eröffnungsstatistik
        okey = ("W" if as_white else "S", op_name)
        entry = prof["openings"].setdefault(
            okey, {"n": 0, "score_sum": 0.0, "score_n": 0,
                   "acpl_sum": 0, "acpl_n": 0})
        entry["n"] += 1
        if res is not None:
            entry["score_sum"] += res
            entry["score_n"] += 1

        if not as_white and sans:
            vkey = f"1.{sans[0]}"
            ventry = prof["vs_first"].setdefault(
                vkey, {"n": 0, "score_sum": 0.0, "score_n": 0})
            ventry["n"] += 1
            if res is not None:
                ventry["score_sum"] += res
                ventry["score_n"] += 1

        opponent = rec.headers.get("Black" if as_white else "White", "?")

        for j in rec.judgements:
            if j.mover_white != (color == chess.WHITE):
                continue
            loss = j.cp_loss()
            prof["acpl"][j.phase][0] += loss
            prof["acpl"][j.phase][1] += 1
            if j.phase == "Eröffnung":
                entry["acpl_sum"] += loss
                entry["acpl_n"] += 1
            if j.judgement:
                prof["counts"][j.phase][j.judgement] += 1
                for m in j.motifs:
                    prof["motifs"][m["code"]] += 1
                if j.judgement == "??" and len(prof["examples"]) < 6:
                    move_no = j.ply // 2 + 1
                    dots = "." if j.mover_white else "…"
                    motif = j.motifs[0]["text"] if j.motifs else ""
                    prof["examples"].append(
                        f"Partie {gi + 1} (gegen {opponent}): "
                        f"{move_no}{dots}{j.san}?? statt {j.best_san}. {motif}")

    return prof


# ---------------------------------------------------------------- Bericht

def _avg(pair) -> Optional[float]:
    s, n = pair
    return (s / n) if n else None


def _score_pct(entry) -> Optional[float]:
    if entry.get("score_n"):
        return 100.0 * entry["score_sum"] / entry["score_n"]
    return None


def render_report(prof: dict) -> str:
    L: List[str] = []
    name = prof["name"]
    n = prof["games"]
    w, b = prof["white"], prof["black"]

    L.append(f"GEGNER-DOSSIER: {name}")
    L.append("=" * (16 + len(name)))
    total_n = w["n"] + b["n"]
    total_score = (100.0 * (w["score"] + b["score"]) / total_n) if total_n else 0.0
    L.append(f"Basis: {n} Partien "
             f"({w['n']} mit Weiß, {b['n']} mit Schwarz) · "
             f"Gesamtscore {total_score:.0f} %")
    if w["n"]:
        L.append(f"  Score mit Weiß:    {100.0 * w['score'] / w['n']:.0f} %")
    if b["n"]:
        L.append(f"  Score mit Schwarz: {100.0 * b['score'] / b['n']:.0f} %")
    L.append("")

    # Phasen
    L.append("FEHLERQUOTE NACH PARTIEPHASE")
    L.append("-" * 28)
    acpl_by_phase = {}
    for phase in PHASES:
        avg = _avg(prof["acpl"][phase])
        cnt = prof["counts"][phase]
        if avg is None:
            continue
        acpl_by_phase[phase] = avg
        L.append(f"  {phase:<12} ACPL {avg:5.0f}   "
                 f"Patzer {cnt.get('??', 0):>2} · Fehler {cnt.get('?', 0):>2} · "
                 f"Ungenauigkeiten {cnt.get('?!', 0):>2}")
    weakest = None
    if acpl_by_phase:
        weakest = max(acpl_by_phase, key=lambda p: acpl_by_phase[p])
        L.append(f"  → Schwächste Phase: {weakest}")
    L.append("")

    # Fehlermuster
    if prof["motifs"]:
        L.append("WIEDERKEHRENDE FEHLERMUSTER")
        L.append("-" * 27)
        for code, cnt in prof["motifs"].most_common():
            L.append(f"  {cnt:>2}× {MOTIF_LABELS.get(code, code)}")
        L.append("")

    # Eröffnungen
    L.append("ERÖFFNUNGSREPERTOIRE")
    L.append("-" * 20)
    for color_key, label in (("W", "Mit Weiß"), ("S", "Mit Schwarz")):
        rows = [(k[1], v) for k, v in prof["openings"].items()
                if k[0] == color_key]
        if not rows:
            continue
        rows.sort(key=lambda kv: kv[1]["n"], reverse=True)
        L.append(f"  {label}:")
        for op_name, v in rows:
            sc = _score_pct(v)
            sc_txt = f"Score {sc:.0f} %" if sc is not None else "Score –"
            ac = (f"ACPL(Eröffn.) {v['acpl_sum'] / v['acpl_n']:.0f}"
                  if v["acpl_n"] else "")
            L.append(f"    {v['n']:>2}× {op_name:<28} {sc_txt}  {ac}")
    if prof["vs_first"]:
        L.append("  Als Schwarz nach erstem Zug von Weiß:")
        for k, v in sorted(prof["vs_first"].items(),
                           key=lambda kv: kv[1]["n"], reverse=True):
            sc = _score_pct(v)
            sc_txt = f"Score {sc:.0f} %" if sc is not None else ""
            L.append(f"    {v['n']:>2}× gegen {k:<6} {sc_txt}")
    L.append("")

    # Beispiele
    if prof["examples"]:
        L.append("MARKANTE PATZER (Beispiele)")
        L.append("-" * 27)
        for ex in prof["examples"]:
            L.append(f"  • {ex}")
        L.append("")

    # Empfehlungen
    recs = _recommendations(prof, acpl_by_phase, weakest)
    if recs:
        L.append("VORBEREITUNGS-EMPFEHLUNGEN")
        L.append("-" * 26)
        for i, r in enumerate(recs, 1):
            L.append(f"  {i}. {r}")
        L.append("")

    L.append("(Analyse-Basis: Engine-Bewertung jeder Stellung; "
             "ACPL = mittlerer Centipawn-Verlust pro Zug.)")
    return "\n".join(L)


def _recommendations(prof: dict, acpl_by_phase: dict,
                     weakest: Optional[str]) -> List[str]:
    recs: List[str] = []
    n = max(1, prof["games"])
    m = prof["motifs"]

    if weakest and prof["acpl"][weakest][1] >= 15:
        a = acpl_by_phase[weakest]
        others = [v for k, v in acpl_by_phase.items() if k != weakest]
        if others and a >= 1.4 * max(others):
            if weakest == "Endspiel":
                recs.append(f"Seine Endspiele sind deutlich schwächer "
                            f"(ACPL {a:.0f}). Tausche in ausgeglichene oder "
                            f"leicht bessere Endspiele ab – dort macht er die "
                            f"meisten Fehler.")
            elif weakest == "Eröffnung":
                recs.append(f"Er verlässt die Theorie früh mit Fehlern "
                            f"(ACPL {a:.0f} in der Eröffnung). Spiele solide "
                            f"Hauptvarianten und halte die Spannung – die "
                            f"ersten 15 Züge sind deine beste Phase.")
            else:
                recs.append(f"Im Mittelspiel ist er am anfälligsten "
                            f"(ACPL {a:.0f}). Halte viele Figuren auf dem "
                            f"Brett und vermeide frühe Vereinfachungen.")

    hang = m.get("figur_haengt", 0) + m.get("material_verlust", 0)
    if hang / n >= 0.6:
        recs.append(f"Er übersieht regelmäßig hängende Figuren "
                    f"({hang}× in {n} Partien). Stelle in jeder Stellung "
                    f"konkrete Drohungen auf – einfache Angriffe auf "
                    f"ungedeckte Figuren zahlen sich gegen ihn aus.")
    if m.get("gabel", 0) >= 3:
        recs.append(f"Anfällig für Gabeln und Doppelangriffe "
                    f"({m['gabel']}×). Achte auf Springer-Manöver und "
                    f"Damen-Ausfälle mit Doppeldrohung.")
    if m.get("matt_zugelassen", 0) >= 2:
        recs.append(f"Königssicherheit ist ein Problem "
                    f"({m['matt_zugelassen']}× Matt zugelassen). Strebe "
                    f"Angriffsstellungen gegen seinen König an, statt früh "
                    f"abzuwickeln.")

    # Schwächste Eröffnung je Farbe
    for color_key, side_txt, my_txt in (
            ("W", "mit Weiß", "Als Schwarz"),
            ("S", "mit Schwarz", "Als Weiß")):
        worst = None
        for (ck, op_name), v in prof["openings"].items():
            if ck != color_key or v["n"] < 3:
                continue
            sc = _score_pct(v)
            if sc is None:
                continue
            if worst is None or sc < worst[1]:
                worst = (op_name, sc, v["n"])
        if worst and worst[1] <= 35.0:
            recs.append(f"{my_txt} lohnt sich {worst[0]}: {side_txt} holt er "
                        f"dort nur {worst[1]:.0f} % aus {worst[2]} Partien.")

    # 1.e4 vs 1.d4 als Schwarz
    e4 = prof["vs_first"].get("1.e4")
    d4 = prof["vs_first"].get("1.d4")
    if e4 and d4 and e4["score_n"] >= 3 and d4["score_n"] >= 3:
        se, sd = _score_pct(e4), _score_pct(d4)
        if se is not None and sd is not None and abs(se - sd) >= 15.0:
            better = "1.e4" if se < sd else "1.d4"
            worse_pct = min(se, sd)
            recs.append(f"Eröffne mit {better}: dagegen erreicht er als "
                        f"Schwarz nur {worse_pct:.0f} %.")

    blunders = sum(c.get("??", 0) for c in prof["counts"].values())
    if blunders / n >= 1.5:
        recs.append(f"Taktisch unzuverlässig ({blunders / n:.1f} Patzer pro "
                    f"Partie). Halte die Stellung scharf und rechne konkret – "
                    f"lange Partien mit vielen kritischen Momenten sind gegen "
                    f"ihn dein Freund.")

    return recs
