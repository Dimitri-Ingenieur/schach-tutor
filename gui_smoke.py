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
    ap.add_argument("--seconds", type=float, default=3.0,
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

    # Beobachten-Tab: ein Stream-Ereignis durch den echten Pfad schicken.
    ev = {"fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR "
                 "b KQkq - 0 1",
          "lm": "e2e4", "wc": 180, "bc": 180}
    app.live_tab._apply_stream_event(app.live_tab._gen, ev)
    app.update()
    assert app.live_tab.board.piece_at(chess.E4) is not None, (
        "Beobachten-Tab hat das Stream-Ereignis nicht übernommen")
    print("Beobachten-Tab OK: Stream-Ereignis verarbeitet")

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
