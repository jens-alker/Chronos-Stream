"""
mock_llm.py — deterministisches Mock-LLM für den Harness.

Kein externer Aufruf, keine Kosten, reproduzierbar (gleicher Input -> gleicher
Output). Ersetzt beim Testen das lokale/Frontier-Modell, damit Testiterationen
LLM-frei, deterministisch und gratis laufen (Feinkonzept_SchichtS §3.0 /
Kontext/Zielfunktion-und-Engpaesse_v1.md).

Später wird hier das echte (freie, ensemblierte) Extraktions-LLM eingehängt
(Modul 2d); die Schnittstelle bleibt gleich.

**Ensemble-Fähigkeit (für Modul 2d, §2d):** ein Mock kann mehrere *Stimmen* (voices)
haben — lokal + freie Fremdstufe. Auf klaren Fällen stimmen alle überein (-> hohe
Konfidenz/`beobachtet`); auf mode-verdächtigen/abstrakten Konzept-Wörtern divergieren
sie (-> Dissens -> `vermutet`/flaggen). So hat die Self-Consistency-/Ensemble-Prüfung
etwas Echtes zu voten. Nur Standardbibliothek.
"""

# Klare Schlagworte -> (Kategorie, ordinale Stärke): alle Stimmen einig (physisch geerdet).
_KEYWORDS = {
    "solid": ("Festkoerperbatterie", "stark"),
    "battery": ("Festkoerperbatterie", "stark"),
    "batterie": ("Festkoerperbatterie", "stark"),
    "transformator": ("Transformatoren", "stark"),
    "grid": ("Stromnetz", "mittel"),
    "netz": ("Stromnetz", "mittel"),
    "lithium": ("Lithium", "mittel"),
}

# Mode-verdächtige/abstrakte Konzept-Wörter: Stimmen UNEINIG (§9 — Sprache ≠ Wirtschaft).
# Pro Schlagwort ein dict voice -> (kat, staerke) oder None (Stimme enthält sich).
_AMBIG = {
    "agentic": {"lokal": ("AgenticAI", "schwach"), "frei": None},
    "metaverse": {"lokal": ("Metaverse", "schwach"), "frei": ("VR", "schwach")},
}

DEFAULT_VOICE = "lokal"


class MockLLM:
    """Deterministischer Extraktor/Kategorisierer für Tests, mit optionaler Stimme."""

    def __init__(self, voice=DEFAULT_VOICE):
        self.voice = voice
        self.modell_vintage = f"mock-{voice}"      # QS-Claude M1: echte Stimmen-Identität in der Zeile

    def kategorisiere(self, text):
        """text -> list[(kategorie, staerke_ordinal)], dedupliziert & deterministisch.

        Klare Schlagworte: stimmenunabhängig. Mode-verdächtige: je nach Stimme (kann
        sich enthalten). Rückgabe kann leer sein -> Aufrufer setzt rest_status."""
        t = (text or "").lower()
        seen, out = set(), []
        for kw, (kat, staerke) in _KEYWORDS.items():
            if kw in t and kat not in seen:
                seen.add(kat)
                out.append((kat, staerke))
        for kw, per_voice in _AMBIG.items():
            if kw in t:
                treffer = per_voice.get(self.voice)
                if treffer and treffer[0] not in seen:
                    seen.add(treffer[0])
                    out.append(treffer)
        return out


def ensemble(voices=("lokal", "frei")):
    """Bequemer Ensemble-Bau für Modul 2d: eine Stimme je freiem Modell (lokal + Gemini-Free).
    Modul 2d orchestriert das Voting/Self-Consistency selbst — der Harness liefert nur die Stimmen."""
    return [MockLLM(voice=v) for v in voices]
