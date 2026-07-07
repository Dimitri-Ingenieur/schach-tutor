# -*- coding: utf-8 -*-
"""Gegner-Analyse: PGN laden, Partien durchrechnen, Dossier + Partie-Review."""

import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Dict, List, Optional

import chess
import chess.pgn

from analysis import feedback_text
from board_widget import ARROW_BAD, BoardWidget
import chesscom
import config
import lichess
from opponent_book import build_book
from puzzles import generate_punish_puzzles
from opponent import (GameRecord, analyze_game, build_profile, game_key,
                      load_cached, render_report, save_cached)


class OpponentTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.games: List[chess.pgn.Game] = []
        self.raw_games: List[str] = []
        self.records: Dict[int, GameRecord] = {}
        self.cancel: Optional[threading.Event] = None
        self._build_ui()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Button(top, text="PGN laden …",
                   command=self.load_pgn).pack(side=tk.LEFT)
        self.file_var = tk.StringVar(value="keine Datei geladen")
        ttk.Label(top, textvariable=self.file_var).pack(side=tk.LEFT, padx=8)
        ttk.Label(top, text="   oder online:").pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value="Lichess")
        ttk.Combobox(top, textvariable=self.source_var, width=10,
                     state="readonly",
                     values=["Lichess", "Chess.com"]).pack(side=tk.LEFT,
                                                           padx=(4, 0))
        self.lichess_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.lichess_var,
                  width=16).pack(side=tk.LEFT, padx=4)
        self.lichess_btn = ttk.Button(top, text="Partien laden",
                                      command=self.load_from_online)
        self.lichess_btn.pack(side=tk.LEFT)

        row2 = ttk.Frame(self)
        row2.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row2, text="Spieler:").pack(side=tk.LEFT)
        self.player_var = tk.StringVar()
        self.player_box = ttk.Combobox(row2, textvariable=self.player_var,
                                       width=24, state="readonly")
        self.player_box.pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Knoten/Stellung:").pack(side=tk.LEFT,
                                                      padx=(12, 0))
        self.nodes_var = tk.IntVar(
            value=int(self.app.settings.get("opponent_nodes", 150)))
        ttk.Spinbox(row2, from_=10, to=100000, width=7,
                    textvariable=self.nodes_var).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="max. Partien:").pack(side=tk.LEFT, padx=(12, 0))
        self.max_var = tk.IntVar(value=20)
        ttk.Spinbox(row2, from_=1, to=500, width=5,
                    textvariable=self.max_var).pack(side=tk.LEFT, padx=4)
        self.analyze_btn = ttk.Button(row2, text="Spieler analysieren",
                                      command=self.start_analysis)
        self.analyze_btn.pack(side=tk.LEFT, padx=12)
        self.cancel_btn = ttk.Button(row2, text="Abbrechen",
                                     command=self.cancel_analysis,
                                     state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        prog = ttk.Frame(self)
        prog.pack(fill=tk.X, padx=8, pady=4)
        self.progress = ttk.Progressbar(prog, mode="determinate", length=280)
        self.progress.pack(side=tk.LEFT)
        self.prog_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.prog_var).pack(side=tk.LEFT, padx=8)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, padx=8, pady=4)
        cols = ("weiss", "schwarz", "erg", "datum", "ok")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=7)
        for cid, text, width in (("weiss", "Weiß", 170),
                                 ("schwarz", "Schwarz", 170),
                                 ("erg", "Ergebnis", 70),
                                 ("datum", "Datum", 90),
                                 ("ok", "analysiert", 80)):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(mid, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(mid, text="Partie ansehen",
                   command=self.open_review).pack(side=tk.LEFT, padx=8,
                                                  anchor="n")

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        self.report = ScrolledText(bottom, wrap=tk.NONE, height=14,
                                   font=("DejaVu Sans Mono", 9),
                                   state=tk.DISABLED)
        self.report.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        btns = ttk.Frame(bottom)
        btns.pack(side=tk.LEFT, padx=8, anchor="n")
        ttk.Button(btns, text="Bericht speichern …",
                   command=self.save_report).pack()
        self.punish_btn = ttk.Button(
            btns, text="Widerlegungs-Rätsel\nerzeugen",
            command=self.make_punish_puzzles, state=tk.DISABLED)
        self.punish_btn.pack(pady=(8, 0))
        ttk.Button(btns, text="Gegner-Buch (.bin)\nerzeugen …",
                   command=self.make_opponent_book).pack(pady=(8, 0))

    # ------------------------------------------------------------ PGN

    def load_pgn(self) -> None:
        path = filedialog.askopenfilename(
            title="PGN-Datei wählen",
            filetypes=[("PGN-Dateien", "*.pgn"), ("Alle Dateien", "*.*")])
        if not path:
            return
        self._load_pgn_file(path)

    def _load_pgn_file(self, path: str) -> None:
        games: List[chess.pgn.Game] = []
        raws: List[str] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                while True:
                    game = chess.pgn.read_game(f)
                    if game is None:
                        break
                    games.append(game)
                    raws.append(str(game))
        except OSError as exc:
            messagebox.showerror("Fehler", f"PGN konnte nicht gelesen "
                                           f"werden:\n{exc}")
            return
        if not games:
            messagebox.showinfo("Leer", "Keine Partien in der Datei gefunden.")
            return

        self.games, self.raw_games = games, raws
        self.records.clear()
        self.file_var.set(f"{path.split('/')[-1]}  ({len(games)} Partien)")

        names: Dict[str, int] = {}
        for g in games:
            for key in ("White", "Black"):
                name = g.headers.get(key, "").strip()
                if name and name != "?":
                    names[name] = names.get(name, 0) + 1
        ordered = sorted(names, key=lambda n: -names[n])
        self.player_box.configure(values=ordered)
        if ordered:
            self.player_var.set(ordered[0])

        self.tree.delete(*self.tree.get_children())
        for i, g in enumerate(games):
            h = g.headers
            self.tree.insert("", tk.END, iid=str(i), values=(
                h.get("White", "?"), h.get("Black", "?"),
                h.get("Result", "*"), h.get("Date", "?"), ""))

    # ------------------------------------------------------------ Analyse

    def start_analysis(self) -> None:
        hub = self.app.hub
        if hub is None:
            messagebox.showwarning("Engine fehlt",
                                   "Bitte zuerst unter Datei → Einstellungen "
                                   "eine Engine konfigurieren.")
            return
        name = self.player_var.get().strip()
        if not name:
            messagebox.showinfo("Spieler wählen",
                                "Bitte zuerst eine PGN laden und einen "
                                "Spieler auswählen.")
            return
        try:
            nodes = max(10, int(self.nodes_var.get()))
            max_games = max(1, int(self.max_var.get()))
        except (tk.TclError, ValueError):
            nodes, max_games = 150, 20
        self.app.settings["opponent_nodes"] = nodes

        jobs = [(i, self.games[i], self.raw_games[i])
                for i in range(len(self.games))
                if name in (self.games[i].headers.get("White", ""),
                            self.games[i].headers.get("Black", ""))]
        jobs = jobs[:max_games]
        if not jobs:
            messagebox.showinfo("Keine Partien",
                                f"Keine Partien von „{name}“ gefunden.")
            return

        self.cancel = threading.Event()
        self.analyze_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.progress.configure(maximum=len(jobs), value=0)
        self.prog_var.set(f"0/{len(jobs)} Partien")
        dispatch = self.app.dispatch
        cancel = self.cancel

        def batch(engine):
            done: List[tuple] = []
            for gi, (idx, game, raw) in enumerate(jobs):
                key = game_key(raw, nodes)
                rec = load_cached(key)
                if rec is None:
                    def prog(p, t, gi=gi):
                        dispatch(lambda: self.prog_var.set(
                            f"{gi}/{len(jobs)} Partien · "
                            f"Stellung {p}/{t}"))
                    rec = analyze_game(engine, game, nodes,
                                       cancel=cancel, progress=prog)
                    if rec is None:      # abgebrochen
                        return ("cancelled", done)
                    save_cached(key, rec)
                done.append((idx, rec))
                dispatch(lambda gi=gi, idx=idx: self._mark_progress(
                    gi + 1, len(jobs), idx))
            return ("ok", done)

        hub.submit(batch, cb=lambda res, err: self._analysis_done(
            name, res, err))

    def _mark_progress(self, done_games: int, total: int, idx: int) -> None:
        self.progress.configure(value=done_games)
        self.prog_var.set(f"{done_games}/{total} Partien")
        vals = list(self.tree.item(str(idx), "values"))
        if len(vals) == 5:
            vals[4] = "✓"
            self.tree.item(str(idx), values=vals)

    def cancel_analysis(self) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.prog_var.set("Abbruch angefordert …")

    def _analysis_done(self, name: str, result, err) -> None:
        self.analyze_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        if err is not None:
            messagebox.showerror("Engine-Fehler",
                                 f"Analyse fehlgeschlagen:\n{err}")
            self.prog_var.set("Fehler.")
            return
        status, pairs = result
        for idx, rec in pairs:
            self.records[idx] = rec
        if status == "cancelled":
            self.prog_var.set(f"Abgebrochen – {len(pairs)} Partien fertig.")
        else:
            self.prog_var.set(f"Fertig – {len(pairs)} Partien analysiert.")
        if not pairs:
            return
        self._analyzed_name = name
        self.punish_btn.configure(state=tk.NORMAL)
        prof = build_profile(name, [rec for _, rec in pairs])
        text = render_report(prof)
        self.report.configure(state=tk.NORMAL)
        self.report.delete("1.0", tk.END)
        self.report.insert(tk.END, text)
        self.report.configure(state=tk.DISABLED)

    # ------------------------------------------------------- Lichess

    def load_from_online(self) -> None:
        source = self.source_var.get()
        user = self.lichess_var.get().strip()
        if not user:
            messagebox.showinfo(source,
                                f"Bitte einen {source}-Nutzernamen eingeben.")
            return
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", user)
        prefix = "lichess" if source == "Lichess" else "chesscom"
        dest = os.path.join(config.APP_DIR, f"{prefix}_{safe}.pgn")
        self.lichess_btn.configure(state=tk.DISABLED)
        self.prog_var.set(f"{source}-Download …")
        dispatch = self.app.dispatch

        def worker():
            try:
                if source == "Lichess":
                    def prog(nbytes):
                        dispatch(lambda n=nbytes: self.prog_var.set(
                            f"Lichess-Download … {n // 1024} KiB "
                            f"(drosselt auf ~20 Partien/s)"))
                    lichess.download_games(user, dest, progress=prog)
                else:
                    def prog(ngames, month):
                        dispatch(lambda n=ngames, m=month:
                                 self.prog_var.set(
                                     f"Chess.com-Download … {n} Partien "
                                     f"(Archiv {m})"))
                    n = chesscom.download_games(user, dest, progress=prog)
                    if n == 0:
                        raise RuntimeError(
                            "Keine passenden Partien gefunden (nur "
                            "gewertete Standard-Partien in Blitz/Rapid/"
                            "Daily werden übernommen).")
            except Exception as exc:
                dispatch(lambda e=exc: self._lichess_done(None, e))
                return
            dispatch(lambda: self._lichess_done(dest, None))

        threading.Thread(target=worker, daemon=True,
                         name="OnlineDownload").start()

    def _lichess_done(self, path, err) -> None:
        self.lichess_btn.configure(state=tk.NORMAL)
        if err is not None:
            self.prog_var.set("Download fehlgeschlagen.")
            messagebox.showerror("Partien-Download",
                                 f"Download fehlgeschlagen:\n{err}")
            return
        self.prog_var.set("Download fertig.")
        self._load_pgn_file(path)

    # ---------------------------------------------------- Gegner-Buch

    def make_opponent_book(self) -> None:
        """Polyglot-Buch aus den Zügen des gewählten Spielers schreiben."""
        name = self.player_var.get().strip()
        if not self.games or not name:
            messagebox.showinfo("Partien laden",
                                "Bitte zuerst eine PGN laden und den "
                                "Spieler auswählen.")
            return
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
        path = filedialog.asksaveasfilename(
            title="Gegner-Buch speichern",
            defaultextension=".bin",
            initialfile=f"gegner_{safe}.bin",
            filetypes=[("Polyglot-Buch", "*.bin")])
        if not path:
            return
        try:
            positions, entries = build_book(self.games, name, path)
        except OSError as exc:
            messagebox.showerror("Fehler",
                                 f"Buch konnte nicht geschrieben "
                                 f"werden:\n{exc}")
            return
        if entries == 0:
            messagebox.showinfo("Leer",
                                f"Keine Züge von „{name}“ gefunden.")
            return
        if messagebox.askyesno(
                "Gegner-Buch",
                f"{entries} Buchzüge aus {positions} Stellungen "
                f"geschrieben.\n\n"
                f"Als Engine-Buch für das Training übernehmen?\n"
                f"Die Engine eröffnet dann wie {name}.\n"
                f"(Volle Gegner-Simulation: dazu Maia-Netz als Weights "
                f"und Spiel-Knoten = 1 — siehe README.)"):
            self.app.settings["book_path"] = path
            config.save(self.app.settings)
            self.app.status_var.set(
                f"Engine-Buch: Eröffnungen von {name} aktiv.")

    # -------------------------------------------------- Widerlegungs-Rätsel

    def make_punish_puzzles(self) -> None:
        """Rätsel aus den Fehlern des analysierten Spielers erzeugen."""
        hub = self.app.hub
        name = getattr(self, "_analyzed_name", "") or self.player_var.get()
        recs = [self.records[k] for k in sorted(self.records)]
        if hub is None or not recs or not name:
            messagebox.showinfo("Erst analysieren",
                                "Bitte zuerst den Spieler analysieren.")
            return
        verify = int(self.app.settings.get("puzzle_verify_nodes", 1000))
        self.punish_btn.configure(state=tk.DISABLED)
        self.prog_var.set("Erzeuge Widerlegungs-Rätsel …")
        dispatch = self.app.dispatch

        def batch(engine):
            def prog(g, t, msg):
                dispatch(lambda: self.prog_var.set(
                    f"Widerlegungs-Rätsel · Partie {g}/{t} ({msg})"))
            return generate_punish_puzzles(engine, recs, name, verify,
                                           progress=prog)

        def done(pz, err):
            self.punish_btn.configure(state=tk.NORMAL)
            if err is not None:
                messagebox.showerror("Engine-Fehler",
                                     f"Rätsel-Erzeugung fehlgeschlagen:\n{err}")
                self.prog_var.set("Fehler.")
                return
            added = self.app.puzzle_tab.db.add(pz or [])
            self.app.puzzle_tab.db.save()
            self.app.puzzle_tab._refresh_deck_info()
            self.prog_var.set(f"{len(pz or [])} Kandidaten geprüft, "
                              f"{added} neue Widerlegungs-Rätsel.")
            messagebox.showinfo(
                "Widerlegungs-Rätsel",
                f"{added} neue Rätsel aus den Fehlern von {name} im Deck.\n\n"
                "Trainieren: Tab „Rätsel“ → Quelle „Widerlegung "
                "(Gegnerfehler)“ → Training starten.")

        hub.submit(batch, cb=done)

    # ------------------------------------------------------------ Bericht

    def save_report(self) -> None:
        text = self.report.get("1.0", tk.END).strip()
        if not text:
            messagebox.showinfo("Kein Bericht",
                                "Es gibt noch keinen Bericht zu speichern.")
            return
        path = filedialog.asksaveasfilename(
            title="Bericht speichern", defaultextension=".txt",
            filetypes=[("Textdatei", "*.txt"), ("Markdown", "*.md")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as exc:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen:\n{exc}")

    # ------------------------------------------------------------ Review

    def open_review(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Partie wählen",
                                "Bitte zuerst eine Partie in der Liste "
                                "auswählen.")
            return
        idx = int(sel[0])
        rec = self.records.get(idx)
        if rec is None:
            messagebox.showinfo("Nicht analysiert",
                                "Diese Partie wurde noch nicht analysiert – "
                                "erst „Spieler analysieren“ ausführen.")
            return
        ReviewWindow(self, rec)


class ReviewWindow(tk.Toplevel):
    """Analysierte Partie Zug für Zug durchgehen; jede Stellung kann direkt
    gegen die Engine weitertrainiert werden."""

    def __init__(self, parent: OpponentTab, rec: GameRecord):
        super().__init__(parent)
        self.app = parent.app
        self.rec = rec
        h = rec.headers
        self.title(f"Review: {h.get('White', '?')} – {h.get('Black', '?')} "
                   f"({h.get('Result', '*')})")

        self.positions: List[chess.Board] = [rec.start_board()]
        self.moves: List[chess.Move] = []
        b = rec.start_board()
        for uci in rec.moves_uci:
            mv = chess.Move.from_uci(uci)
            self.moves.append(mv)
            b.push(mv)
            self.positions.append(b.copy())
        self.index = 0    # Anzahl gespielter Züge in der Anzeige

        left = ttk.Frame(self)
        left.pack(side=tk.LEFT, padx=8, pady=8, anchor="n")
        self.board_widget = BoardWidget(left, square=56, interactive=False)
        self.board_widget.pack()

        nav = ttk.Frame(left)
        nav.pack(pady=6)
        ttk.Button(nav, text="|<", width=4,
                   command=lambda: self.goto(0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="<", width=4,
                   command=lambda: self.goto(self.index - 1)).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(nav, text=">", width=4,
                   command=lambda: self.goto(self.index + 1)).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(nav, text=">|", width=4,
                   command=lambda: self.goto(len(self.moves))).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(left, text="Ab hier gegen Engine trainieren",
                   command=self.train_here).pack(pady=4)

        right = ttk.Frame(self)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

        cols = ("zug", "bew", "verlust")
        self.movelist = ttk.Treeview(right, columns=cols, show="headings",
                                     height=16)
        for cid, text, width in (("zug", "Zug", 130),
                                 ("bew", "Bewertung", 90),
                                 ("verlust", "Verlust", 70)):
            self.movelist.heading(cid, text=text)
            self.movelist.column(cid, width=width, anchor="w")
        self.movelist.pack(fill=tk.BOTH, expand=True)
        self.movelist.tag_configure("??", foreground="#b02a1e")
        self.movelist.tag_configure("?", foreground="#c05a00")
        self.movelist.tag_configure("?!", foreground="#b08a00")
        self.movelist.bind("<<TreeviewSelect>>", self._on_select)

        self.info = ScrolledText(right, height=7, wrap=tk.WORD,
                                 font=("DejaVu Sans", 10), state=tk.DISABLED)
        self.info.pack(fill=tk.X, pady=(6, 0))

        for i, mj in enumerate(rec.judgements):
            move_no = mj.ply // 2 + 1
            dots = "." if mj.mover_white else "…"
            loss = mj.cp_loss()
            self.movelist.insert(
                "", tk.END, iid=str(i),
                values=(f"{move_no}{dots} {mj.san}{mj.judgement or ''}",
                        mj.score_after.fmt(chess.WHITE),
                        f"-{loss / 100:.1f}" if loss else ""),
                tags=(mj.judgement,) if mj.judgement else ())

        self.bind("<Left>", lambda e: self.goto(self.index - 1))
        self.bind("<Right>", lambda e: self.goto(self.index + 1))
        self.goto(0)

    def goto(self, index: int) -> None:
        index = max(0, min(len(self.moves), index))
        self.index = index
        board = self.positions[index]
        last = self.moves[index - 1] if index > 0 else None
        self.board_widget.set_position(board, last)

        self.info.configure(state=tk.NORMAL)
        self.info.delete("1.0", tk.END)
        arrows = []
        if index > 0:
            mj = self.rec.judgements[index - 1]
            self.info.insert(tk.END, feedback_text(mj))
            if mj.judgement and mj.refut_line:
                refut = self._parse_first_san(board, mj.refut_line)
                if refut is not None:
                    arrows.append((refut.from_square, refut.to_square,
                                   ARROW_BAD))
            self._syncing = True
            sel = str(index - 1)
            if self.movelist.exists(sel):
                self.movelist.selection_set(sel)
                self.movelist.see(sel)
            self._syncing = False
        else:
            self.info.insert(tk.END, "Ausgangsstellung.")
        self.board_widget.set_arrows(arrows)
        self.info.configure(state=tk.DISABLED)

    @staticmethod
    def _parse_first_san(board: chess.Board, line: str) -> Optional[chess.Move]:
        for token in line.split():
            if token.endswith(".") or token.endswith("…"):
                continue
            try:
                return board.parse_san(token)
            except ValueError:
                return None
        return None

    def _on_select(self, _event) -> None:
        if getattr(self, "_syncing", False):
            return
        sel = self.movelist.selection()
        if sel:
            self.goto(int(sel[0]) + 1)

    def train_here(self) -> None:
        board = self.positions[self.index]
        if board.is_game_over():
            messagebox.showinfo("Partie beendet",
                                "Diese Stellung ist bereits beendet.")
            return
        training = self.app.training_tab
        training.start_from(board, board.turn)
        training._feed("Trainingsstellung aus dem Review geladen – "
                       "du bist am Zug.", "info")
        self.app.show_training()
