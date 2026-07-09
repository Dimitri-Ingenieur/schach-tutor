# -*- coding: utf-8 -*-
"""Beobachten-Tab: laufende Partien von Lichess (live) / Chess.com (Daily).

Wichtige Eigenheit der Lichess-API (in der Praxis verifiziert): Der
Positions-Stream einer Partie liefert nach der Beschreibungszeile die
KOMPLETTE Zugfolge von Zug 1 an und geht dann nahtlos in Echtzeit über.
Der Tab spielt diese Aufholphase deshalb still auf einem separaten
Replay-Brett nach (keine Brett-Sprünge, keine Log-Flut, keine
Engine-Aufrufe) und schaltet erst ab dem aktuellen Stand auf
Live-Anzeige um. Nach einem Reconnect passiert dasselbe automatisch.

Architektur wie im Rest der App: Netzwerk im Hintergrund-Thread, alle
GUI-Updates über die Callback-Queue (`app.dispatch`), Generationszähler
gegen veraltete Ereignisse, Live-Bewertung entprellt über den EngineHub.
"""

import io
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import List, Optional

import chess
import chess.pgn

import live
from analysis import san_line, win_pct
from board_widget import BoardWidget, EvalBar

POLL_SECONDS = 12          # Chess.com-Daily-Abfrageintervall
RECONNECT_SECONDS = 3      # Pause vor Lichess-Reconnect
EVAL_DEBOUNCE_MS = 400     # Live-Bewertung erst, wenn kurz Ruhe ist
EVAL_MAX_WAIT_MS = 1500    # ...aber spätestens nach dieser Zeit zwingend (sonst verhungert der Entpreller bei schnellem Spiel)


def _board_from_fen(fen: str) -> Optional[chess.Board]:
    """FEN robust parsen (auch ohne Zähler-Felder)."""
    for candidate in (fen, fen + " 0 1", fen + " - - 0 1"):
        try:
            return chess.Board(candidate)
        except ValueError:
            continue
    return None


class LiveTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.state = "idle"            # idle | watching
        self._gen = 0
        self._stop: Optional[threading.Event] = None
        self.watch_user = ""

        self.board = chess.Board()     # angezeigter Stand
        self._sboard: Optional[chess.Board] = None   # Replay-Brett (Stream)
        self._initial_fen = chess.STARTING_FEN
        self._catchup_to = 0           # bis zu diesem Ply still aufholen
        self._live = False             # Aufholphase abgeschlossen?
        self._pending: List[str] = []  # still aufgeholte Züge (für 1 Zeile)

        self._eval_gen = 0
        self._eval_after: Optional[str] = None
        self._last_eval_at = 0.0
        self._shown_history = False
        self._delay_noted = False
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

    def _clear_log(self) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
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
        self._eval_gen += 1
        self.state = "watching"
        self.watch_user = user
        self.watch_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._clear_log()
        self.board = chess.Board()
        self._sboard = None
        self._live = False
        self._catchup_to = 0
        self._pending = []
        self._shown_history = False
        self._delay_noted = False
        source = self.source_var.get()
        self.status_var.set(f"Suche laufende Partie von {user} …")
        target = (self._watch_lichess if source == "Lichess"
                  else self._watch_chesscom)
        threading.Thread(target=target, args=(user, gen, self._stop),
                         daemon=True, name="LiveWatch").start()

    def stop_watching(self) -> None:
        self._gen += 1
        # _eval_gen absichtlich NICHT hier hochzählen: eine gerade erst
        # losgeschickte Bewertung (z. B. die Schlussstellung aus
        # _finished()) soll noch ankommen dürfen. Eine neue Beobachtung
        # invalidiert über start_watching() ohnehin sauber.
        if self._eval_after is not None:
            try:
                self.after_cancel(self._eval_after)
            except ValueError:
                pass
            self._eval_after = None
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
            # Vor jedem (Re-)Connect: still bis zum bekannten Stand aufholen.
            dispatch(lambda: self._prepare_stream(gen))
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
            # Stream endete ohne Stopp: entweder Partie vorbei oder die
            # Verbindung war nur still (Timeout) → Stand prüfen.
            try:
                nxt = live.lichess_current_game(user)
            except Exception:
                dispatch(lambda: self._finished(gen))
                return
            nid = str(nxt.get("id", ""))
            running = str(nxt.get("status", "")) in ("started", "created")
            if nid == gid and running:
                time.sleep(1.0)          # Denkpause/Timeout → reconnect
                continue
            if nid and nid != gid and running:
                gid = nid                # er spielt schon die nächste
                dispatch(lambda: self._apply_snapshot(gen, nxt))
                continue
            dispatch(lambda: self._finished(gen))
            return

    def _prepare_stream(self, gen: int) -> None:
        """Replay-Brett zurücksetzen; bis zum aktuellen Stand still bleiben."""
        if gen != self._gen:
            return
        self._sboard = None
        self._live = False
        self._pending = []
        self._catchup_to = max(self._catchup_to, self.board.ply())

    def _apply_snapshot(self, gen: int, game: dict) -> None:
        """Snapshot aus /current-game: Metadaten + sofortige Brett-Anzeige."""
        if gen != self._gen:
            return
        board, sans = live.board_from_san(game.get("moves", ""))
        initial = game.get("initialFen", "")
        if initial and initial != "startpos":
            b0 = _board_from_fen(initial)
            if b0 is not None:
                self._initial_fen = b0.fen()
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
        self.eval_bar.set_flipped(me_black)
        self.board = board
        self._catchup_to = max(self._catchup_to, board.ply())
        last = board.peek() if board.move_stack else None
        self.board_widget.set_position(board, last)
        speed = game.get("speed", "")
        self.status_var.set(f"Live: {white} – {black} ({speed})")
        if not self._delay_noted:
            self._delay_noted = True
            self._feed("Hinweis: Lichess verzögert den öffentlichen "
                       "Zuschauer-Stream absichtlich um 3 Züge (Schutz "
                       "vor Cheat-Bots). Die Anzeige hinkt dem echten "
                       "Spiel also bewusst hinterher; am Partieende "
                       "holt sie auf.")
        self._request_eval_debounced()

    def _apply_stream_event(self, gen: int, d: dict) -> None:
        """Eine Zeile aus dem Lichess-Stream.

        Erste Zeile: Beschreibung (id/players/…) → nur Metadaten.
        Danach: die komplette Zugfolge ab Zug 1, dann live weiter —
        bis `_catchup_to` wird still auf dem Replay-Brett nachgespielt.
        """
        if gen != self._gen:
            return

        # Beschreibungszeile erkennen (kein reines Zug-Update)
        if "id" in d and ("players" in d or "initialFen" in d
                          or "speed" in d or "variant" in d):
            initial = d.get("initialFen", "")
            if initial and initial != "startpos":
                b0 = _board_from_fen(initial)
                if b0 is not None:
                    self._initial_fen = b0.fen()
            turns = d.get("turns")
            if isinstance(turns, int):
                self._catchup_to = max(self._catchup_to, turns)
            if self._status_finished(d.get("status")):
                self._finished(gen)
            return

        fen = d.get("fen")
        if not fen:
            return
        if self._sboard is None:
            self._sboard = chess.Board(self._initial_fen)

        # Zug einordnen: normal weiterschieben oder notfalls resynchronisieren
        lm = d.get("lm") or d.get("lastMove") or ""
        san = ""
        move_no = self._sboard.fullmove_number
        was_black = self._sboard.turn == chess.BLACK
        mv = None
        try:
            mv = chess.Move.from_uci(lm) if lm else None
        except ValueError:
            mv = None
        if mv is not None and self._sboard.is_legal(mv):
            san = self._sboard.san(mv)
            self._sboard.push(mv)
            if self._sboard.board_fen() != fen.split()[0]:
                # Stream und Replay auseinandergelaufen → hart übernehmen
                resync = _board_from_fen(fen)
                if resync is not None:
                    self._sboard = resync
                san = ""
        else:
            resync = _board_from_fen(fen)
            if resync is None:
                return
            self._sboard = resync

        entry = ""
        if san:
            entry = (f"{move_no}… {san}" if was_black
                     else f"{move_no}. {san}")

        caught_up = self._sboard.ply() >= self._catchup_to

        if not caught_up:
            # Aufholphase: still sammeln, Brett/Engine/Uhr nicht anfassen.
            if entry:
                self._pending.append(entry)
            return

        if not self._live:
            self._live = True
            if entry:
                self._pending.append(entry)
                entry = ""
            if self._pending and not self._shown_history:
                self._feed("Bisher: " + "  ".join(self._pending))
                self._shown_history = True
            self._pending = []

        self.board = self._sboard.copy()
        self.board_widget.set_position(self.board, mv)
        if entry:
            self._feed(entry)
        wc, bc = d.get("wc"), d.get("bc")
        if wc is not None or bc is not None:
            self.clock_var.set(f"Uhr — Weiß: {live.fmt_clock(wc)}   "
                               f"Schwarz: {live.fmt_clock(bc)}")
        if self._status_finished(d.get("status")):
            self._finished(gen)
            return
        self._request_eval_debounced()

    @staticmethod
    def _status_finished(status) -> bool:
        if isinstance(status, dict):
            return status.get("name") not in (None, "started", "created")
        if isinstance(status, str):
            return status not in ("", "started", "created")
        return False

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
        board = _board_from_fen(game.get("fen", chess.STARTING_FEN))
        if board is None:
            return
        self.board = board
        white = str(game.get("white", "")).rsplit("/", 1)[-1]
        black = str(game.get("black", "")).rsplit("/", 1)[-1]
        self.players_var.set(f"Weiß: {white}   ·   Schwarz: {black}")
        black_side = self.watch_user.lower() == black.lower()
        self.board_widget.set_flipped(black_side)
        self.eval_bar.set_flipped(black_side)
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
                    parts = []
                    for mv in g.mainline_moves():
                        prefix = (f"{b.fullmove_number}. "
                                  if b.turn == chess.WHITE else "")
                        parts.append(prefix + b.san(mv))
                        b.push(mv)
                    self._clear_log()
                    self._feed("Bisher: " + " ".join(parts))
            except Exception:
                pass
        self._request_eval_debounced()

    # ------------------------------------------------------ Bewertung

    def _request_eval_debounced(self) -> None:
        """Bewertet erst, wenn kurz keine neue Stellung mehr kam.

        Reines "warte auf Ruhe" kann bei Blitz/Bullet (oder schnell
        heruntergespielter Eröffnungstheorie) verhungern, wenn Züge
        durchgehend schneller als EVAL_DEBOUNCE_MS aufeinanderfolgen —
        dann feuert der Timer nie, die Bewertung bliebe für die ganze
        Beobachtung beim Startwert stehen. Deshalb zusätzlich eine harte
        Obergrenze: läuft seit der letzten tatsächlichen Bewertung schon
        länger als EVAL_MAX_WAIT_MS, wird sofort ausgewertet statt erneut
        zu verschieben.
        """
        if self._eval_after is not None:
            try:
                self.after_cancel(self._eval_after)
            except ValueError:
                pass
            self._eval_after = None
        overdue = (time.monotonic() - self._last_eval_at) * 1000
        if overdue >= EVAL_MAX_WAIT_MS:
            self._request_eval()
        else:
            self._eval_after = self.after(EVAL_DEBOUNCE_MS,
                                          self._request_eval)

    @staticmethod
    def _terminal_label(board: chess.Board) -> str:
        if board.is_checkmate():
            return "Matt"
        if board.is_stalemate():
            return "Patt"
        return "Remis"

    def _request_eval(self) -> None:
        self._eval_after = None
        self._last_eval_at = time.monotonic()
        hub = self.app.hub
        board = self.board
        if not self.eval_var.get() or hub is None or self.state != "watching":
            return
        if board.is_game_over():
            self.eval_bar.set_eval(50.0, self._terminal_label(board))
            self.eval_line_var.set("Partie beendet.")
            return
        self._eval_gen += 1
        egen = self._eval_gen
        nodes = int(self.app.settings.get("analysis_nodes", 600))
        snapshot = board.copy()

        def done(infos, err):
            # Nur an _eval_gen prüfen, nicht an _gen: _gen ändert sich
            # auch bei einem simplen Stopp (soll die letzte, schon
            # unterwegs befindliche Bewertung nicht verwerfen — siehe
            # _finished()). _eval_gen wird ausschließlich von einer
            # wirklich neuen Beobachtung (start_watching) hochgezählt,
            # das reicht als Schutz gegen ein verspätet eintreffendes
            # Ergebnis einer alten Partie.
            if egen != self._eval_gen:
                return
            if err or not infos:
                return
            info = infos[0]
            wp = win_pct(info.score.cp(chess.WHITE))
            self.eval_bar.set_eval(wp, info.score.fmt(chess.WHITE))
            line = san_line(snapshot, info.pv, 6)
            self.eval_line_var.set(
                f"LC0: {info.score.fmt(chess.WHITE)}   {line}")

        hub.analyse(snapshot, nodes, multipv=1, cb=done)

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
        # Schlussstellung noch auswerten (wichtig, wenn die Partie beim
        # Verbinden bereits vorbei war: der Stream meldet dann sofort den
        # Endstatus, und ohne dies hier würde stop_watching() gleich
        # darunter jede noch laufende/anstehende Bewertung abwürgen).
        if self.state == "watching":
            self._request_eval()
        self.stop_watching()
