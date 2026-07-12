# -*- coding: utf-8 -*-
"""Video-Tab: Schachbrett im Videobild erkennen und live kommentieren.

Quelle kann eine Videodatei, eine Webcam, eine Stream-URL oder der eigene
Bildschirm sein (z. B. ein Browserfenster mit einer laufenden Partie oder
ein YouTube/Twitch-Stream). Ablauf:

  1. Quelle starten → Vorschau erscheint.
  2. Brett-Rechteck mit der Maus in der Vorschau aufziehen.
  3. „Kalibrieren": Die Stellung im Bild muss dem FEN-Feld entsprechen
     (leer = Ausgangsstellung). Daraus lernt die Erkennung Schwellwerte
     und Brett-Orientierung (vision.py).
  4. Ab dann: erkannte Züge laufen durch die vorhandene Zugbewertung
     (judge_move/feedback_text) — der Tutor kommentiert live, mit
     Eval-Balken und bester Fortsetzung.

Architektur wie im Rest der App: Capture + Erkennung in einem
Hintergrund-Thread, GUI-Updates über die Callback-Queue, Generations-
zähler gegen veraltete Ereignisse. Umwandlungen werden als Dame
angenommen (Belegung ist für alle Umwandlungsfiguren identisch).
"""

import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional, Tuple

import chess
import numpy as np
from PIL import Image, ImageTk

import vision
from analysis import feedback_text, judge_move, san_line, win_pct
from board_widget import BoardWidget, EvalBar

PREVIEW_W, PREVIEW_H = 512, 400
CAPTURE_FPS = 8
PREVIEW_EVERY = 0.15          # Sekunden zwischen Vorschau-Updates


class VisionTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.state = "idle"            # idle | running
        self._gen = 0
        self._stop: Optional[threading.Event] = None
        self._frame_lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None      # RGB
        self.classifier: Optional[vision.CellClassifier] = None
        self.recognizer: Optional[vision.Recognizer] = None
        self.board = chess.Board()     # GUI-Spiegel der erkannten Partie
        self.rect: Optional[Tuple[int, int, int, int]] = None  # nativ
        self._scale = 1.0
        self._drag = None
        self._preview_img = None
        self._last_info = None
        self._desync_noted = False
        self._build_ui()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        row1 = ttk.Frame(self)
        row1.pack(fill=tk.X, padx=8, pady=(8, 2))
        ttk.Label(row1, text="Quelle:").pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value="Videodatei")
        ttk.Combobox(row1, textvariable=self.source_var, width=12,
                     state="readonly",
                     values=["Videodatei", "Webcam", "Stream-URL",
                             "Bildschirm"]).pack(side=tk.LEFT, padx=4)
        self.path_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.path_var,
                  width=34).pack(side=tk.LEFT, padx=4)
        ttk.Button(row1, text="…", width=3,
                   command=self._browse).pack(side=tk.LEFT)
        self.start_btn = ttk.Button(row1, text="Start",
                                    command=self.start_capture)
        self.start_btn.pack(side=tk.LEFT, padx=(10, 4))
        self.stop_btn = ttk.Button(row1, text="Stopp", state=tk.DISABLED,
                                   command=self.stop_capture)
        self.stop_btn.pack(side=tk.LEFT)

        row2 = ttk.Frame(self)
        row2.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(row2, text="Stellung im Bild (FEN, leer = "
                             "Ausgangsstellung):").pack(side=tk.LEFT)
        self.fen_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.fen_var,
                  width=44).pack(side=tk.LEFT, padx=4)
        self.calib_btn = ttk.Button(row2, text="Kalibrieren",
                                    command=self.calibrate,
                                    state=tk.DISABLED)
        self.calib_btn.pack(side=tk.LEFT, padx=6)

        self.status_var = tk.StringVar(
            value="Quelle wählen, Start drücken, Brett in der Vorschau "
                  "aufziehen, kalibrieren.")
        ttk.Label(self, textvariable=self.status_var,
                  font=("DejaVu Sans", 11, "bold")).pack(fill=tk.X, padx=8)

        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, anchor="n")
        self.preview = tk.Canvas(left, width=PREVIEW_W, height=PREVIEW_H,
                                 bg="#202020", highlightthickness=1,
                                 highlightbackground="#555")
        self.preview.pack()
        self.preview.bind("<ButtonPress-1>", self._drag_start)
        self.preview.bind("<B1-Motion>", self._drag_move)
        self.preview.bind("<ButtonRelease-1>", self._drag_end)
        ttk.Label(left, text="Brett mit der Maus aufziehen").pack()

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        boards = ttk.Frame(right)
        boards.pack(anchor="n")
        self.board_widget = BoardWidget(boards, square=44,
                                        interactive=False)
        self.board_widget.pack(side=tk.LEFT)
        self.eval_bar = EvalBar(boards,
                                height=self.board_widget.winfo_reqheight())
        self.eval_bar.pack(side=tk.LEFT, padx=(6, 0), anchor="n")
        self.eval_line_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.eval_line_var, wraplength=430,
                  justify=tk.LEFT).pack(fill=tk.X, pady=2)
        self.log = ScrolledText(right, height=10, wrap=tk.WORD,
                                font=("DejaVu Sans", 10),
                                state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)
        for tag, color, bold in (("good", "#1a7a1a", True),
                                 ("warn", "#a06000", True),
                                 ("bad", "#b02a1e", True),
                                 ("info", "#444444", False)):
            self.log.tag_configure(
                tag, foreground=color,
                font=("DejaVu Sans", 10, "bold" if bold else "normal"))

    def _feed(self, text: str, tag: str = "info") -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n", tag)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Videodatei wählen",
            filetypes=[("Videos", "*.mp4 *.avi *.mkv *.webm *.mov"),
                       ("Alle Dateien", "*.*")])
        if path:
            self.path_var.set(path)
            self.source_var.set("Videodatei")

    # ---------------------------------------------------- Rechteck-Auswahl

    def _drag_start(self, ev) -> None:
        self._drag = (ev.x, ev.y, ev.x, ev.y)

    def _drag_move(self, ev) -> None:
        if self._drag is None:
            return
        x0, y0, _, _ = self._drag
        self._drag = (x0, y0, ev.x, ev.y)
        self.preview.delete("rect")
        self.preview.create_rectangle(x0, y0, ev.x, ev.y, outline="#00d000",
                                      width=2, tags="rect")

    def _drag_end(self, ev) -> None:
        if self._drag is None:
            return
        x0, y0, x1, y1 = self._drag
        self._drag = None
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        side = min(x1 - x0, y1 - y0)
        if side < 40:
            self.status_var.set("Rechteck zu klein – Brett großzügig "
                                "aufziehen.")
            return
        s = self._scale
        self.rect = (int(x0 / s), int(y0 / s), int(side / s), int(side / s))
        self.preview.delete("rect")
        self.preview.create_rectangle(x0, y0, x0 + side, y0 + side,
                                      outline="#00d000", width=2,
                                      tags="rect")
        self.status_var.set("Brett gewählt – jetzt „Kalibrieren“ "
                            "(Stellung im Bild = FEN-Feld).")
        if self.state == "running":
            self.calib_btn.configure(state=tk.NORMAL)

    def _redraw_rect(self) -> None:
        """Das (nachjustierte) Brett-Rechteck in der Vorschau anzeigen."""
        if self.rect is None:
            return
        x, y, w, h = self.rect
        sc = self._scale
        self.preview.delete("rect")
        self.preview.create_rectangle(x * sc, y * sc, (x + w) * sc,
                                      (y + h) * sc, outline="#00d000",
                                      width=2, tags="rect")

    # ------------------------------------------------------------ Capture

    def start_capture(self) -> None:
        self.stop_capture()
        self._gen += 1
        gen = self._gen
        self._stop = threading.Event()
        self.state = "running"
        self.classifier = None
        self.recognizer = None
        self._last_info = None
        self._desync_noted = False
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        if self.rect is not None:
            self.calib_btn.configure(state=tk.NORMAL)
        source = self.source_var.get()
        spec = self.path_var.get().strip()
        self.status_var.set(f"Starte Quelle ({source}) …")
        threading.Thread(target=self._capture_loop,
                         args=(gen, self._stop, source, spec),
                         daemon=True, name="VisionCapture").start()

    def stop_capture(self) -> None:
        self._gen += 1
        if self._stop is not None:
            self._stop.set()
        if self.state == "running":
            self.status_var.set("Gestoppt.")
        self.state = "idle"
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.calib_btn.configure(state=tk.DISABLED)

    def _capture_loop(self, gen: int, stop, source: str, spec: str) -> None:
        dispatch = self.app.dispatch
        release = lambda: None  # noqa: E731
        try:
            if source == "Bildschirm":
                import mss
                sct = mss.mss()
                monitor = sct.monitors[1]

                def grab():
                    shot = sct.grab(monitor)
                    return np.array(shot)[:, :, 2::-1]   # BGRA → RGB
                release = sct.close
            else:
                import cv2
                src = int(spec) if source == "Webcam" and spec.isdigit() \
                    else (spec or 0)
                cap = cv2.VideoCapture(src)
                if not cap.isOpened():
                    raise RuntimeError(f"Quelle nicht lesbar: {spec!r}")

                def grab():
                    ok, frame = cap.read()
                    if not ok:
                        return None
                    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                release = cap.release
        except Exception as exc:
            dispatch(lambda e=exc: self._fail(gen, f"Quelle: {e}"))
            return

        last_preview = 0.0
        try:
            while not stop.is_set():
                frame = grab()
                if frame is None:
                    dispatch(lambda: self._fail(gen, "Quelle beendet "
                                                     "(Video zu Ende?)."))
                    return
                with self._frame_lock:
                    self._latest = frame
                now = time.time()
                if now - last_preview >= PREVIEW_EVERY:
                    last_preview = now
                    small = self._downscale(frame)
                    dispatch(lambda f=small: self._show_preview(gen, f))
                clf, rec, rect = self.classifier, self.recognizer, self.rect
                if clf is not None and rec is not None and rect is not None:
                    grid = clf.classify(vision.split_cells(frame, rect))
                    moves = rec.feed(grid)
                    if moves:
                        ucis = [m.uci() for m in moves]
                        dispatch(lambda u=ucis:
                                 self._apply_moves(gen, u))
                    elif rec.desync and not self._desync_noted:
                        self._desync_noted = True
                        dispatch(lambda: self._note_desync(gen))
                    elif not rec.desync:
                        self._desync_noted = False
                time.sleep(1.0 / CAPTURE_FPS)
        finally:
            try:
                release()
            except Exception:
                pass

    @staticmethod
    def _downscale(frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        scale = min(PREVIEW_W / w, PREVIEW_H / h, 1.0)
        if scale >= 1.0:
            return frame
        img = Image.fromarray(frame).resize(
            (int(w * scale), int(h * scale)), Image.BILINEAR)
        return np.array(img)

    # ------------------------------------------------------------ GUI-Seite

    def _show_preview(self, gen: int, frame: np.ndarray) -> None:
        if gen != self._gen:
            return
        with self._frame_lock:
            native = self._latest
        if native is not None:
            self._scale = frame.shape[1] / float(native.shape[1])
        self._preview_img = ImageTk.PhotoImage(Image.fromarray(frame),
                                               master=self.preview)
        self.preview.delete("frame")
        self.preview.create_image(0, 0, anchor="nw",
                                  image=self._preview_img, tags="frame")
        self.preview.tag_raise("rect")

    def calibrate(self) -> None:
        if self.state != "running" or self.rect is None:
            return
        fen = self.fen_var.get().strip()
        expected = None                      # leer → automatisch erkennen
        if fen:
            try:
                expected = chess.Board(fen)
            except ValueError:
                self.status_var.set("Ungültiger FEN.")
                return
        with self._frame_lock:
            frame = self._latest
        if frame is None:
            self.status_var.set("Noch kein Bild von der Quelle.")
            return
        self.status_var.set("Kalibriere (justiere Rechteck nach) …")
        self.update_idletasks()
        clf, refined = vision.calibrate_search(frame, self.rect, expected)
        if clf is None:
            if expected is None:
                self.status_var.set(
                    "Automatische Stellungs-Erkennung fehlgeschlagen – "
                    "Brett-Rechteck neu aufziehen; hilft das nicht, "
                    "Stellung als FEN eintragen. (Rechteck wird bis "
                    "±8 px nachjustiert.)")
            else:
                self.status_var.set(
                    "Kalibrierung fehlgeschlagen – stimmen Stellung "
                    "(FEN) und Brettbereich ungefähr? Bei stärkerem "
                    "Versatz das Rechteck neu aufziehen.")
            return
        self.rect = refined
        self._redraw_rect()
        if expected is not None:
            start_board = expected.copy()
            turn_uncertain = False
        else:
            start_board = clf.detected_board.copy()
            turn_uncertain = clf.turn_uncertain
        self.classifier = clf
        self.recognizer = vision.Recognizer(board=start_board.copy(),
                                            turn_uncertain=turn_uncertain)
        self.board = start_board
        self._last_info = None
        self.board_widget.set_flipped(clf.flipped)
        self.board_widget.set_position(self.board)
        self.eval_bar.set_flipped(clf.flipped)
        seite = "Schwarz" if clf.flipped else "Weiß"
        self.status_var.set(f"Kalibriert ({seite} unten im Video) – "
                            f"kommentiere ab jetzt live.")
        self._feed(f"— Kalibriert. Erkanntes Brett: {seite} unten. "
                   f"Umwandlungen werden als Dame angenommen. —", "info")
        if expected is None:
            self._feed(f"— Stellung automatisch erkannt: "
                       f"{start_board.board_fen()} —", "info")
            if start_board.board_fen() != chess.Board().board_fen():
                zug = "Weiß" if start_board.turn else "Schwarz"
                unsicher = (" (unsicher – korrigiere ich beim ersten "
                            "Zug automatisch)" if turn_uncertain else "")
                self._feed(f"— Am Zug: {zug}{unsicher}. Rochaderechte "
                           f"aus Grundstellungs-Heuristik geraten. —",
                           "info")
        self._request_eval()

    def _apply_moves(self, gen: int, ucis) -> None:
        if gen != self._gen:
            return
        burst = len(ucis) > 1
        if burst:
            # Aufgeholte Züge: Endstellung ist gesichert, die Reihenfolge
            # kann eine Zugumstellung sein — deshalb keine Einzelzug-
            # Bewertung, nur sauber nachziehen und neu aufsetzen.
            self._feed(f"⟳ {len(ucis)} Züge nachgeholt (Reihenfolge "
                       f"rekonstruiert):", "info")
            self._last_info = None
        last_mv = None
        for uci in ucis:
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                return
            if not self.board.is_legal(mv):
                return
            prev_board = self.board.copy()
            san = self.board.san(mv)
            no = self.board.fullmove_number
            dots = "." if self.board.turn == chess.WHITE else "…"
            self.board.push(mv)
            last_mv = mv
            self._feed(f"{no}{dots} {san}")
            if mv.promotion == chess.QUEEN:
                self._feed("   (Umwandlung – Dame angenommen)", "info")
            if not burst:
                self._judge_and_eval(gen, prev_board, mv)
        self.board_widget.set_position(self.board, last_mv)
        if burst:
            self._request_eval()
        self._desync_noted = False

    def _judge_and_eval(self, gen: int, prev_board: chess.Board,
                        mv: chess.Move) -> None:
        hub = self.app.hub
        if hub is None:
            return
        prev_info = self._last_info
        nodes = int(self.app.settings.get("analysis_nodes", 600))
        after = self.board.copy()

        def done(infos, err):
            if gen != self._gen or err or not infos:
                return
            info = infos[0]
            self._last_info = info
            self.eval_bar.set_eval(win_pct(info.score.cp(chess.WHITE)),
                                   info.score.fmt(chess.WHITE))
            self.eval_line_var.set(
                f"LC0: {info.score.fmt(chess.WHITE)}   "
                f"{san_line(after, info.pv, 6)}")
            if prev_info is not None:
                mj = judge_move(prev_board, mv, prev_info, info)
                if mj.judgement:
                    tag = {"?!": "warn", "?": "bad", "??": "bad"}.get(
                        mj.judgement, "info")
                    self._feed(feedback_text(mj), tag)

        hub.analyse(after, nodes, multipv=1, cb=done)

    def _request_eval(self) -> None:
        """Erste Bewertung direkt nach der Kalibrierung."""
        hub = self.app.hub
        if hub is None or self.board.is_game_over():
            return
        gen = self._gen
        snapshot = self.board.copy()
        nodes = int(self.app.settings.get("analysis_nodes", 600))

        def done(infos, err):
            if gen != self._gen or err or not infos:
                return
            self._last_info = infos[0]
            self.eval_bar.set_eval(
                win_pct(infos[0].score.cp(chess.WHITE)),
                infos[0].score.fmt(chess.WHITE))

        hub.analyse(snapshot, nodes, multipv=1, cb=done)

    def _note_desync(self, gen: int) -> None:
        if gen != self._gen:
            return
        self._feed("⚠ Stellung im Bild aktuell nicht aus der letzten "
                   "bekannten erreichbar (Overlay? Sprung im Video?). "
                   "Ich versuche weiter aufzuholen — hilft das nicht: "
                   "aktuelle Stellung als FEN eintragen und neu "
                   "kalibrieren.", "warn")
        self.status_var.set("Erkennung holt auf …")

    def _fail(self, gen: int, text: str) -> None:
        if gen != self._gen:
            return
        self.status_var.set(text)
        self.stop_capture()
