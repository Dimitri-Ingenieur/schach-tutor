# -*- coding: utf-8 -*-
"""Funktionstest ohne GUI: prüft Klassifikation, Motive und Gegner-Dossier.

Beispiele:
    python selftest.py                                  # Engine aus settings.json
    python selftest.py --engine /usr/games/stockfish    # beliebige UCI-Engine
    python selftest.py --nodes 5000
"""

import argparse
import io
import os
import sys
import tempfile
from datetime import date, timedelta

import chess
import chess.engine
import chess.pgn

import config
from analysis import feedback_text, judge_move, normalize_info
from opponent import analyze_game, build_profile, render_report
from opponent_book import build_book
import lichess
from puzzles import (THEME_LABELS, PuzzleDB, check_move_sync,
                     generate_punish_puzzles, generate_puzzles)

SAMPLE_PGN = """\
[Event "Test"]
[White "Trainer"]
[Black "Mustermann"]
[Result "1-0"]

1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0

[Event "Test"]
[White "Mustermann"]
[Black "Trainer"]
[Result "0-1"]

1. f3 e5 2. g4 Qh4# 0-1
"""


def analyse(engine, board, nodes, multipv=1):
    raw = engine.analyse(board, chess.engine.Limit(nodes=nodes),
                         multipv=multipv)
    if isinstance(raw, dict):
        raw = [raw]
    return [normalize_info(i) for i in raw]


def check(label, cond):
    mark = "OK " if cond else "FEHLER"
    print(f"  [{mark}] {label}")
    return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", help="Pfad zur UCI-Engine (Standard: settings.json)")
    ap.add_argument("--nodes", type=int, default=20000)
    args = ap.parse_args()

    if args.engine:
        cmd = [args.engine]
    else:
        settings = config.load()
        cmd = config.build_engine_command(settings)
    print(f"Engine: {' '.join(cmd)}   (Knoten: {args.nodes})\n")

    engine = chess.engine.SimpleEngine.popen_uci(cmd)
    ok = True
    try:
        # --- Test 1: Matt-in-1 zugelassen (Schäfermatt) -------------------
        print("Test 1 – Matt zugelassen (3...Sf6?? erlaubt Dxf7#):")
        b = chess.Board()
        for mv in ("e2e4", "e7e5", "d1h5", "b8c6", "f1c4"):
            b.push(chess.Move.from_uci(mv))
        move = chess.Move.from_uci("g8f6")
        info_before = analyse(engine, b, args.nodes)[0]
        after = b.copy(); after.push(move)
        info_after = analyse(engine, after, args.nodes)[0]
        mj = judge_move(b, move, info_before, info_after)
        print("    " + feedback_text(mj).replace("\n", "\n    "))
        ok &= check("als Patzer (??) erkannt", mj.judgement == "??")
        ok &= check("Motiv 'matt_zugelassen' gefunden",
                    any(m["code"] == "matt_zugelassen" for m in mj.motifs))

        # --- Test 2: Materialverlust (2...g6?? nach 2.Dh5) -----------------
        print("\nTest 2 – Materialverlust/Gabel (2...g6?? nach 2.Dh5):")
        b = chess.Board()
        for mv in ("e2e4", "e7e5", "d1h5"):
            b.push(chess.Move.from_uci(mv))
        move = chess.Move.from_uci("g7g6")
        info_before = analyse(engine, b, args.nodes)[0]
        after = b.copy(); after.push(move)
        info_after = analyse(engine, after, args.nodes)[0]
        mj = judge_move(b, move, info_before, info_after)
        print("    " + feedback_text(mj).replace("\n", "\n    "))
        ok &= check("als Fehler/Patzer erkannt", mj.judgement in ("?", "??"))
        ok &= check("Material-/Gabel-Motiv gefunden",
                    any(m["code"] in ("material_verlust", "gabel",
                                      "figur_haengt") for m in mj.motifs))

        # --- Test 3: Bester Zug wird nicht bestraft ------------------------
        print("\nTest 3 – Bester Zug bleibt unbeanstandet:")
        b = chess.Board()
        info_before = analyse(engine, b, args.nodes)[0]
        best = info_before.pv[0]
        after = b.copy(); after.push(best)
        info_after = analyse(engine, after, args.nodes)[0]
        mj = judge_move(b, best, info_before, info_after)
        print("    " + feedback_text(mj).replace("\n", "\n    "))
        ok &= check("kein Fehlerurteil", mj.judgement is None)

        # --- Test 4: Gegner-Dossier aus Mini-PGN ---------------------------
        print("\nTest 4 – Gegner-Dossier (2 Kurzpartien, 'Mustermann'):")
        stream = io.StringIO(SAMPLE_PGN)
        records = []
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            rec = analyze_game(engine, game, nodes=min(args.nodes, 5000))
            records.append(rec)
        prof = build_profile("Mustermann", records)
        report = render_report(prof)
        print("    " + report.replace("\n", "\n    "))
        ok &= check("2 Partien im Profil", prof["games"] == 2)
        ok &= check("mindestens 1 Patzer erfasst",
                    sum(c.get("??", 0) for c in prof["counts"].values()) >= 1)

        # --- Test 5: Rätsel aus eigenen Fehlern --------------------------
        print("\nTest 5 – Rätsel-Erzeugung aus eigener Partie "
              "(4.Sc3?? statt Dxf7#):")
        own_pgn = (
            '[Event "Test"]\n[White "Ich"]\n[Black "Gegner"]\n'
            '[Result "*"]\n\n'
            '1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Nc3 g6 5. Qf3 d6 *\n')
        game = chess.pgn.read_game(io.StringIO(own_pgn))
        rec = analyze_game(engine, game, nodes=min(args.nodes, 5000))
        pz = generate_puzzles(engine, [rec], "Ich",
                              verify_nodes=min(args.nodes, 10000))
        ok &= check("mindestens 1 Rätsel erzeugt", bool(pz))
        target = next((p for p in pz or []
                       if p.solution and p.solution[0] == "h5f7"), None)
        ok &= check("Lösung ist Dxf7#", target is not None)
        if target is not None:
            print(f"    Themen: {[THEME_LABELS.get(t, t) for t in target.themes]}"
                  f" · Züge: {target.solution} · Matt: {target.mate}")
            ok &= check("als Mattführung erkannt", target.mate)
            b = chess.Board(target.fen)
            res_wrong = check_move_sync(engine, b,
                                        chess.Move.from_uci("b1c3"),
                                        target, 0,
                                        nodes=min(args.nodes, 5000))
            res_right = check_move_sync(engine, b,
                                        chess.Move.from_uci("h5f7"),
                                        target, 0,
                                        nodes=min(args.nodes, 5000))
            ok &= check("Original-Fehlzug wird abgelehnt",
                        res_wrong == "wrong")
            ok &= check("Lösungszug wird akzeptiert", res_right == "correct")

            # Leitner-Logik
            with tempfile.TemporaryDirectory() as td:
                db = PuzzleDB(os.path.join(td, "puzzles.json"))
                added = db.add(pz)
                ok &= check("Deck übernimmt Rätsel", added == len(pz))
                ok &= check("Duplikate werden verworfen", db.add(pz) == 0)
                today = date.today()
                db.apply_result(target, solved=True, today=today)
                ok &= check("gelöst → Box 1, fällig morgen",
                            target.box == 1 and target.due ==
                            (today + timedelta(days=1)).isoformat())
                db.apply_result(target, solved=False, today=today)
                ok &= check("Fehlversuch → zurück in Box 0",
                            target.box == 0 and target.due ==
                            today.isoformat())
                ok &= check("fällige Rätsel werden gefunden",
                            any(p.pid == target.pid for p in db.due()))

        # --- Test 6: Widerlegungs-Rätsel aus Gegnerfehlern ---------------
        print("\nTest 6 – Widerlegungs-Rätsel (Gegner erlaubt Dxf7#):")
        punish = generate_punish_puzzles(engine, [rec], "Gegner",
                                         verify_nodes=min(args.nodes, 10000))
        ok &= check("mindestens 1 Widerlegungs-Rätsel", bool(punish))
        pw = next((p for p in punish or []
                   if p.solution and p.solution[0] == "h5f7"), None)
        ok &= check("Widerlegung ist Dxf7#", pw is not None)
        if pw is not None:
            ok &= check("Quelle als Widerlegung markiert",
                        pw.source.get("kind") == "widerlegung"
                        and pw.source.get("opponent") == "Gegner")
            ok &= check("Patzer-Zug gespeichert",
                        pw.source.get("played_uci") == "g8f6")

        # --- Test 7: Polyglot-Gegner-Buch --------------------------------
        print("\nTest 7 – Gegner-Eröffnungsbuch (inkl. Rochade-Kodierung):")
        book_pgn = (
            '[Event "T"]\n[White "Ich"]\n[Black "Gegner"]\n[Result "*"]\n\n'
            '1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. d3 d6 *\n\n'
            '[Event "T"]\n[White "Ich"]\n[Black "X"]\n[Result "*"]\n\n'
            '1. e4 c5 2. Nf3 d6 *\n')
        fh = io.StringIO(book_pgn)
        bgames = []
        while True:
            g = chess.pgn.read_game(fh)
            if g is None:
                break
            bgames.append(g)
        with tempfile.TemporaryDirectory() as td:
            bpath = os.path.join(td, "gegner.bin")
            positions, entries = build_book(bgames, "Ich", bpath)
            ok &= check("Buch geschrieben (Stellungen > 3)",
                        positions >= 4 and entries >= positions)
            with chess.polyglot.open_reader(bpath) as reader:
                b = chess.Board()
                first = reader.find(b)
                ok &= check("Startzug = e4 mit Gewicht 2",
                            first.move == chess.Move.from_uci("e2e4")
                            and first.weight == 2)
                for mv in ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"):
                    b.push_uci(mv)
                castle = reader.weighted_choice(b)
                ok &= check("Rochade dekodiert legal (e1g1)",
                            castle.move == chess.Move.from_uci("e1g1")
                            and b.is_legal(castle.move))

        # --- Test 8: Lichess-Download (Offline-Stub) ---------------------
        print("\nTest 8 – Lichess-Export (mit Stub-Verbindung):")
        stub_pgn = book_pgn.encode("utf-8")

        def fake_opener(req):
            assert "TestSpieler42" in req.full_url
            assert "max=2000" in req.full_url
            return io.BytesIO(stub_pgn)

        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "dl.pgn")
            n = lichess.download_games("TestSpieler42", dest,
                                       opener=fake_opener)
            ok &= check("PGN heruntergeladen und geschrieben",
                        n == len(stub_pgn) and os.path.getsize(dest) == n)
        try:
            lichess.download_games("../böse", "/tmp/x.pgn",
                                   opener=fake_opener)
            ok &= check("ungültiger Name wird abgelehnt", False)
        except ValueError:
            ok &= check("ungültiger Name wird abgelehnt", True)

    finally:
        engine.quit()

    print("\n" + ("ALLE TESTS OK" if ok else "MINDESTENS EIN TEST FEHLGESCHLAGEN"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
