# -*- coding: utf-8 -*-
"""GUI-Rauchtest: startet die komplette App unter einem (virtuellen) Display.

Fängt Fehlerklassen, die Import- und Logik-Tests nicht sehen können –
z. B. überschriebene tkinter-interne Attribute, Layout-Crashes oder
Callbacks, die beim Aufbau der Oberfläche feuern.

Aufruf mit Display:   python3 gui_smoke.py [--engine PFAD]
Headless (CI/Server): xvfb-run -a python3 gui_smoke.py
"""

import argparse
import time
import tkinter as tk

import config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="/usr/games/stockfish",
                    help="UCI-Engine für den Test (Default: Stockfish)")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="wie lange Events gepumpt werden")
    args = ap.parse_args()

    # Einstellungen so schreiben, dass beim Start kein modaler
    # Einstellungs-Dialog aufgeht (der würde den Test blockieren).
    s = config.load()
    s["engine_path"] = args.engine
    s["weights_path"] = ""
    config.save(s)

    from app import ChessTutorApp  # Import erst nach dem Settings-Setup

    app = ChessTutorApp()
    app.update()

    # Alle Tabs einmal anzeigen (erzwingt Layout/Zeichnen jedes Widgets).
    for tab in (app.training_tab, app.puzzle_tab, app.opponent_tab,
                app.live_tab):
        app.notebook.select(tab)
        app.update()

    # EvalBar aktiv zeichnen lassen (hier saß der _w-Bug).
    app.training_tab.eval_bar.set_eval(63.0, "+0.55")
    app.training_tab.eval_bar.set_eval(12.0, "-2.10")
    app.update()

    # Umwandlungsdialog wirklich auslösen (hier saß der grab_set-Bug: er
    # zeigte sich nur mit echtem Window Manager, s. gui_smoke.py-Historie).
    import chess
    bw = app.training_tab.board_widget
    bw.set_position(chess.Board("8/P6k/8/8/8/8/8/7K w - - 0 1"))
    app.update()

    def click_promo_choice(tries=0):
        for w in bw.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.winfo_children()[0].invoke()   # erste Option = Dame
                return
        if tries < 100:
            app.after(30, click_promo_choice, tries + 1)

    app.after(50, click_promo_choice)
    promo_move = bw._build_move(chess.A7, chess.A8)
    app.update()
    assert promo_move is not None and promo_move.promotion == chess.QUEEN, (
        f"Umwandlungsdialog lieferte kein gültiges Ergebnis: {promo_move}")
    print("Umwandlungsdialog OK:", promo_move.uci())

    # Beobachten-Tab: kompletten Stream-Ablauf simulieren — Beschreibung,
    # stilles Aufholen (Replay ab Zug 1), dann Live-Zug mit Nummerierung.
    lt = app.live_tab
    lt.state = "watching"
    lt.board = chess.Board()
    lt._sboard = None
    lt._live = False
    lt._catchup_to = 0
    lt._pending = []
    lt._clear_log()
    gen = lt._gen
    lt._apply_stream_event(gen, {"id": "smoke123", "speed": "blitz",
                                 "turns": 2, "players": {}})
    b = chess.Board()
    b.push_uci("e2e4")
    lt._apply_stream_event(gen, {"fen": b.fen(), "lm": "e2e4"})
    b.push_uci("c7c6")
    lt._apply_stream_event(gen, {"fen": b.fen(), "lm": "c7c6"})
    b.push_uci("d2d4")
    lt._apply_stream_event(gen, {"fen": b.fen(), "lm": "d2d4",
                                 "wc": 180, "bc": 179})
    app.update()
    logtxt = lt.log.get("1.0", "end")
    assert "Bisher: 1. e4  1… c6" in logtxt, f"Aufhol-Zeile fehlt: {logtxt!r}"
    assert "2. d4" in logtxt, f"Live-Zug fehlt/falsch nummeriert: {logtxt!r}"
    assert logtxt.count("1. e4") == 1, "Züge doppelt geloggt"
    assert lt.board.piece_at(chess.D4) is not None, "Brett nicht aktuell"

    # Reconnect simulieren: Lichess spielt danach ALLES ab Zug 1 erneut
    # vor — es darf keine einzige Zeile doppelt erscheinen, und die
    # "Bisher"-Zeile darf nicht erneut auftauchen. Nur der wirklich neue
    # Zug (3... d5) wird geloggt.
    lt._prepare_stream(lt._gen)
    replay = chess.Board()
    for uci in ("e2e4", "c7c6", "d2d4"):
        replay.push_uci(uci)
        lt._apply_stream_event(lt._gen, {"fen": replay.fen(), "lm": uci})
    replay.push_uci("d7d5")
    lt._apply_stream_event(lt._gen, {"fen": replay.fen(), "lm": "d7d5",
                                     "wc": 170, "bc": 168})
    app.update()
    logtxt = lt.log.get("1.0", "end")
    assert logtxt.count("Bisher:") == 1, f"Bisher doppelt: {logtxt!r}"
    assert logtxt.count("1. e4") == 1, f"Replay doppelt geloggt: {logtxt!r}"
    assert logtxt.count("2. d4") == 1, f"Replay doppelt geloggt: {logtxt!r}"
    assert logtxt.count("2… d5") == 1, f"neuer Zug fehlt: {logtxt!r}"
    lt.stop_watching()
    print("Beobachten-Tab OK: Aufholphase still, Nummerierung korrekt, "
          "Reconnect ohne Duplikate")

    # Live-Bewertung darf bei schnellem Spiel (Züge < Entpreller-Frist)
    # nicht für die ganze Beobachtung beim Startwert hängen bleiben (hier
    # saß der EVAL_MAX_WAIT_MS-Bug: reiner "warte auf Ruhe"-Entpreller
    # verhungert bei Blitz/Bullet).
    lt._catchup_to = 0
    lt._sboard = None
    lt._live = False
    lt._pending = []
    lt.state = "watching"
    lt.eval_bar.set_eval(50.0, "0.0")   # definierter Ausgangspunkt
    lt._last_eval_at = 0.0
    fb = chess.Board()
    for uci in ("e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4",
               "g8f6", "b1c3", "a7a6"):
        fb.push_uci(uci)
        lt._apply_stream_event(lt._gen, {"fen": fb.fen(), "lm": uci})
        app.update()
        time.sleep(0.15)                     # < EVAL_DEBOUNCE_MS
        app.update()
    assert lt.eval_bar._label != "0.0", (
        "Live-Bewertung ist bei schnellem Spiel nie gefeuert (Entpreller "
        "ohne Obergrenze verhungert) — Label steht noch auf Startwert")
    lt.stop_watching()
    print("Beobachten-Tab OK: Live-Bewertung verhungert nicht bei "
          "schnellem Spiel (Label:", lt.eval_bar._label + ")")

    # EvalBar muss mit der Brettdrehung mitgehen (hier saß der
    # "Schwarz-immer-oben"-Bug: der Balken zeigte Weiß' Anteil auch dann
    # noch von unten, wenn das Brett auf Schwarz gedreht war).
    lt.eval_bar.set_flipped(False)
    lt.eval_bar.set_eval(80.0, "+2.0")           # Weiß klar besser
    lt.eval_bar.set_flipped(True)                # Brett zeigt jetzt Schwarz unten
    flipped_bottom = 1.0 - lt.eval_bar._frac      # Schwarz' eigener Anteil
    assert flipped_bottom < 0.3, (
        f"EvalBar zeigt nach Flip nicht Schwarz' Anteil unten "
        f"(erwartet klein, da Schwarz schlecht steht): {flipped_bottom}")
    lt.eval_bar.set_flipped(False)
    print("EvalBar-Drehung OK: folgt der Brettorientierung")

    # Bereits beendete Partie beim Verbinden (Stream meldet sofortiges
    # Ende, z. B. nach Aufgabe): die Schlussstellung muss trotzdem
    # ausgewertet werden, statt vom Auto-Stopp abgewürgt zu werden.
    lt.state = "watching"
    lt._gen += 1
    fgen = lt._gen
    lt.board = chess.Board(
        "r1bq1rk1/ppp2ppp/2n5/3np3/2B5/2N2N2/PPPP1PPP/R1BQ1RK1 w - - 4 9")
    lt.eval_bar.set_eval(50.0, "0.0")
    lt.eval_line_var.set("")
    lt._finished(fgen)
    t_end = time.time() + 6
    while time.time() < t_end and lt.eval_bar._label == "0.0":
        app.update()
        time.sleep(0.02)
    assert lt.eval_bar._label != "0.0", (
        "Schlussstellung einer beim Verbinden bereits beendeten Partie "
        "wurde nicht ausgewertet (stop_watching hat die Bewertung "
        "abgewürgt)")
    print("Beobachten-Tab OK: Schlussstellung wird trotz Auto-Stopp "
         "noch bewertet (Label:", lt.eval_bar._label + ")")

    # Video-Tab: Kalibrierung + ein erkannter Zug durch den echten
    # Kommentar-Pfad (ohne echte Videoquelle — Frame wird injiziert).
    from vision_testboard import render_frame, BOARD_RECT
    vt = app.vision_tab
    vt.state = "running"
    vt._gen += 1
    vt.rect = BOARD_RECT
    with vt._frame_lock:
        vt._latest = render_frame(chess.Board())
    vt.calibrate()
    assert vt.classifier is not None, "Video-Kalibrierung fehlgeschlagen"
    vt._apply_moves(vt._gen, ["e2e4"])
    app.update()
    assert vt.board.piece_at(chess.E4) is not None, "Zug nicht übernommen"
    vlog = vt.log.get("1.0", "end")
    assert "1. e4" in vlog, f"Zug nicht im Kommentar-Log: {vlog!r}"
    t_end = time.time() + 6
    while time.time() < t_end and vt.eval_bar._label == "0.0":
        app.update()
        time.sleep(0.02)
    assert vt.eval_bar._label != "0.0", "Video-Tab: Bewertung kam nie an"
    vt.stop_capture()
    print("Video-Tab OK: Kalibrierung, Zugerkennung und Kommentar-Pfad "
         "(Eval:", vt.eval_bar._label + ")")

    # Events pumpen, bis der Engine-Ping durch die Callback-Queue zurück ist.
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        app.update()
        time.sleep(0.02)
        if "bereit" in app.status_var.get():
            break

    engine_ok = app.hub is not None
    status = app.status_var.get()
    app._on_close()

    print(f"Engine gestartet: {engine_ok} ({status})")
    assert engine_ok, "EngineHub wurde nicht gestartet"
    assert "bereit" in status, ("Engine-Ready kam nie im Mainloop an — "
                                "Dispatch-Kette (Worker → Queue → Tk) defekt?")
    print("GUI-SMOKE OK")


if __name__ == "__main__":
    main()
