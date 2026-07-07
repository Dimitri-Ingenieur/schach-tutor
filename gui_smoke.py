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
    for tab in (app.training_tab, app.puzzle_tab, app.opponent_tab):
        app.notebook.select(tab)
        app.update()

    # EvalBar aktiv zeichnen lassen (hier saß der _w-Bug).
    app.training_tab.eval_bar.set_eval(63.0, "+0.55")
    app.training_tab.eval_bar.set_eval(12.0, "-2.10")
    app.update()

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
