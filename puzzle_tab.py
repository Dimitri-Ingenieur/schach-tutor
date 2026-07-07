# -*- coding: utf-8 -*-
"""Rätsel-Tab: eigene Partien → persönliches Taktik-Deck → Schwächen abbauen."""

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Dict, List, Optional

import chess
import chess.pgn

import config
from analysis import JUDGE_NAMES
from board_widget import ARROW_BAD, ARROW_BEST, ARROW_HINT, BoardWidget
from opponent import analyze_game, game_key, load_cached, save_cached
from puzzles import (THEME_LABELS, Puzzle, PuzzleDB, alt_move_ok,
                     generate_puzzles, solution_move)


class PuzzleTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.db = PuzzleDB(config.PUZZLES_FILE)

        self.games: List[chess.pgn.Game] = []
        self.raw_games: List[str] = []
        self.cancel: Optional[threading.Event] = None

        self.state = "idle"     # idle | solving | checking | replying | done
        self._gen = 0
        self.current: Optional[Puzzle] = None
        self.board = chess.Board()
        self.sol_idx = 0        # bereits gespielte Züge der Lösungsvariante
        self.failed = False
        self.tips = 0
        self.queue: List[Puzzle] = []
        self.session_solved = 0
        self.session_total = 0

        self._build_ui()
        self._refresh_deck_info()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        gen = ttk.LabelFrame(self, text=" Rätsel aus eigenen Partien erzeugen ")
        gen.pack(fill=tk.X, padx=8, pady=(8, 4))

        row1 = ttk.Frame(gen)
        row1.pack(fill=tk.X, padx=6, pady=4)
        ttk.Button(row1, text="Eigene Partien laden (PGN) …",
                   command=self.load_pgn).pack(side=tk.LEFT)
        self.file_var = tk.StringVar(value="keine Datei geladen")
        ttk.Label(row1, textvariable=self.file_var).pack(side=tk.LEFT, padx=8)

        row2 = ttk.Frame(gen)
        row2.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(row2, text="Ich bin:").pack(side=tk.LEFT)
        self.player_var = tk.StringVar(
            value=self.app.settings.get("player_name", ""))
        self.player_box = ttk.Combobox(row2, textvariable=self.player_var,
                                       width=20, state="readonly")
        self.player_box.pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Analyse-Knoten:").pack(side=tk.LEFT,
                                                     padx=(12, 0))
        self.nodes_var = tk.IntVar(
            value=int(self.app.settings.get("opponent_nodes", 150)))
        ttk.Spinbox(row2, from_=10, to=100000, width=7,
                    textvariable=self.nodes_var).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Prüf-Knoten:").pack(side=tk.LEFT, padx=(12, 0))
        self.verify_var = tk.IntVar(
            value=int(self.app.settings.get("puzzle_verify_nodes", 1000)))
        ttk.Spinbox(row2, from_=50, to=1000000, width=8,
                    textvariable=self.verify_var).pack(side=tk.LEFT, padx=4)
        self.gen_btn = ttk.Button(row2, text="Rätsel erzeugen",
                                  command=self.start_generation)
        self.gen_btn.pack(side=tk.LEFT, padx=12)
        self.cancel_btn = ttk.Button(row2, text="Abbrechen",
                                     command=self.cancel_generation,
                                     state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        row3 = ttk.Frame(gen)
        row3.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.progress = ttk.Progressbar(row3, mode="determinate", length=280)
        self.progress.pack(side=tk.LEFT)
        self.prog_var = tk.StringVar(value="")
        ttk.Label(row3, textvariable=self.prog_var).pack(side=tk.LEFT, padx=8)

        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, anchor="n")
        self.board_widget = BoardWidget(left, square=60,
                                        on_move=self._on_user_move)
        self.board_widget.pack()
        self.board_widget.set_interactive(False)

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        self.deck_var = tk.StringVar()
        ttk.Label(right, textvariable=self.deck_var).pack(fill=tk.X)

        row4 = ttk.Frame(right)
        row4.pack(fill=tk.X, pady=4)
        ttk.Label(row4, text="Quelle:").pack(side=tk.LEFT)
        self.kind_var = tk.StringVar(value="Alle Quellen")
        self.kind_box = ttk.Combobox(
            row4, textvariable=self.kind_var, width=24, state="readonly",
            values=["Alle Quellen", "Eigene Fehler",
                    "Widerlegung (Gegnerfehler)"])
        self.kind_box.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row4, text="Thema:").pack(side=tk.LEFT)
        self.theme_var = tk.StringVar(value="Alle Themen")
        self.theme_box = ttk.Combobox(row4, textvariable=self.theme_var,
                                      width=32, state="readonly")
        self.theme_box.pack(side=tk.LEFT, padx=4)
        ttk.Button(row4, text="Training starten",
                   command=self.start_training).pack(side=tk.LEFT, padx=8)
        ttk.Button(row4, text="Statistik …",
                   command=self.show_stats).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Noch kein Training gestartet.")
        ttk.Label(right, textvariable=self.status_var,
                  font=("DejaVu Sans", 11, "bold")).pack(fill=tk.X, pady=4)

        row5 = ttk.Frame(right)
        row5.pack(fill=tk.X, pady=(0, 4))
        self.hint_btn = ttk.Button(row5, text="Tipp", command=self.hint,
                                   state=tk.DISABLED)
        self.hint_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.solution_btn = ttk.Button(row5, text="Lösung zeigen",
                                       command=self.show_solution,
                                       state=tk.DISABLED)
        self.solution_btn.pack(side=tk.LEFT, padx=4)
        self.skip_btn = ttk.Button(row5, text="Überspringen",
                                   command=self.skip, state=tk.DISABLED)
        self.skip_btn.pack(side=tk.LEFT, padx=4)
        self.next_btn = ttk.Button(row5, text="Nächstes Rätsel",
                                   command=self.next_puzzle,
                                   state=tk.DISABLED)
        self.next_btn.pack(side=tk.LEFT, padx=4)

        self.info = ScrolledText(right, height=11, wrap=tk.WORD,
                                 font=("DejaVu Sans", 10), state=tk.DISABLED)
        self.info.pack(fill=tk.BOTH, expand=True)
        self.info.tag_configure("good", foreground="#1a7a1a",
                                font=("DejaVu Sans", 10, "bold"))
        self.info.tag_configure("bad", foreground="#b02a1e",
                                font=("DejaVu Sans", 10, "bold"))
        self.info.tag_configure("info", foreground="#444444")

    # ------------------------------------------------------------ Helfer

    def _feed(self, text: str, tag: str = "info") -> None:
        self.info.configure(state=tk.NORMAL)
        self.info.insert(tk.END, text + "\n", tag)
        self.info.see(tk.END)
        self.info.configure(state=tk.DISABLED)

    def _clear_info(self) -> None:
        self.info.configure(state=tk.NORMAL)
        self.info.delete("1.0", tk.END)
        self.info.configure(state=tk.DISABLED)

    def _refresh_deck_info(self) -> None:
        due = len(self.db.due())
        total = len(self.db.puzzles)
        mastered = self.db.mastered_count()
        self.deck_var.set(f"Deck: {total} Rätsel · heute fällig: {due} · "
                          f"gemeistert (Box 4): {mastered}")
        themes = ["Alle Themen"] + [THEME_LABELS.get(t, t)
                                    for t in self.db.themes_in_deck()]
        self.theme_box.configure(values=themes)
        if self.theme_var.get() not in themes:
            self.theme_var.set("Alle Themen")

    def _theme_code(self) -> Optional[str]:
        label = self.theme_var.get()
        if label == "Alle Themen":
            return None
        for code, lab in THEME_LABELS.items():
            if lab == label:
                return code
        return None

    def _kind_code(self):
        return {"Eigene Fehler": "eigener_fehler",
                "Widerlegung (Gegnerfehler)": "widerlegung"}.get(
                    self.kind_var.get())

    def _buttons(self, hint=False, solution=False, skip=False, nxt=False):
        self.hint_btn.configure(state=tk.NORMAL if hint else tk.DISABLED)
        self.solution_btn.configure(
            state=tk.NORMAL if solution else tk.DISABLED)
        self.skip_btn.configure(state=tk.NORMAL if skip else tk.DISABLED)
        self.next_btn.configure(state=tk.NORMAL if nxt else tk.DISABLED)

    # ------------------------------------------------------------ PGN

    def load_pgn(self) -> None:
        path = filedialog.askopenfilename(
            title="Eigene Partien (PGN) wählen",
            filetypes=[("PGN-Dateien", "*.pgn"), ("Alle Dateien", "*.*")])
        if not path:
            return
        games, raws = [], []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                while True:
                    game = chess.pgn.read_game(f)
                    if game is None:
                        break
                    games.append(game)
                    raws.append(str(game))
        except OSError as exc:
            messagebox.showerror("Fehler",
                                 f"PGN konnte nicht gelesen werden:\n{exc}")
            return
        if not games:
            messagebox.showinfo("Leer", "Keine Partien in der Datei gefunden.")
            return
        self.games, self.raw_games = games, raws
        self.file_var.set(f"{path.split('/')[-1]}  ({len(games)} Partien)")

        names: Dict[str, int] = {}
        for g in games:
            for key in ("White", "Black"):
                n = g.headers.get(key, "").strip()
                if n and n != "?":
                    names[n] = names.get(n, 0) + 1
        ordered = sorted(names, key=lambda n: -names[n])
        self.player_box.configure(values=ordered)
        remembered = self.app.settings.get("player_name", "")
        if remembered in ordered:
            self.player_var.set(remembered)
        elif ordered:
            self.player_var.set(ordered[0])

    # ------------------------------------------------------------ Erzeugung

    def start_generation(self) -> None:
        hub = self.app.hub
        if hub is None:
            messagebox.showwarning("Engine fehlt",
                                   "Bitte zuerst unter Datei → Einstellungen "
                                   "eine Engine konfigurieren.")
            return
        name = self.player_var.get().strip()
        if not self.games or not name:
            messagebox.showinfo("Partien laden",
                                "Bitte zuerst eine PGN mit deinen Partien "
                                "laden und deinen Namen auswählen.")
            return
        try:
            nodes = max(10, int(self.nodes_var.get()))
            verify = max(50, int(self.verify_var.get()))
        except (tk.TclError, ValueError):
            nodes, verify = 150, 1000
        self.app.settings["opponent_nodes"] = nodes
        self.app.settings["puzzle_verify_nodes"] = verify
        self.app.settings["player_name"] = name

        jobs = [(self.games[i], self.raw_games[i])
                for i in range(len(self.games))
                if name in (self.games[i].headers.get("White", ""),
                            self.games[i].headers.get("Black", ""))]
        if not jobs:
            messagebox.showinfo("Keine Partien",
                                f"Keine Partien von „{name}“ gefunden.")
            return

        self.cancel = threading.Event()
        self.gen_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.progress.configure(maximum=len(jobs) * 2, value=0)
        self.prog_var.set("Analysiere Partien …")
        dispatch = self.app.dispatch
        cancel = self.cancel

        def batch(engine):
            recs = []
            for gi, (game, raw) in enumerate(jobs):
                key = game_key(raw, nodes)
                rec = load_cached(key)
                if rec is None:
                    def prog(p, t, gi=gi):
                        dispatch(lambda: self.prog_var.set(
                            f"Partie {gi + 1}/{len(jobs)} · "
                            f"Stellung {p}/{t}"))
                    rec = analyze_game(engine, game, nodes,
                                       cancel=cancel, progress=prog)
                    if rec is None:
                        return ("cancelled", [])
                    save_cached(key, rec)
                recs.append(rec)
                dispatch(lambda gi=gi: self.progress.configure(value=gi + 1))

            def gprog(g, t, msg):
                dispatch(lambda: (
                    self.progress.configure(value=len(jobs) + g),
                    self.prog_var.set(f"Erzeuge Rätsel · Partie {g}/{t} "
                                      f"({msg})")))

            pz = generate_puzzles(engine, recs, name, verify,
                                  cancel=cancel, progress=gprog)
            if pz is None:
                return ("cancelled", [])
            return ("ok", pz)

        hub.submit(batch, cb=self._generation_done)

    def cancel_generation(self) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.prog_var.set("Abbruch angefordert …")

    def _generation_done(self, result, err) -> None:
        self.gen_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        if err is not None:
            messagebox.showerror("Engine-Fehler",
                                 f"Rätsel-Erzeugung fehlgeschlagen:\n{err}")
            self.prog_var.set("Fehler.")
            return
        status, pz = result
        if status == "cancelled":
            self.prog_var.set("Abgebrochen.")
            return
        added = self.db.add(pz)
        self.db.save()
        self._refresh_deck_info()
        self.prog_var.set(f"Fertig: {len(pz)} Kandidaten geprüft, "
                          f"{added} neue Rätsel im Deck.")
        if added == 0 and not pz:
            self._feed("Keine eindeutigen Rätsel gefunden – entweder waren "
                       "die Fehler positioneller Natur (mehrere gleich gute "
                       "Züge) oder es gab schlicht keine ?/??-Züge von dir "
                       "in diesen Partien.", "info")

    # ------------------------------------------------------------ Training

    def start_training(self) -> None:
        self._gen += 1
        theme = self._theme_code()
        kind = self._kind_code()
        self.queue = self.db.due(theme, kind=kind)
        self.session_solved = 0
        self.session_total = len(self.queue)
        if not self.queue:
            nxt = self.db.next_due_date(theme, kind=kind)
            if not self.db.puzzles:
                self.status_var.set("Das Deck ist leer – erst oben Rätsel "
                                    "aus deinen Partien erzeugen.")
            elif nxt is not None:
                self.status_var.set(f"Für heute alles erledigt! Nächste "
                                    f"Wiederholung fällig am {nxt.strftime('%d.%m.%Y')}.")
            return
        self.next_puzzle()

    def next_puzzle(self) -> None:
        self._gen += 1
        if not self.queue:
            self.state = "idle"
            self.board_widget.set_interactive(False)
            self._buttons()
            self.status_var.set(
                f"Runde beendet: {self.session_solved}/{self.session_total} "
                f"beim ersten Versuch gelöst. Stark!")
            self._refresh_deck_info()
            return
        p = self.queue.pop(0)
        self.current = p
        self.board = chess.Board(p.fen)
        self.sol_idx = 0
        self.failed = False
        self.tips = 0
        solver = self.board.turn
        self.board_widget.set_flipped(solver == chess.BLACK)
        src = p.source or {}
        kind = src.get("kind", "eigener_fehler")
        last = None
        if kind == "widerlegung" and src.get("played_uci"):
            try:
                last = chess.Move.from_uci(src["played_uci"])
            except ValueError:
                last = None
        self.board_widget.set_position(self.board, last)
        self.board_widget.clear_arrows()
        self.board_widget.set_interactive(True)
        self.state = "solving"
        self._buttons(hint=True, solution=True, skip=True)
        self._clear_info()
        farbe = "Weiß" if solver == chess.WHITE else "Schwarz"
        n = self.current.user_move_count()
        extra = f" (Lösung: {n} Züge)" if n > 1 else ""
        if kind == "widerlegung":
            self.status_var.set(
                f"{farbe} am Zug – finde die Widerlegung!{extra}")
            self._feed(f"{src.get('opponent', 'Der Gegner')} spielte hier "
                       f"{src.get('move_no', '')}{src.get('played_san', '?')}"
                       f"{src.get('judgement', '')} – bestrafe den Fehler! "
                       f"(Partie vom {src.get('date', '?')})", "info")
        else:
            self.status_var.set(
                f"{farbe} am Zug – finde den besten Zug!{extra}")
            self._feed(f"Aus deiner Partie gegen "
                       f"{src.get('opponent', '?')} "
                       f"({src.get('date', '?')}).", "info")
        left = len(self.queue)
        self._feed(f"Noch {left} Rätsel in dieser Runde danach.", "info")

    # ------------------------------------------------------------ Zugprüfung

    def _on_user_move(self, move: chess.Move) -> None:
        if self.state != "solving" or self.current is None:
            return
        if not self.board.is_legal(move):
            return
        sol = solution_move(self.current, self.sol_idx // 2)
        if sol is not None and move == sol:
            self._accept(move)
            return
        after = self.board.copy()
        after.push(move)
        if after.is_checkmate() and self.current.mate:
            self._accept(move, alt=True)
            return
        hub = self.app.hub
        if hub is None:
            self._reject(move, checked=False)
            return
        self.state = "checking"
        self.status_var.set("Prüfe deinen Zug …")
        gen = self._gen
        step = self.sol_idx // 2
        target = self.current.targets[step] if step < len(
            self.current.targets) else 50.0
        solver = self.board.turn
        nodes = max(200, int(self.app.settings.get(
            "puzzle_verify_nodes", 1000)) // 2)

        def done(infos, err):
            if gen != self._gen:
                return
            self.state = "solving"
            if err or not infos:
                self._reject(move, checked=False)
                return
            if alt_move_ok(infos[0], target, solver):
                self._accept(move, alt=True)
            else:
                self._reject(move)

        hub.analyse(after, nodes, multipv=1, cb=done)

    def _accept(self, move: chess.Move, alt: bool = False) -> None:
        p = self.current
        san = self.board.san(move)
        self.board.push(move)
        self.sol_idx += 1
        self.board_widget.set_position(self.board, move)
        if alt:
            self._feed(f"✓ {san} – nicht die gespeicherte Lösung, aber "
                       f"genauso stark. Zählt!", "good")
            self._finish(solved=True)
            return
        if self.sol_idx >= len(p.solution):
            self._finish(solved=True)
            return
        # Engine-Antwort einspielen, dann nächster Nutzerzug
        self.state = "replying"
        self.status_var.set(f"✓ {san} – richtig!")
        gen = self._gen

        def play_reply():
            if gen != self._gen:
                return
            reply = chess.Move.from_uci(p.solution[self.sol_idx])
            if not self.board.is_legal(reply):
                self._finish(solved=True)
                return
            rsan = self.board.san(reply)
            self.board.push(reply)
            self.sol_idx += 1
            self.board_widget.set_position(self.board, reply)
            self.state = "solving"
            self.status_var.set(f"Gegner spielt {rsan} – weiter!")

        self.after(450, play_reply)

    def _reject(self, move: chess.Move, checked: bool = True) -> None:
        try:
            san = self.board.san(move)
        except ValueError:
            san = move.uci()
        if not self.failed:
            self.failed = True
            self.db.apply_result(self.current, solved=False)
            self._refresh_deck_info()
            note = ("" if checked else
                    " (keine Engine zum Gegenprüfen – nur der Lösungszug "
                    "zählt)")
            self._feed(f"✗ {san} ist es nicht – das Rätsel kommt wieder ins "
                       f"Deck.{note} Versuch es trotzdem zu Ende zu lösen!",
                       "bad")
        else:
            self._feed(f"✗ {san} – noch nicht.", "bad")
        self.status_var.set("Falsch – versuch es noch einmal "
                            "(oder Tipp/Lösung).")

    def _finish(self, solved: bool) -> None:
        p = self.current
        self.state = "done"
        self.board_widget.set_interactive(False)
        self._buttons(nxt=True)
        if solved and not self.failed:
            self.db.apply_result(p, solved=True)
            self.session_solved += 1
            self.status_var.set("✓ Gelöst!")
            self._feed("✓ Sauber gelöst – das Rätsel steigt eine Box auf.",
                       "good")
        elif solved:
            self.status_var.set("Gelöst – nach Fehlversuch.")
            self._feed("Am Ende gefunden. Beim nächsten Mal klappt es "
                       "direkt – die Wiederholung kommt morgen.", "info")
        themes = ", ".join(THEME_LABELS.get(t, t) for t in p.themes)
        src = p.source or {}
        played = src.get("played_san", "?")
        judg = src.get("judgement", "")
        jname = JUDGE_NAMES.get(judg, "Fehler") if judg else "Fehler"
        self._feed(f"Thema: {themes}", "info")
        if src.get("kind", "eigener_fehler") == "widerlegung":
            self._feed(f"{src.get('opponent', 'Der Gegner')} patzte mit "
                       f"{src.get('move_no', '')}{played}{judg} "
                       f"({jname}, −{p.swing:.0f} % für ihn) – solche "
                       f"Geschenke jetzt auch am Brett einsammeln!", "info")
        else:
            self._feed(f"Damals spieltest du {src.get('move_no', '')}"
                       f"{played}{judg} ({jname}, −{p.swing:.0f} % "
                       f"Gewinnchance).", "info")
        self._refresh_deck_info()

    # ------------------------------------------------------------ Hilfen

    def hint(self) -> None:
        if self.state != "solving" or self.current is None:
            return
        self.tips += 1
        if self.tips == 1:
            themes = ", ".join(THEME_LABELS.get(t, t)
                               for t in self.current.themes)
            self._feed(f"Tipp: Thema ist {themes}.", "info")
            return
        sol = solution_move(self.current, self.sol_idx // 2)
        if sol is None:
            return
        if not self.failed:
            self.failed = True
            self.db.apply_result(self.current, solved=False)
            self._refresh_deck_info()
            self._feed("Pfeil-Tipp genommen – das Rätsel kommt zur "
                       "Wiederholung zurück.", "info")
        self.board_widget.set_arrows(
            [(sol.from_square, sol.to_square, ARROW_HINT)])

    def show_solution(self) -> None:
        if self.state not in ("solving",) or self.current is None:
            return
        if not self.failed:
            self.failed = True
            self.db.apply_result(self.current, solved=False)
            self._refresh_deck_info()
        self.state = "replying"
        self.board_widget.set_interactive(False)
        self.status_var.set("Lösung wird vorgespielt …")
        p = self.current
        gen = self._gen

        def step():
            if gen != self._gen:
                return
            if self.sol_idx >= len(p.solution):
                self._finish(solved=False)
                self.status_var.set("Das war die Lösung.")
                return
            mv = chess.Move.from_uci(p.solution[self.sol_idx])
            if not self.board.is_legal(mv):
                self._finish(solved=False)
                return
            is_user = self.sol_idx % 2 == 0
            san = self.board.san(mv)
            arrow = ARROW_BEST if is_user else ARROW_BAD
            self.board_widget.set_arrows(
                [(mv.from_square, mv.to_square, arrow)])
            self.board.push(mv)
            self.sol_idx += 1
            self.board_widget.set_position(self.board, mv)
            self._feed(("→ " if is_user else "   Antwort: ") + san,
                       "good" if is_user else "info")
            self.after(650, step)

        step()

    def skip(self) -> None:
        if self.current is None:
            return
        self._feed("Übersprungen – bleibt fällig.", "info")
        self.next_puzzle()

    # ------------------------------------------------------------ Statistik

    def show_stats(self) -> None:
        win = tk.Toplevel(self)
        win.title("Rätsel-Statistik: deine Schwächen")
        txt = ScrolledText(win, width=64, height=20,
                           font=("DejaVu Sans Mono", 10))
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        total = len(self.db.puzzles)
        due = len(self.db.due())
        lines = [f"Deck: {total} Rätsel · heute fällig: {due} · "
                 f"gemeistert: {self.db.mastered_count()}", ""]
        rows = self.db.theme_stats()
        if rows:
            lines.append("Erfolgsquote je Thema (schwächste zuerst):")
            lines.append("-" * 50)
            for theme, n, attempts, correct in rows:
                label = THEME_LABELS.get(theme, theme)
                rate = (f"{100.0 * correct / attempts:3.0f} %"
                        if attempts else "  – ")
                lines.append(f"  {rate}  {label}  "
                             f"({correct}/{attempts} Versuche, "
                             f"{n} Rätsel)")
            lines.append("")
            worst = [THEME_LABELS.get(t, t) for t, n, a, c in rows[:2]
                     if a >= 3 and (c / a) < 0.7]
            if worst:
                lines.append("→ Konzentriere dich auf: " + ", ".join(worst))
                lines.append("  (über den Themen-Filter gezielt trainieren)")
        else:
            lines.append("Noch keine Versuche – starte ein Training!")
        txt.insert(tk.END, "\n".join(lines))
        txt.configure(state=tk.DISABLED)
