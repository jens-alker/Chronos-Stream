"""
test_dedup_kern.py — die EINE geteilte Like-Dedup-Definition (Heim = Cloud, Jens 30.07.).
Reine Logik, netz-/DB-frei: Kosinus + Best-Match-Entscheidung + Politik-Konstanten.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONN = os.path.dirname(_HERE)
if _CONN not in sys.path:
    sys.path.insert(0, _CONN)

import dedup_kern as D                                                     # noqa: E402


class TestKosinus(unittest.TestCase):
    def test_identisch_und_orthogonal(self):
        self.assertAlmostEqual(D.kosinus([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(D.kosinus([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_failsafe(self):
        self.assertEqual(D.kosinus([0.0, 0.0], [1.0, 0.0]), 0.0)          # Null-Norm -> 0 (kein Div-0)
        self.assertEqual(D.kosinus([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)     # Dim-Mismatch -> 0 (nomic vs MiniLM)
        self.assertEqual(D.kosinus([], [1.0]), 0.0)


class TestBesteUebereinstimmung(unittest.TestCase):
    def test_best_match_nicht_first_match(self):
        # Kandidat 7 ist ähnlicher als der zuerst gelistete 3 -> 7 gewinnt (Best-Match).
        vec = [1.0, 0.0]
        kand = [(3, [0.8, 0.6]), (7, [0.99, 0.14])]
        self.assertEqual(D.beste_uebereinstimmung(vec, kand, schwelle=0.9), 7)

    def test_unter_schwelle_kein_dup(self):
        self.assertIsNone(D.beste_uebereinstimmung([1.0, 0.0], [(3, [0.0, 1.0])], schwelle=0.9))

    def test_leerer_vektor_und_leere_kandidaten(self):
        self.assertIsNone(D.beste_uebereinstimmung([], [(1, [1.0, 0.0])], 0.9))
        self.assertIsNone(D.beste_uebereinstimmung([1.0, 0.0], [], 0.9))

    def test_tie_bricht_zum_zuerst_gelisteten(self):
        # exakter Gleichstand -> der zuerst gelistete Kandidat gewinnt (sim > best, deterministisch).
        kand = [(5, [1.0, 0.0]), (2, [1.0, 0.0])]
        self.assertEqual(D.beste_uebereinstimmung([1.0, 0.0], kand, 0.9), 5)

    def test_ueberspringt_leeren_kandidaten_vektor(self):
        # ein leerer Kandidaten-Vektor (z. B. 'geprüft, kein Inhalt') matcht nie.
        kand = [(9, []), (4, [1.0, 0.0])]
        self.assertEqual(D.beste_uebereinstimmung([1.0, 0.0], kand, 0.9), 4)


class TestPolitik(unittest.TestCase):
    def test_konstanten(self):
        self.assertTrue(D.BLOCK_SOURCE_TYPE)             # konzept-kritisch: nie über Reifegrad-Sprossen
        self.assertEqual(D.FENSTER_TAGE, 21)


if __name__ == "__main__":
    unittest.main(verbosity=2)
