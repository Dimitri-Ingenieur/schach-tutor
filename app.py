# -*- coding: utf-8 -*-
"""Hauptfenster: Tabs, Menü, Einstellungen und Engine-Verwaltung."""

import os
import queue
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import chess

import config
from engine_hub import EngineHub
from opponent_tab import OpponentTab
from puzzle_tab import PuzzleTab
from training_tab import TrainingTab


class ChessTutorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Schach-Tutor (LC0)")
        self.settings = config.load()
        self.hub = None
        self._cb_queue = queue.Queue()

        self._build_menu()

        self.notebook = ttk.Notebook(self)
        self.training_tab = TrainingTab(self.notebook, self)
        self.puzzle_tab = PuzzleTab(self.notebook, self)
        self.opponent_tab = OpponentTab(self.notebook, self)
        self.notebook.add(self.training_tab, text="  Training  ")
        self.notebook.add(self.puzzle_tab, text="  Rätsel  ")
        self.notebook.add(self.opponent_tab, text="  Gegner-Analyse  ")
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="Engine wird gestartet …")
        ttk.Label(self, textvariable=self.status_var, anchor="w",
                  relief=tk.SUNKEN).pack(fill=tk.X, side=tk.BOTTOM)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(25, self._poll_callbacks)
        self.after(100, self.restart_engine)

    # ------------------------------------------------------------ Infrastruktur

    def dispatch(self, fn) -> None:
        """Hebt Engine-Callbacks thread-sicher in den Tk-Mainloop.

        Wird aus dem Engine-Worker-Thread aufgerufen und darf deshalb
        selbst keine Tk-Aufrufe machen — tkinter ist nicht thread-sicher
        (after() aus fremden Threads wirft „main thread is not in main
        loop"). Die Callbacks landen in einer Queue, die der Mainloop
        alle 25 ms leert.
        """
        self._cb_queue.put(fn)

    def _poll_callbacks(self) -> None:
        try:
            while True:
                fn = self._cb_queue.get_nowait()
                try:
                    fn()
                except tk.TclError:
                    pass
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self.after(25, self._poll_callbacks)
        except tk.TclError:
            pass

    def show_training(self) -> None:
        self.notebook.select(self.training_tab)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Einstellungen …",
                           command=self.open_settings)
        m_file.add_separator()
        m_file.add_command(label="Beenden", command=self._on_close)
        menubar.add_cascade(label="Datei", menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Über", command=self._about)
        menubar.add_cascade(label="Hilfe", menu=m_help)
        self.config(menu=menubar)

    def _about(self) -> None:
        messagebox.showinfo(
            "Über",
            "Schach-Tutor\n\n"
            "Trainingspartner und Gegner-Analyse auf Basis von LC0\n"
            "(oder jeder anderen UCI-Engine).\n\n"
            "Modi:\n"
            "• Training: spielen, Fehler sofort erklärt bekommen,\n"
            "  Züge zurücknehmen und besser machen.\n"
            "• Rätsel: eigene Partien laden – aus deinen Fehlern\n"
            "  entsteht ein persönliches Taktik-Deck mit\n"
            "  Wiederholung nach dem Leitner-System.\n"
            "• Gegner-Analyse: PGN eines Spielers laden, Schwächen-\n"
            "  Dossier erzeugen, Partien reviewen und kritische\n"
            "  Stellungen direkt weitertrainieren.")

    # ------------------------------------------------------------ Engine

    def restart_engine(self) -> None:
        if self.hub is not None:
            self.hub.quit()
            self.hub = None

        path = config.engine_path(self.settings)
        if not path or not os.path.isfile(path):
            self.status_var.set(
                "Keine Engine gefunden – bitte Datei → Einstellungen öffnen.")
            self.open_settings()
            return

        cmd = config.build_engine_command(self.settings)
        self.status_var.set(f"Starte Engine: {' '.join(cmd)}")
        self.hub = EngineHub(cmd, dispatcher=self.dispatch)
        self.hub.start()

        def pong(infos, err):
            if err is not None:
                self.status_var.set(f"Engine-Fehler: {err}")
                messagebox.showerror(
                    "Engine-Fehler",
                    f"Die Engine konnte nicht gestartet werden:\n{err}\n\n"
                    "Häufigste Ursache: Pfad oder Backend passen nicht zu "
                    "dieser Maschine.\n"
                    "NVIDIA-Build:  cuda-fp16 (bzw. onnx-trt)\n"
                    "ROCm-Build:    rocm-fp16\n\n"
                    "Die Einstellungen werden jetzt geöffnet.")
                self.open_settings()
                return
            name = self.hub.engine_name if self.hub else "Engine"
            self.status_var.set(f"Engine bereit: {name}")

        self.hub.analyse(chess.Board(), nodes=1, multipv=1, cb=pong)

    def _on_close(self) -> None:
        try:
            self.training_tab._sync_settings()
        except Exception:
            pass
        config.save(self.settings)
        if self.hub is not None:
            self.hub.quit()
        self.destroy()

    # ------------------------------------------------------------ Einstellungen

    def open_settings(self) -> None:
        SettingsDialog(self)


class SettingsDialog(tk.Toplevel):
    def __init__(self, app: ChessTutorApp):
        super().__init__(app)
        self.app = app
        self.title("Einstellungen")
        self.resizable(False, False)
        s = app.settings

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        self.vars = {}
        rows = [
            ("engine_path", "Engine (LC0-Binärdatei):", True),
            ("weights_path", "Weights (.pb.gz, nur LC0):", True),
            ("backend", "Backend (nur LC0):", False),
            ("extra_args", "Zusätzliche Argumente:", False),
            ("book_path", "Polyglot-Buch (.bin, optional):", True),
        ]
        backends = ["", "cuda-fp16", "cuda", "cuda-auto", "onnx-trt",
                    "rocm-fp16", "rocm", "eigen", "blas", "opencl"]
        for r, (key, label, browse) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w",
                                              pady=3)
            var = tk.StringVar(value=str(s.get(key, "")))
            self.vars[key] = var
            if key == "backend":
                ttk.Combobox(frame, textvariable=var, width=46,
                             values=backends).grid(
                    row=r, column=1, sticky="we", padx=4)
            else:
                ttk.Entry(frame, textvariable=var, width=48).grid(
                    row=r, column=1, sticky="we", padx=4)
            if browse:
                ttk.Button(frame, text="…", width=3,
                           command=lambda v=var: self._browse(v)).grid(
                    row=r, column=2)

        num_rows = [
            ("analysis_nodes", "Analyse-Knoten (Training):"),
            ("play_nodes", "Spiel-Knoten (Engine-Stärke):"),
            ("opponent_nodes", "Knoten für Gegner-Analyse:"),
        ]
        base = len(rows)
        for i, (key, label) in enumerate(num_rows):
            ttk.Label(frame, text=label).grid(row=base + i, column=0,
                                              sticky="w", pady=3)
            var = tk.IntVar(value=int(s.get(key, 100)))
            self.vars[key] = var
            ttk.Spinbox(frame, from_=1, to=1000000, width=10,
                        textvariable=var).grid(row=base + i, column=1,
                                               sticky="w", padx=4)

        self.takeback_var = tk.BooleanVar(
            value=bool(s.get("offer_takeback", True)))
        ttk.Checkbutton(frame, text="Bei Fehlern Rücknahme anbieten",
                        variable=self.takeback_var).grid(
            row=base + len(num_rows), column=0, columnspan=2, sticky="w",
            pady=4)

        btns = ttk.Frame(frame)
        btns.grid(row=base + len(num_rows) + 1, column=0, columnspan=3,
                  pady=(10, 0))
        ttk.Button(btns, text="Speichern und Engine (neu) starten",
                   command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Abbrechen",
                   command=self.destroy).pack(side=tk.LEFT, padx=4)

        self.transient(app)
        self.grab_set()

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(parent=self)
        if path:
            var.set(path)

    def _save(self) -> None:
        s = self.app.settings
        for key, var in self.vars.items():
            try:
                s[key] = var.get()
            except (tk.TclError, ValueError):
                pass
        s["offer_takeback"] = bool(self.takeback_var.get())
        config.save(s)
        self.destroy()
        self.app.restart_engine()


def main() -> None:
    app = ChessTutorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
