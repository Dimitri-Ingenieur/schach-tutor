# -*- coding: utf-8 -*-
"""Trainingsmodus: gegen die Engine spielen und aus Fehlern lernen.

Ablauf pro Zug:
  1. Während der Nutzer nachdenkt, analysiert die Engine die Stellung
     bereits im Hintergrund (Vorab-Analyse → sofortiges Feedback).
  2. Nach dem Nutzerzug wird die neue Stellung analysiert.
  3. Aus beiden Bewertungen entsteht das Urteil (?! / ? / ??) samt
     Erklärung; bei Fehlern wird die Rücknahme angeboten.
"""

import os
import random
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

import chess

from analysis import (NormInfo, feedback_text, judge_move,
                      outcome_text, terminal_score, win_pct)
from board_widget import (ARROW_BAD, ARROW_BEST, ARROW_HINT, BoardWidget,
                          EvalBar)


class TrainingTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.board = chess.Board()
        self.user_color = chess.WHITE
        self.state = "idle"          # idle | user | busy | decide | engine
        self._gen = 0                # entwertet veraltete Async-Callbacks
        self._pre: Optional[dict] = None   # {"fen": ..., "infos": [...]}

        self._build_ui()
        self._feed("Neue Partie starten, um loszulegen.", "info")

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        left = ttk.Frame(self)
        left.pack(side=tk.LEFT, padx=8, pady=8, anchor="n")

        self.board_widget = BoardWidget(left, square=64,
                                        on_move=self._on_user_move)
        self.eval_bar = EvalBar(left, height=self.board_widget.winfo_reqheight())
        self.eval_bar.pack(side=tk.LEFT, padx=(0, 6))
        self.board_widget.pack(side=tk.LEFT)
        self.board_widget.set_interactive(False)

        right = ttk.Frame(self)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

        row1 = ttk.Frame(right)
        row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row1, text="Neue Partie als:").pack(side=tk.LEFT)
        self.color_var = tk.StringVar(value="Weiß")
        ttk.Combobox(row1, textvariable=self.color_var, width=8,
                     state="readonly",
                     values=["Weiß", "Schwarz", "Zufall"]).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(row1, text="Start", command=self.new_game).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(row1, text="Brett drehen",
                   command=self._flip).pack(side=tk.LEFT, padx=4)

        row2 = ttk.Frame(right)
        row2.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(row2, text="Tipp", command=self.hint).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(row2, text="Zug zurücknehmen",
                   command=self.undo_pair).pack(side=tk.LEFT, padx=4)

        row3 = ttk.Frame(right)
        row3.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row3, text="Spielstärke (Knoten):").pack(side=tk.LEFT)
        self.play_nodes_var = tk.IntVar(
            value=int(self.app.settings.get("play_nodes", 200)))
        ttk.Spinbox(row3, from_=1, to=1000000, width=8,
                    textvariable=self.play_nodes_var).pack(
            side=tk.LEFT, padx=(4, 12))
        ttk.Label(row3, text="Analyse (Knoten):").pack(side=tk.LEFT)
        self.analysis_nodes_var = tk.IntVar(
            value=int(self.app.settings.get("analysis_nodes", 600)))
        ttk.Spinbox(row3, from_=1, to=1000000, width=8,
                    textvariable=self.analysis_nodes_var).pack(
            side=tk.LEFT, padx=4)

        row4 = ttk.Frame(right)
        row4.pack(fill=tk.X, pady=(0, 4))
        self.live_var = tk.BooleanVar(
            value=bool(self.app.settings.get("live_eval", False)))
        ttk.Checkbutton(row4, text="Live-Bewertung während des Nachdenkens",
                        variable=self.live_var).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(right, textvariable=self.status_var,
                  font=("DejaVu Sans", 10, "bold")).pack(
            fill=tk.X, pady=(4, 4))

        self.feedback = ScrolledText(right, width=52, height=20,
                                     wrap=tk.WORD, state=tk.DISABLED,
                                     font=("DejaVu Sans", 10))
        self.feedback.pack(fill=tk.BOTH, expand=True)
        self.feedback.tag_configure("good", foreground="#1a7a1a")
        self.feedback.tag_configure("warn", foreground="#b06f00")
        self.feedback.tag_configure("bad", foreground="#b02a1e",
                                    font=("DejaVu Sans", 10, "bold"))
        self.feedback.tag_configure("info", foreground="#444444")
        self.feedback.tag_configure("line", foreground="#333333",
                                    font=("DejaVu Sans Mono", 9))

        self.decide_frame = ttk.Frame(right)
        ttk.Label(self.decide_frame,
                  text="Das war ein Fehler – noch einmal versuchen?").pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(self.decide_frame, text="Zurücknehmen",
                   command=self._takeback).pack(side=tk.LEFT, padx=4)
        ttk.Button(self.decide_frame, text="Weiterspielen",
                   command=self._continue_after_decide).pack(
            side=tk.LEFT, padx=4)

    # ------------------------------------------------------------ Helfer

    def _feed(self, text: str, tag: str = "info") -> None:
        self.feedback.configure(state=tk.NORMAL)
        self.feedback.insert(tk.END, text + "\n", tag)
        self.feedback.see(tk.END)
        self.feedback.configure(state=tk.DISABLED)

    def _feed_sep(self) -> None:
        self._feed("─" * 44, "info")

    def _hub(self):
        hub = self.app.hub
        if hub is None:
            messagebox.showwarning(
                "Engine fehlt",
                "Keine Engine konfiguriert. Bitte unter Datei → "
                "Einstellungen den Pfad zu LC0 angeben.")
        return hub

    def _sync_settings(self) -> None:
        try:
            self.app.settings["play_nodes"] = int(self.play_nodes_var.get())
            self.app.settings["analysis_nodes"] = int(
                self.analysis_nodes_var.get())
            self.app.settings["live_eval"] = bool(self.live_var.get())
        except (tk.TclError, ValueError):
            pass

    def _update_eval(self, info: NormInfo) -> None:
        cp = info.score.cp(chess.WHITE)
        self.eval_bar.set_eval(win_pct(cp), info.score.fmt(chess.WHITE))

    def _flip(self) -> None:
        self.board_widget.set_flipped(not self.board_widget.flipped)

    # ------------------------------------------------------------ Partie

    def new_game(self) -> None:
        if self._hub() is None:
            return
        choice = self.color_var.get()
        if choice == "Zufall":
            self.user_color = random.choice([chess.WHITE, chess.BLACK])
        else:
            self.user_color = chess.WHITE if choice == "Weiß" else chess.BLACK
        self.start_from(chess.Board(), self.user_color)
        farbe = "Weiß" if self.user_color == chess.WHITE else "Schwarz"
        self._feed(f"Neue Partie – du spielst {farbe}.", "info")

    def start_from(self, board: chess.Board, user_color: bool) -> None:
        """Startpunkt für neue Partien und für 'Stellung trainieren'."""
        self._gen += 1
        self.board = board.copy()
        self.user_color = user_color
        self._pre = None
        self.decide_frame.pack_forget()
        self.board_widget.set_flipped(user_color == chess.BLACK)
        last = self.board.move_stack[-1] if self.board.move_stack else None
        self.board_widget.set_position(self.board, last)
        self.board_widget.clear_arrows()
        if self.board.turn == user_color:
            self._begin_user_turn()
        else:
            self._engine_turn()

    def _begin_user_turn(self) -> None:
        self.state = "user"
        self.board_widget.set_interactive(True)
        farbe = "Weiß" if self.board.turn == chess.WHITE else "Schwarz"
        self.status_var.set(f"Du bist am Zug ({farbe}).")
        self._start_preanalysis()

    def _start_preanalysis(self) -> None:
        hub = self.app.hub
        if hub is None:
            return
        gen = self._gen
        fen = self.board.fen()
        if self._pre and self._pre["fen"] == fen:
            if self.live_var.get():
                self._update_eval(self._pre["infos"][0])
            return
        self._sync_settings()
        nodes = self.app.settings["analysis_nodes"]

        def done(infos, err):
            if gen != self._gen or err or not infos:
                return
            self._pre = {"fen": fen, "infos": infos}
            if self.live_var.get() and self.state == "user":
                self._update_eval(infos[0])

        hub.analyse(self.board, nodes, multipv=1, cb=done)

    # ------------------------------------------------------------ Nutzerzug

    def _on_user_move(self, move: chess.Move) -> None:
        if self.state != "user" or not self.board.is_legal(move):
            return
        hub = self._hub()
        if hub is None:
            return
        self.state = "busy"
        self.board_widget.set_interactive(False)
        self.status_var.set("Analysiere deinen Zug …")
        gen = self._gen
        fen = self.board.fen()

        if self._pre and self._pre["fen"] == fen:
            self._apply_user_move(move, self._pre["infos"][0], gen)
            return

        self._sync_settings()
        nodes = self.app.settings["analysis_nodes"]

        def done(infos, err):
            if gen != self._gen:
                return
            if err or not infos:
                self._engine_error(err)
                return
            self._apply_user_move(move, infos[0], gen)

        hub.analyse(self.board, nodes, multipv=1, cb=done)

    def _apply_user_move(self, move: chess.Move, info_before: NormInfo,
                         gen: int) -> None:
        if gen != self._gen:
            return
        board_before = self.board.copy()
        self.board.push(move)
        self.board_widget.set_position(self.board, move)
        self.board_widget.clear_arrows()

        ts = terminal_score(self.board)
        if ts is not None:
            info_after = NormInfo(score=ts, pv=[])
            self._finish_judgement(move, board_before, info_before,
                                   info_after, gen)
            return

        self._sync_settings()
        nodes = self.app.settings["analysis_nodes"]

        def done(infos, err):
            if gen != self._gen:
                return
            if err or not infos:
                self._engine_error(err)
                return
            self._finish_judgement(move, board_before, info_before,
                                   infos[0], gen)

        self.app.hub.analyse(self.board, nodes, multipv=1, cb=done)

    def _finish_judgement(self, move: chess.Move, board_before: chess.Board,
                          info_before: NormInfo, info_after: NormInfo,
                          gen: int) -> None:
        if gen != self._gen:
            return
        mj = judge_move(board_before, move, info_before, info_after)
        self._last_mj = mj
        self._last_info_before = info_before
        self._update_eval(info_after)

        self._feed_sep()
        tag = "info"
        if mj.judgement == "??":
            tag = "bad"
        elif mj.judgement in ("?", "?!"):
            tag = "warn"
        elif mj.best_san and mj.san == mj.best_san:
            tag = "good"
        self._feed(feedback_text(mj), tag)

        if mj.judgement and info_after.pv:
            r0 = info_after.pv[0]
            self.board_widget.set_arrows(
                [(r0.from_square, r0.to_square, ARROW_BAD)])

        if self.board.is_game_over(claim_draw=True):
            self._finish_game()
            return

        self._sync_settings()
        if (mj.judgement in ("?", "??")
                and self.app.settings.get("offer_takeback", True)):
            self.state = "decide"
            self.status_var.set("Fehler erkannt – zurücknehmen oder weiter?")
            self.decide_frame.pack(fill=tk.X, pady=4)
            return

        self._engine_turn()

    # ------------------------------------------------------------ Rücknahme

    def _takeback(self) -> None:
        if self.state != "decide":
            return
        self.decide_frame.pack_forget()
        self.board.pop()
        last = self.board.move_stack[-1] if self.board.move_stack else None
        self.board_widget.set_position(self.board, last)
        info_before = getattr(self, "_last_info_before", None)
        if info_before and info_before.pv:
            best = info_before.pv[0]
            self.board_widget.set_arrows(
                [(best.from_square, best.to_square, ARROW_BEST)])
        self._feed("Zug zurückgenommen – versuch es noch einmal "
                   "(grüner Pfeil = Engine-Empfehlung).", "info")
        self._pre = {"fen": self.board.fen(),
                     "infos": [info_before]} if info_before else None
        self._begin_user_turn()

    def _continue_after_decide(self) -> None:
        if self.state != "decide":
            return
        self.decide_frame.pack_forget()
        self._engine_turn()

    def undo_pair(self) -> None:
        """Letztes Zugpaar zurücknehmen (nur wenn der Nutzer am Zug ist)."""
        if self.state not in ("user", "idle") or not self.board.move_stack:
            return
        self._gen += 1
        pops = 2 if len(self.board.move_stack) >= 2 else 1
        for _ in range(pops):
            self.board.pop()
        self._pre = None
        last = self.board.move_stack[-1] if self.board.move_stack else None
        self.board_widget.set_position(self.board, last)
        self.board_widget.clear_arrows()
        self._feed("Zugpaar zurückgenommen.", "info")
        if self.board.turn == self.user_color:
            self._begin_user_turn()
        else:
            self._engine_turn()

    # ------------------------------------------------------------ Engine

    def _engine_turn(self) -> None:
        hub = self._hub()
        if hub is None:
            self.state = "idle"
            return
        self.state = "engine"
        self.board_widget.set_interactive(False)
        self.status_var.set("Engine denkt …")
        gen = self._gen

        book_move = self._book_move()
        if book_move is not None:
            self.after(150, lambda: self._engine_moved(book_move, None, gen,
                                                       from_book=True))
            return

        self._sync_settings()
        nodes = self.app.settings["play_nodes"]

        def done(move, err):
            if gen != self._gen:
                return
            self._engine_moved(move, err, gen)

        hub.play(self.board, nodes, cb=done)

    def _engine_moved(self, move: Optional[chess.Move],
                      err: Optional[Exception], gen: int,
                      from_book: bool = False) -> None:
        if gen != self._gen:
            return
        if err is not None or move is None:
            self._engine_error(err)
            return
        if not self.board.is_legal(move):
            self._engine_error(ValueError(f"Illegaler Engine-Zug: {move}"))
            return
        san = self.board.san(move)
        self.board.push(move)
        self.board_widget.set_position(self.board, move)
        src = " (Buch)" if from_book else ""
        self._feed(f"Engine spielt {san}{src}", "info")
        if self.board.is_game_over(claim_draw=True):
            self._finish_game()
            return
        self._begin_user_turn()

    def _book_move(self) -> Optional[chess.Move]:
        path = os.path.expanduser(
            self.app.settings.get("book_path", "").strip())
        if not path or not os.path.isfile(path) or self.board.ply() > 24:
            return None
        try:
            import chess.polyglot
            with chess.polyglot.open_reader(path) as reader:
                return reader.weighted_choice(self.board).move
        except (IOError, IndexError, ValueError):
            return None

    def _finish_game(self) -> None:
        self.state = "idle"
        self.board_widget.set_interactive(False)
        text = outcome_text(self.board) or "Partie beendet."
        self.status_var.set(text)
        self._feed(f"Partie beendet: {text}", "info")

    def _engine_error(self, err: Optional[Exception]) -> None:
        self.state = "idle"
        self.status_var.set("Engine-Fehler.")
        messagebox.showerror(
            "Engine-Fehler",
            f"Die Engine hat einen Fehler gemeldet:\n{err}\n\n"
            "Prüfe Pfad, Weights und Backend unter Datei → Einstellungen.")

    # ------------------------------------------------------------ Tipp

    def hint(self) -> None:
        if self.state != "user":
            return
        hub = self._hub()
        if hub is None:
            return
        gen = self._gen
        fen = self.board.fen()

        def show(infos):
            if gen != self._gen or not infos or not infos[0].pv:
                return
            best = infos[0].pv[0]
            san = self.board.san(best)
            self.board_widget.set_arrows(
                [(best.from_square, best.to_square, ARROW_HINT)])
            pov = self.board.turn
            self._feed(f"Tipp: {san}   ({infos[0].score.fmt(pov)})", "info")

        if self._pre and self._pre["fen"] == fen:
            show(self._pre["infos"])
            return

        self._sync_settings()
        nodes = self.app.settings["analysis_nodes"]

        def done(infos, err):
            if gen != self._gen or err:
                return
            self._pre = {"fen": fen, "infos": infos}
            show(infos)

        hub.analyse(self.board, nodes, multipv=1, cb=done)
