# -*- coding: utf-8 -*-
"""Kleine Eröffnungstabelle (Familien-Ebene) für das Gegner-Dossier."""

from typing import List, Optional

# Schlüssel: SAN-Zugfolge ab Zug 1. Längster Treffer gewinnt.
OPENINGS = {
    # 1.e4 e5
    ("e4", "e5", "Nf3", "Nc6", "Bb5"): "Spanisch",
    ("e4", "e5", "Nf3", "Nc6", "Bc4"): "Italienisch",
    ("e4", "e5", "Nf3", "Nc6", "d4"): "Schottisch",
    ("e4", "e5", "Nf3", "Nf6"): "Russisch (Petroff)",
    ("e4", "e5", "Nf3", "d6"): "Philidor",
    ("e4", "e5", "f4"): "Königsgambit",
    ("e4", "e5", "Nc3"): "Wiener Partie",
    ("e4", "e5", "Bc4"): "Läuferspiel",
    ("e4", "e5"): "Offene Partie",
    # Sizilianisch
    ("e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"): "Sizilianisch (Najdorf)",
    ("e4", "c5", "Nf3", "d6"): "Sizilianisch (…d6)",
    ("e4", "c5", "Nf3", "Nc6"): "Sizilianisch (…Sc6)",
    ("e4", "c5", "Nf3", "e6"): "Sizilianisch (…e6)",
    ("e4", "c5", "c3"): "Sizilianisch (Alapin)",
    ("e4", "c5", "Nc3"): "Sizilianisch (Geschlossen)",
    ("e4", "c5"): "Sizilianisch",
    # weitere Antworten auf 1.e4
    ("e4", "e6"): "Französisch",
    ("e4", "c6"): "Caro-Kann",
    ("e4", "d5"): "Skandinavisch",
    ("e4", "d6"): "Pirc",
    ("e4", "g6"): "Moderne Verteidigung",
    ("e4", "Nf6"): "Aljechin",
    # 1.d4
    ("d4", "d5", "c4", "e6"): "Abgelehntes Damengambit",
    ("d4", "d5", "c4", "c6"): "Slawisch",
    ("d4", "d5", "c4", "dxc4"): "Angenommenes Damengambit",
    ("d4", "d5", "c4"): "Damengambit",
    ("d4", "d5", "Nf3", "Nf6", "Bf4"): "Londoner System",
    ("d4", "Nf6", "Nf3", "d5", "Bf4"): "Londoner System",
    ("d4", "d5", "Bf4"): "Londoner System",
    ("d4", "Nf6", "Bg5"): "Trompowsky",
    ("d4", "Nf6", "c4", "e6", "Nc3", "Bb4"): "Nimzoindisch",
    ("d4", "Nf6", "c4", "e6", "Nf3", "b6"): "Damenindisch",
    ("d4", "Nf6", "c4", "e6", "g3"): "Katalanisch",
    ("d4", "Nf6", "c4", "g6", "Nc3", "d5"): "Grünfeld-Indisch",
    ("d4", "Nf6", "c4", "g6"): "Königsindisch",
    ("d4", "Nf6", "c4", "c5"): "Benoni",
    ("d4", "f5"): "Holländisch",
    ("d4", "d5"): "Damenbauernspiel",
    ("d4", "Nf6"): "Indische Verteidigung",
    # Flankeneröffnungen
    ("c4",): "Englisch",
    ("Nf3",): "Réti",
    ("f4",): "Bird-Eröffnung",
    ("b3",): "Larsen-Eröffnung",
    ("g3",): "Königsfianchetto",
    ("b4",): "Sokolski (Orang-Utan)",
    ("Nc3",): "Van-Geet",
}

_SORTED = sorted(OPENINGS.items(), key=lambda kv: len(kv[0]), reverse=True)


def name_for(sans: List[str]) -> Optional[str]:
    """Liefert den Namen zur längsten passenden Zugfolge, sonst None."""
    for key, name in _SORTED:
        if len(sans) >= len(key) and tuple(sans[:len(key)]) == key:
            return name
    return None
