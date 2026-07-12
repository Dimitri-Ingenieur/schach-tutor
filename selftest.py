# -*- coding: utf-8 -*-
"""Funktionstest ohne GUI: prüft Klassifikation, Motive und Gegner-Dossier.

Beispiele:
    python selftest.py                                  # Engine aus settings.json
    python selftest.py --engine /usr/games/stockfish    # beliebige UCI-Engine
    python selftest.py --nodes 5000
"""

import argparse
import io
import json
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
import chesscom
import lichess
import live
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

        # --- Test 9: Chess.com-Download (Offline-Stub) -------------------
        print("\nTest 9 – Chess.com-Export (Monatsarchive, Stub):")
        P = lambda ev: f'[Event "{ev}"]\n[Result "*"]\n\n1. e4 e5 *'
        base = "https://api.chess.com/pub/player/testspieler42/games"
        payloads = {
            base + "/archives": {"archives": [base + "/2026/05",
                                              base + "/2026/06"]},
            base + "/2026/05": {"games": [
                {"pgn": P("ALT"), "rules": "chess", "rated": True,
                 "time_class": "rapid"}]},
            base + "/2026/06": {"games": [
                {"pgn": P("JUNI_A"), "rules": "chess", "rated": True,
                 "time_class": "blitz"},
                {"pgn": P("VARIANTE"), "rules": "chess960", "rated": True,
                 "time_class": "blitz"},
                {"pgn": P("JUNI_B"), "rules": "chess", "rated": True,
                 "time_class": "rapid"},
                {"pgn": P("BULLET"), "rules": "chess", "rated": True,
                 "time_class": "bullet"},
                {"pgn": P("UNGEWERTET"), "rules": "chess", "rated": False,
                 "time_class": "blitz"}]},
        }

        def cc_opener(req):
            return io.BytesIO(json.dumps(payloads[req.full_url]).encode())

        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "cc.pgn")
            n = chesscom.download_games("testspieler42", dest, max_games=10,
                                        opener=cc_opener)
            txt = open(dest, encoding="utf-8").read()
            ok &= check("3 Standard-Partien übernommen", n == 3)
            ok &= check("Varianten/Bullet/Ungewertet gefiltert",
                        all(x not in txt for x in
                            ("VARIANTE", "BULLET", "UNGEWERTET")))
            ok &= check("chronologische Reihenfolge (ALT vor JUNI)",
                        -1 < txt.find("ALT") < txt.find("JUNI_A")
                        < txt.find("JUNI_B"))
            n2 = chesscom.download_games("testspieler42", dest, max_games=1,
                                         opener=cc_opener)
            txt2 = open(dest, encoding="utf-8").read()
            ok &= check("max_games greift (nur neueste Partie)",
                        n2 == 1 and "JUNI_B" in txt2 and "ALT" not in txt2)
        try:
            chesscom.download_games("../evil", "/tmp/x.pgn",
                                    opener=cc_opener)
            ok &= check("ungültiger Chess.com-Name wird abgelehnt", False)
        except ValueError:
            ok &= check("ungültiger Chess.com-Name wird abgelehnt", True)

        # --- Test 10: Live-Beobachtung (Offline-Stubs) -------------------
        print("\nTest 10 – Live-Beobachtung (Lichess-Stream/Chess.com-Daily):")
        cur_json = {"id": "abcd1234", "moves": "e4 e5 Nf3", "speed": "blitz",
                    "players": {
                        "white": {"user": {"name": "A"}, "rating": 1500},
                        "black": {"user": {"name": "TestSpieler42"},
                                  "rating": 1490}}}

        def cur_opener(req):
            assert "/api/user/testspieler42/current-game" in req.full_url
            return io.BytesIO(json.dumps(cur_json).encode())

        g = live.lichess_current_game("testspieler42", opener=cur_opener)
        ok &= check("Snapshot der laufenden Partie geladen",
                    g.get("id") == "abcd1234")
        b, sans = live.board_from_san(g["moves"])
        ok &= check("SAN-Züge korrekt aufgebaut (Sf3 steht)",
                    sans == ["e4", "e5", "Nf3"]
                    and b.piece_at(chess.F3) is not None)

        fen1 = ("rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R "
                "w KQkq - 2 3")
        stream_bytes = (
            json.dumps({"id": "abcd1234", "fen": fen1,
                        "lastMove": "g8f6"}) + "\n"
            + "\n"                                    # Keepalive
            + json.dumps({"fen": fen1.replace("RNBQKB1R w", "RNBQKB1R b"),
                          "lm": "f1c4", "wc": 175, "bc": 168}) + "\n")

        def stream_opener(req):
            assert "/api/stream/game/abcd1234" in req.full_url
            return io.BytesIO(stream_bytes.encode())

        import threading as _thr
        events = []
        n = live.stream_game("abcd1234", events.append, _thr.Event(),
                             opener=stream_opener)
        ok &= check("Stream: 2 Ereignisse, Keepalive ignoriert",
                    n == 2 and len(events) == 2
                    and events[1].get("lm") == "f1c4")
        stop = _thr.Event()
        events2 = []
        live.stream_game("abcd1234",
                         lambda d: (events2.append(d), stop.set()),
                         stop, opener=stream_opener)
        ok &= check("Stream: Stopp greift nach 1. Ereignis",
                    len(events2) == 1)
        try:
            live.stream_game("ab/../cd", events.append, _thr.Event(),
                             opener=stream_opener)
            ok &= check("ungültige Partie-ID wird abgelehnt", False)
        except ValueError:
            ok &= check("ungültige Partie-ID wird abgelehnt", True)

        daily_json = {"games": [{
            "fen": chess.STARTING_FEN, "pgn": "1. d4 *",
            "white": "https://api.chess.com/pub/player/a",
            "black": "https://api.chess.com/pub/player/testspieler42",
            "last_activity": 42}]}

        def daily_opener(req):
            assert "/pub/player/testspieler42/games" in req.full_url
            return io.BytesIO(json.dumps(daily_json).encode())

        dg = live.chesscom_daily_games("testspieler42",
                                       opener=daily_opener)
        ok &= check("Daily-Partien-Snapshot geladen",
                    len(dg) == 1 and "fen" in dg[0])
        ok &= check("Uhr-Formatierung (s und ms)",
                    live.fmt_clock(65) == "1:05"
                    and live.fmt_clock(3700) == "1:01:40"
                    and live.fmt_clock(3_700_000) == "1:01:40")

        # --- Test 11: APP_DIR unter PyInstaller (--onefile) --------------
        print("\nTest 11 – APP_DIR bleibt stabil, auch 'gefroren' "
              "(PyInstaller):")
        import importlib
        import config as config_mod
        normal_dir = config_mod._app_dir()
        ok &= check("normal: Ordner des Moduls selbst",
                    normal_dir == os.path.dirname(
                        os.path.abspath(config_mod.__file__)))

        old_frozen = getattr(sys, "frozen", False)
        old_exe = sys.executable
        try:
            sys.frozen = True
            sys.executable = os.path.join(os.sep, "Programme",
                                          "SchachTutor", "SchachTutor.exe")
            frozen_dir = config_mod._app_dir()
            ok &= check("gefroren: Ordner der .exe, NICHT des Temp-"
                        "Entpackpfads",
                        frozen_dir == os.path.join(os.sep, "Programme",
                                                   "SchachTutor"))
            ok &= check("gefroren: unterscheidet sich vom Modul-Pfad "
                        "(kein Zufallstreffer)", frozen_dir != normal_dir)
        finally:
            if old_frozen:
                sys.frozen = old_frozen
            else:
                try:
                    del sys.frozen
                except AttributeError:
                    pass
            sys.executable = old_exe
            importlib.reload(config_mod)  # DEFAULTS/APP_DIR sauber zurück

        # --- Test 12: Video-Zugerkennung (Vision-Pipeline) ---------------
        print("\nTest 12 – Video-Zugerkennung (synthetische Frames + "
              "echtes Video):")
        import cv2
        import vision
        from vision_testboard import render_frame, BOARD_RECT

        def vrun(moves_uci, flipped):
            vb = chess.Board()
            clf = vision.CellClassifier()
            if not clf.calibrate(
                    vision.split_cells(render_frame(vb, flipped),
                                       BOARD_RECT), chess.Board()):
                return None, []
            rec = vision.Recognizer()
            seen = []
            for uci in moves_uci:
                vb.push_uci(uci)
                grid = clf.classify(vision.split_cells(
                    render_frame(vb, flipped), BOARD_RECT))
                got = []
                for _ in range(vision.STABLE_FRAMES):
                    got = rec.feed(grid) or got
                if not got:
                    return clf.flipped, seen
                seen.extend(m.uci() for m in got)
            return clf.flipped, seen

        vg1 = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]
        fl, seen = vrun(vg1, False)
        ok &= check("Partie erkannt (Schlagzug + Matt)",
                    seen == vg1 and fl is False)
        vg2 = ["e2e4", "g8f6", "g1f3", "b7b6", "f1e2", "c8b7", "e1g1",
               "e7e6", "e4e5", "d7d5", "e5d6"]
        fl2, seen2 = vrun(vg2, True)
        ok &= check("gedrehtes Brett: Rochade + en passant",
                    seen2 == vg2 and fl2 is True)

        pb = chess.Board("8/P6k/8/8/8/8/8/7K w - - 0 1")
        clf = vision.CellClassifier()
        okc = clf.calibrate(vision.split_cells(render_frame(pb),
                                               BOARD_RECT), pb.copy())
        rec = vision.Recognizer(board=pb.copy())
        pb.push_uci("a7a8q")
        grid = clf.classify(vision.split_cells(render_frame(pb),
                                               BOARD_RECT))
        got = []
        for _ in range(vision.STABLE_FRAMES):
            got = rec.feed(grid) or got
        ok &= check("Umwandlung → Dame",
                    okc and len(got) == 1 and got[0].uci() == "a7a8q")

        # Hand-ungenaue Rechtecke: die automatische Nachjustierung muss
        # beide Orientierungen retten (hier saß der "Kalibrierung
        # fehlgeschlagen bei Schwarz unten"-Bug: ohne Suche scheiterten
        # ~2/3 aller handgezogenen Rechtecke, orientierungsunabhängig).
        bx, by, bw, bh = BOARD_RECT
        for vflip, (pdx, pdy, pds) in ((False, (3, -4, 5)),
                                       (True, (-4, 3, -6))):
            frame = render_frame(chess.Board(), vflip)
            wobbly = (bx + pdx, by + pdy, bw + pds, bh + pds)
            clf, refined = vision.calibrate_search(frame, wobbly,
                                                   chess.Board())
            ok &= check(f"Rechteck-Nachjustierung (flipped={vflip})",
                        clf is not None and clf.flipped == vflip
                        and refined is not None)

        rec3 = vision.Recognizer()
        db = chess.Board(); db.push_uci("e2e4"); db.push_uci("e7e5")
        caught = []
        for _ in range(vision.STABLE_FRAMES):
            caught = rec3.feed(vision.occupancy_from_board(db)) or caught
        ok &= check("verpasster Zug wird aufgeholt (2 Züge am Stück)",
                    [m.uci() for m in caught] == ["e2e4", "e7e5"]
                    and not rec3.desync
                    and rec3.board.fen() == db.fen())

        rec4 = vision.Recognizer()
        weird = [list(r) for r in
                 vision.occupancy_from_board(chess.Board())]
        weird[4][4] = vision.WHITE_PC        # Geisterfigur → unerreichbar
        wgrid = tuple(tuple(r) for r in weird)
        for _ in range(vision.STABLE_FRAMES):
            rec4.feed(wgrid)
        ok &= check("unerreichbares Muster → Desync bleibt", rec4.desync)

        for vflip in (False, True):
            frame = render_frame(chess.Board(), vflip)
            clf, _r = vision.calibrate_search(
                frame, (bx + 3, by - 4, bw + 5, bh + 5), expected=None)
            ok &= check(f"FEN-lose Kalibrierung (flipped={vflip})",
                        clf is not None and clf.flipped == vflip)
        midb = chess.Board()
        for u in ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"):
            midb.push_uci(u)
        for vflip in (False, True):
            clf, _r = vision.calibrate_search(render_frame(midb, vflip),
                                              BOARD_RECT, expected=None)
            ok &= check(f"Mittelspiel FEN-los erkannt (flipped={vflip})",
                        clf is not None and clf.flipped == vflip
                        and clf.detected_board.board_fen()
                        == midb.board_fen())
        endb = chess.Board("6k1/5ppp/8/8/2Q5/8/5PPP/3R2K1 w - - 0 30")
        clf, _r = vision.calibrate_search(render_frame(endb), BOARD_RECT,
                                          expected=None)
        ok &= check("Endspiel FEN-los erkannt (typbewusste Farben)",
                    clf is not None and clf.detected_board.board_fen()
                    == endb.board_fen())
        oneb = chess.Board(); oneb.push_uci("e2e4")
        clf, ref = vision.calibrate_search(render_frame(oneb), BOARD_RECT,
                                           expected=None)
        rec5 = vision.Recognizer(board=clf.detected_board.copy(),
                                 turn_uncertain=clf.turn_uncertain)
        afterb = oneb.copy(); afterb.push_uci("c7c5")
        agrid = clf.classify(vision.split_cells(render_frame(afterb),
                                                ref))
        caught2 = []
        for _ in range(vision.STABLE_FRAMES):
            caught2 = rec5.feed(agrid) or caught2
        ok &= check("Zugseiten-Autokorrektur (kürzeste Erklärung)",
                    [m.uci() for m in caught2] == ["c7c5"]
                    and rec5.board.board_fen() == afterb.board_fen())

        with tempfile.TemporaryDirectory() as td:
            vpath = os.path.join(td, "spiel.avi")
            vw = cv2.VideoWriter(vpath, cv2.VideoWriter_fourcc(*"MJPG"),
                                 8.0, (520, 460))
            vb = chess.Board()
            vframes = [render_frame(vb)] * 4
            for uci in vg1:
                vb.push_uci(uci)
                vframes += [render_frame(vb)] * 4
            for f in vframes:
                vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
            vw.release()
            cap = cv2.VideoCapture(vpath)
            clf = vision.CellClassifier()
            rec = vision.Recognizer()
            vseen, calibrated = [], False
            while True:
                okr, fr = cap.read()
                if not okr:
                    break
                cells = vision.split_cells(
                    cv2.cvtColor(fr, cv2.COLOR_BGR2RGB), BOARD_RECT)
                if not calibrated:
                    calibrated = clf.calibrate(cells, chess.Board())
                    continue
                got = rec.feed(clf.classify(cells))
                vseen.extend(m.uci() for m in got)
            cap.release()
            ok &= check("Video-Roundtrip (Datei → Züge, MJPG)",
                        vseen == vg1)

    finally:
        engine.quit()

    print("\n" + ("ALLE TESTS OK" if ok else "MINDESTENS EIN TEST FEHLGESCHLAGEN"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
