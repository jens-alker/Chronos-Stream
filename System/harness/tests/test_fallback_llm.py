"""
test_fallback_llm.py — Offline-Tests des Extraktions-Fallbacks Gemini -> Claude Haiku.

Netz-/Modell-frei: Gemini/Haiku werden durch Fakes ersetzt. Geprüft: Umschalten NUR bei Quota,
laufweiter Zustand (nicht je Aufruf neu), fail-loud bei echtem Fehler, Provenienz im `model`.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from fallback_llm import FallbackLLM                                      # noqa: E402


class _Fake:
    def __init__(self, model, antwort=None, fehler=None):
        self.model = model
        self._antwort = antwort or []
        self._fehler = fehler
        self.aufrufe = 0

    def kategorisiere(self, text):
        self.aufrufe += 1
        if self._fehler:
            raise self._fehler
        return self._antwort


def _mk(gem_fehler=None, gem_antwort=None):
    gem = _Fake("gemini", antwort=gem_antwort or [("Kupfer", "stark")], fehler=gem_fehler)
    haiku = _Fake("haiku", antwort=[("Transformatoren", "mittel")])
    z = {"gemini_aus": False}
    return FallbackLLM(gem, haiku, z), gem, haiku, z


class TestFallback(unittest.TestCase):
    def test_gemini_ok_kein_umschalten(self):
        llm, gem, haiku, z = _mk()
        self.assertEqual(llm.kategorisiere("x"), [("Kupfer", "stark")])
        self.assertFalse(z["gemini_aus"])
        self.assertEqual(haiku.aufrufe, 0)
        self.assertEqual(llm.model, "gemini")

    def test_quota_schaltet_auf_haiku(self):
        llm, gem, haiku, z = _mk(gem_fehler=RuntimeError("generateContent: quota exceeded"))
        r = llm.kategorisiere("x")
        self.assertEqual(r, [("Transformatoren", "mittel")])   # Haiku-Antwort
        self.assertTrue(z["gemini_aus"])
        self.assertIn("[fallback]", llm.model)

    def test_umschalten_ist_laufweit(self):
        # nach dem ersten Quota-Fehler geht der REST direkt an Haiku (Gemini nicht neu probiert).
        llm, gem, haiku, z = _mk(gem_fehler=RuntimeError("Resource_Exhausted"))
        llm.kategorisiere("a")                                  # Gemini scheitert -> Haiku
        llm.kategorisiere("b")                                  # direkt Haiku
        llm.kategorisiere("c")
        self.assertEqual(gem.aufrufe, 1)                        # Gemini nur EINMAL versucht
        self.assertEqual(haiku.aufrufe, 3)

    def test_echter_fehler_fail_loud(self):
        # Nicht-Quota-Fehler wird NICHT geschluckt (kein stiller Fallback).
        llm, gem, haiku, z = _mk(gem_fehler=RuntimeError("unerwartete Gemini-Antwort: kaputt"))
        with self.assertRaises(RuntimeError):
            llm.kategorisiere("x")
        self.assertFalse(z["gemini_aus"])
        self.assertEqual(haiku.aufrufe, 0)

    def test_geteilter_zustand_ueber_stimmen(self):
        # zwei Stimmen teilen den Umschalt-Zustand (Gemini-Erschöpfung gilt fürs ganze Ensemble).
        z = {"gemini_aus": False}
        gem = _Fake("gemini", fehler=RuntimeError("quota"))
        haiku = _Fake("haiku", antwort=[("X", "schwach")])
        a = FallbackLLM(gem, haiku, z, voice="a")
        b = FallbackLLM(gem, haiku, z, voice="b")
        a.kategorisiere("1")                                    # a erschöpft Gemini
        b.kategorisiere("2")                                    # b sieht den geteilten Zustand -> Haiku
        self.assertEqual(gem.aufrufe, 1)                        # Gemini nicht von b neu versucht
        self.assertEqual(haiku.aufrufe, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
