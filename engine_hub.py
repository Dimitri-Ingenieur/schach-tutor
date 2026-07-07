# -*- coding: utf-8 -*-
"""Serialisierter Zugriff auf eine UCI-Engine (LC0).

Genau wie beim Flask-Server gilt: LC0 verträgt keinen nebenläufigen
UCI-Zugriff (CommandState-Fehler). Deshalb läuft hier *ein* Worker-Thread,
der alle Aufträge nacheinander abarbeitet. Die GUI reicht Jobs über eine
Queue ein und bekommt Ergebnisse per Callback zurück (über den `dispatcher`
in den Tk-Mainloop gehoben).
"""

import queue
import threading
from typing import Callable, List, Optional

import chess
import chess.engine

from analysis import NormInfo, normalize_info

Callback = Callable[[Optional[object], Optional[Exception]], None]


class EngineHub:
    def __init__(self, command: List[str], uci_options: Optional[dict] = None,
                 dispatcher: Optional[Callable[[Callable], None]] = None):
        self._cmd = command
        self._opts = uci_options or {}
        self._dispatch = dispatcher or (lambda fn: fn())
        self._q: "queue.Queue" = queue.Queue()
        self._engine: Optional[chess.engine.SimpleEngine] = None
        self._start_err: Optional[Exception] = None
        self._alive = False
        self._thread: Optional[threading.Thread] = None
        self.engine_name = ""

    # ------------------------------------------------------------ Lifecycle

    def start(self) -> None:
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="EngineHub")
        self._thread.start()

    def quit(self) -> None:
        self._alive = False
        self._q.put(None)

    def _run(self) -> None:
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self._cmd)
            self.engine_name = self._engine.id.get("name", "UCI-Engine")
            for key, val in self._opts.items():
                try:
                    self._engine.configure({key: val})
                except Exception:
                    pass
        except Exception as exc:
            self._start_err = exc

        while self._alive:
            job = self._q.get()
            if job is None:
                break
            fn, cb = job
            if self._start_err is not None:
                self._deliver(cb, None, self._start_err)
                continue
            try:
                result = fn(self._engine)
                err = None
            except Exception as exc:
                result, err = None, exc
            self._deliver(cb, result, err)

        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass

    def _deliver(self, cb: Optional[Callback], result, err) -> None:
        if cb is None:
            return
        self._dispatch(lambda: cb(result, err))

    # ------------------------------------------------------------ Aufträge

    def submit(self, fn: Callable, cb: Optional[Callback] = None) -> None:
        """fn(engine) -> Ergebnis; läuft im Worker-Thread."""
        self._q.put((fn, cb))

    def analyse(self, board: chess.Board, nodes: int,
                multipv: int = 1, cb: Optional[Callback] = None) -> None:
        """Analysiert eine Stellung; Callback erhält List[NormInfo]."""
        b = board.copy()

        def job(engine) -> List[NormInfo]:
            raw = engine.analyse(b, chess.engine.Limit(nodes=nodes),
                                 multipv=multipv)
            if isinstance(raw, dict):
                raw = [raw]
            return [normalize_info(i) for i in raw]

        self.submit(job, cb)

    def play(self, board: chess.Board, nodes: int,
             cb: Optional[Callback] = None) -> None:
        """Lässt die Engine einen Zug spielen; Callback erhält chess.Move."""
        b = board.copy()

        def job(engine) -> chess.Move:
            return engine.play(b, chess.engine.Limit(nodes=nodes)).move

        self.submit(job, cb)
