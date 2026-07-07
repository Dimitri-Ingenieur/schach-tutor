# -*- coding: utf-8 -*-
"""Beobachten-Tab: laufende Partien von Lichess (live) / Chess.com (Daily).

Architektur wie im Rest der App: Netzwerk läuft in einem Hintergrund-Thread,
alle GUI-Updates werden über die Callback-Queue (`app.dispatch`) in den
Tk-Mainloop gehoben; ein Generationszähler entwertet veraltete Ereignisse.
Die optionale Live-Bewertung nutzt den vorhandenen EngineHub.
"""

import io
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

import chess
import chess.pgn

import live
from analysis import win_pct
from board_widget import BoardWidget, EvalBar

POLL_SECONDS = 12          # Chess.com-Daily-Abfrageintervall
RECONNECT_SECONDS = 3      # Pause vor Lichess-Reconnect


class LiveTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.state = "idle"            # idle | watching
        self._gen = 0
        self._stop: Optional[threading.Event] = None
        self.board = chess.Board()
        self.watch_user = ""
        self._eval_gen = 0
        self._build_ui()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(top, text="Quelle:").pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value="Lichess")
        ttk.Combobox(top, textvariable=self.source_var, width=17,
                     state="readonly",
                     values=["Lichess", "Chess.com (Daily)"]).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(top, text="Nutzer:").pack(side=tk.LEFT, padx=(10, 0))
        self.user_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.user_var,
                  width=16).pack(side=tk.LEFT, padx=4)
        self.watch_btn = ttk.Button(top, text="Beobachten",
                                    command=self.start_watching)
        self.watch_btn.pack(side=tk.LEFT, padx=(8, 4))
        self.stop_btn = ttk.Button(top, text="Stopp", state=tk.DISABLED,
                                   command=self.stop_watching)
        self.stop_btn.pack(side=tk.LEFT)
        self.eval_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Live-Bewertung (LC0)",
                        variable=self.eval_var).pack(side=tk.LEFT,
                                                     padx=(14, 0))

        self.status_var = tk.StringVar(
            value="Nutzernamen eingeben und „Beobachten“ drücken.")
        ttk.Label(self, textvariable=self.status_var,
                  font=("DejaVu Sans", 11, "bold")).pack(fill=tk.X, padx=8)

        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, anchor="n")
        self.board_widget = BoardWidget(left, square=60, interactive=False)
        self.board_widget.pack(side=tk.LEFT)
        self.eval_bar = EvalBar(left,
                                height=self.board_widget.winfo_reqheight())
        self.eval_bar.pack(side=tk.LEFT, padx=(6, 0), anchor="n")

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        self.players_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.players_var,
                  font=("DejaVu Sans", 10, "bold")).pack(fill=tk.X)
        self.clock_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.clock_var).pack(fill=tk.X)
        self.eval_line_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.eval_line_var,
                  wraplength=420, justify=tk.LEFT).pack(fill=tk.X, pady=2)
        self.log = ScrolledText(right, height=16, wrap=tk.WORD,
                                font=("DejaVu Sans", 10), state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)

    def _feed(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    # ------------------------------------------------------- Steuerung

    def start_watching(self) -> None:
        user = self.user_var.get().strip()
        if not user:
            self.status_var.set("Bitte einen Nutzernamen eingeben.")
            return
        self.stop_watching()
        self._gen += 1
        gen = self._gen
        self._stop = threading.Event()
        self.state = "watching"
        self.watch_user = user
        self.watch_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)
        source = self.source_var.get()
        self.status_var.set(f"Suche laufende Partie von {user} …")
        target = (self._watch_lichess if source == "Lichess"
                  else self._watch_chesscom)
        threading.Thread(target=target, args=(user, gen, self._stop),
                         daemon=True, name="LiveWatch").start()

    def stop_watching(self) -> None:
        self._gen += 1
        if self._stop is not None:
            self._stop.set()
        if self.state == "watching":
            self.status_var.set("Beobachtung gestoppt.")
        self.state = "idle"
        self.watch_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    # ------------------------------------------------- Lichess (Stream)

    def _watch_lichess(self, user: str, gen: int, stop) -> None:
        dispatch = self.app.dispatch
        try:
            game = live.lichess_current_game(user)
        except Exception as exc:
            dispatch(lambda e=exc: self._fail(
                gen, f"Keine laufende Partie gefunden ({e})."))
            return
        dispatch(lambda: self._apply_snapshot(gen, game))
        gid = str(game.get("id", ""))
        while not stop.is_set():
            try:
                live.stream_game(
                    gid,
                    on_event=lambda d: dispatch(
                        lambda d=d: self._apply_stream_event(gen, d)),
                    stop=stop)
            except Exception as exc:
                if stop.is_set():
                    return
                dispatch(lambda e=exc: self._note(
                    gen, f"Verbindung unterbrochen ({e}) – neuer Versuch …"))
                time.sleep(RECONNECT_SECONDS)
                continue
            if stop.is_set():
                return
            # Stream endete ohne Stopp: Partie vermutlich vorbei — kurz
            # prüfen, ob eine neue läuft, sonst sauber beenden.
            try:
                nxt = live.lichess_current_game(user)
                if str(nxt.get("id", "")) not in ("", gid):
                    gid = str(nxt.get("id"))
                    dispatch(lambda: self._apply_snapshot(gen, nxt))
                    continue
            except Exception:
                pass
            dispatch(lambda: self._finished(gen))
            return

    def _apply_snapshot(self, gen: int, game: dict) -> None:
        """Erster Stand aus /current-game (Züge als SAN-Liste)."""
        if gen != self._gen:
            return
        board, sans = live.board_from_san(game.get("moves", ""))
        self.board = board
        players = game.get("players", {})

        def pname(side):
            p = players.get(side, {})
            u = p.get("user", {})
            name = u.get("name") or p.get("name") or p.get("aiLevel", "?")
            rating = p.get("rating")
            return f"{name} ({rating})" if rating else str(name)

        white, black = pname("white"), pname("black")
        self.players_var.set(f"Weiß: {white}   ·   Schwarz: {black}")
        me_black = self.watch_user.lower() in str(
            players.get("black", {})).lower()
        self.board_widget.set_flipped(me_black)
        last = board.peek() if board.move_stack else None
        self.board_widget.set_position(board, last)
        speed = game.get("speed", "")
        self.status_var.set(f"Live: {white} – {black} ({speed})")
        if sans:
            self._feed("Bisher: " + " ".join(sans))
        self._request_eval(board)

    def _apply_stream_event(self, gen: int, d: dict) -> None:
        """Eine Zeile aus dem Lichess-Stream (voll oder {fen, lm, wc, bc})."""
        if gen != self._gen:
            return
        fen = d.get("fen")
        if not fen:
            return
        try:
            new_board = chess.Board(fen)
        except ValueError:
            return
        lm = d.get("lm") or d.get("lastMove") or ""
        last_move = None
        san = ""
        try:
            mv = chess.Move.from_uci(lm) if lm else None
            if mv is not None:
                if self.board.is_legal(mv):
                    san = self.board.san(mv)
                last_move = mv
        except ValueError:
            pass
        self.board = new_board
        self.board_widget.set_position(new_board, last_move)
        if san:
            no = new_board.fullmove_number
            dots = "…" if new_board.turn == chess.WHITE else "."
            self._feed(f"{no if dots == '…' else no}{dots} {san}")
        wc, bc = d.get("wc"), d.get("bc")
        if wc is not None or bc is not None:
            self.clock_var.set(f"Uhr — Weiß: {live.fmt_clock(wc)}   "
                               f"Schwarz: {live.fmt_clock(bc)}")
        status = d.get("status", {})
        if isinstance(status, dict) and status.get("name") not in (
                None, "started", "created"):
            self._finished(gen)
            return
        self._request_eval(new_board)

    # --------------------------------------------- Chess.com (Polling)

    def _watch_chesscom(self, user: str, gen: int, stop) -> None:
        dispatch = self.app.dispatch
        last_fen = None
        while not stop.is_set():
            try:
                games = live.chesscom_daily_games(user)
            except Exception as exc:
                dispatch(lambda e=exc: self._fail(
                    gen, f"Abruf fehlgeschlagen ({e})."))
                return
            if not games:
                dispatch(lambda: self._fail(
                    gen, "Keine laufende Daily-Partie gefunden. (Live-"
                         "Blitz/Rapid stellt Chess.com nicht öffentlich "
                         "bereit.)"))
                return
            game = max(games, key=lambda g: g.get("last_activity", 0))
            fen = game.get("fen", "")
            if fen and fen != last_fen:
                last_fen = fen
                dispatch(lambda g=dict(game), n=len(games):
                         self._apply_daily(gen, g, n))
            for _ in range(POLL_SECONDS * 4):
                if stop.is_set():
                    return
                time.sleep(0.25)

    def _apply_daily(self, gen: int, game: dict, n_games: int) -> None:
        if gen != self._gen:
            return
        try:
            board = chess.Board(game.get("fen", chess.STARTING_FEN))
        except ValueError:
            return
        self.board = board
        white = str(game.get("white", "")).rsplit("/", 1)[-1]
        black = str(game.get("black", "")).rsplit("/", 1)[-1]
        self.players_var.set(f"Weiß: {white}   ·   Schwarz: {black}")
        self.board_widget.set_flipped(
            self.watch_user.lower() == black.lower())
        self.board_widget.set_position(board)
        extra = (f" · {n_games} laufende Daily-Partien, zeige die aktivste"
                 if n_games > 1 else "")
        turn = "Weiß" if board.turn == chess.WHITE else "Schwarz"
        self.status_var.set(f"Daily: {white} – {black} · am Zug: {turn} · "
                            f"Aktualisierung alle {POLL_SECONDS} s{extra}")
        pgn = game.get("pgn") or ""
        if pgn:
            try:
                g = chess.pgn.read_game(io.StringIO(pgn))
                if g is not None:
                    b = g.board()
                    sans = []
                    for mv in g.mainline_moves():
                        sans.append(b.san(mv))
                        b.push(mv)
                    self.log.configure(state=tk.NORMAL)
                    self.log.delete("1.0", tk.END)
                    self.log.configure(state=tk.DISABLED)
                    self._feed("Bisher: " + " ".join(sans))
            except Exception:
                pass
        self._request_eval(board)

    # ------------------------------------------------------ Bewertung

    def _request_eval(self, board: chess.Board) -> None:
        hub = self.app.hub
        if not self.eval_var.get() or hub is None or board.is_game_over():
            return
        self._eval_gen += 1
        egen = self._eval_gen
        gen = self._gen
        nodes = int(self.app.settings.get("analysis_nodes", 600))

        def done(infos, err):
            if gen != self._gen or egen != self._eval_gen:
                return
            if err or not infos:
                return
            info = infos[0]
            wp = win_pct(info.score.cp(chess.WHITE))
            self.eval_bar.set_eval(wp, info.score.fmt(chess.WHITE))
            from analysis import san_line
            line = san_line(board, info.pv, 6)
            self.eval_line_var.set(
                f"LC0: {info.score.fmt(chess.WHITE)}   {line}")

        hub.analyse(board.copy(), nodes, multipv=1, cb=done)

    # ------------------------------------------------------- Abschluss

    def _note(self, gen: int, text: str) -> None:
        if gen != self._gen:
            return
        self._feed(text)

    def _fail(self, gen: int, text: str) -> None:
        if gen != self._gen:
            return
        self.status_var.set(text)
        self.stop_watching()

    def _finished(self, gen: int) -> None:
        if gen != self._gen:
            return
        self.status_var.set("Partie beendet.")
        self._feed("— Partie beendet. Über den Import im Gegner-Analyse-"
                   "Tab lässt sie sich gleich mit auswerten. —")
        self.stop_watching()
