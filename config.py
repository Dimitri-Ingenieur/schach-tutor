# -*- coding: utf-8 -*-
"""Einstellungen: Laden/Speichern und Aufbau des Engine-Kommandos."""

import json
import os
import shlex
import sys


def _app_dir() -> str:
    """Ordner für settings.json/cache/puzzles.json.

    Normal (python main.py): der Ordner, in dem dieses Modul liegt — wie
    bisher. Als PyInstaller-Executable (--onefile) zeigt __file__ dagegen
    in einen Temp-Ordner, der bei --onefile bei JEDEM Start neu entpackt
    und danach wieder gelöscht wird — Einstellungen, Rätsel-Deck und
    Analyse-Cache wären also nach jedem Neustart weg. sys.executable
    bleibt stattdessen stabil dort, wo die .exe tatsächlich liegt (auch
    bei --onedir), das ist hier der richtige Bezugspunkt.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
CACHE_DIR = os.path.join(APP_DIR, "cache")
PUZZLES_FILE = os.path.join(APP_DIR, "puzzles.json")

DEFAULTS = {
    # Engine-Setup: beim ersten Start über Datei → Einstellungen setzen.
    # LC0-Beispiele: Backend cuda-fp16 / onnx-trt (NVIDIA), rocm-fp16 (AMD).
    "engine_path": "",                  # z. B. ~/Projects/Chess/lc0/build/release/lc0
    "weights_path": "",                 # z. B. ~/Netze/BT4-1740.pb.gz
    "backend": "",
    "extra_args": "",                   # zusätzliche Kommandozeilen-Argumente
    "book_path": "",                    # Polyglot-Buch (.bin), optional
    # Knoten je Modus
    "analysis_nodes": 600,              # Feedback im Training
    "play_nodes": 200,                  # Spielstärke der Engine
    "opponent_nodes": 150,              # Batch-Analyse von Partien (Gegner & eigene)
    "puzzle_verify_nodes": 1000,        # Eindeutigkeits-Prüfung der Rätsel
    "player_name": "",                  # eigener Name in PGN-Dateien
    # Verhalten
    "offer_takeback": True,
    "live_eval": False,
}


def load() -> dict:
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            settings.update(data)
    except (OSError, ValueError):
        pass
    return settings


def save(settings: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def engine_path(settings: dict) -> str:
    return os.path.expanduser(settings.get("engine_path", "").strip())


def build_engine_command(settings: dict) -> list:
    """LC0 bekommt --weights/--backend; jede andere UCI-Engine wird pur gestartet."""
    path = engine_path(settings)
    cmd = [path]
    if "lc0" in os.path.basename(path).lower():
        weights = os.path.expanduser(settings.get("weights_path", "").strip())
        if weights:
            cmd.append(f"--weights={weights}")
        backend = settings.get("backend", "").strip()
        if backend:
            cmd.append(f"--backend={backend}")
    extra = settings.get("extra_args", "").strip()
    if extra:
        cmd.extend(shlex.split(extra))
    return cmd


def ensure_cache_dir() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return CACHE_DIR
