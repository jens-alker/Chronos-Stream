"""
test_treasury_rates.py — rf(t)-Konnektor: reiner Parser + PIT-Kern (offline, KEIN Live-Abruf).

Realdaten-nah: die Fixture ist byte-genau das echte home.treasury.gov-CSV-Schema (Header mit anführungs-
gequoteten Tenören, MM/DD/YYYY, Prozentwerte, absteigend). Ausführen:
  python3 System/connectors/tests/test_treasury_rates.py
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.dirname(_HERE)
if _CONNECTORS not in sys.path:
    sys.path.insert(0, _CONNECTORS)

import treasury_rates as tr

# Echtes Treasury-CSV-Schema (aus dem Live-Abruf 2020), absteigend nach Datum wie geliefert.
_CSV = (
    'Date,"1 Mo","2 Mo","3 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    '12/31/2020,0.08,0.08,0.09,0.09,0.10,0.13,0.17,0.36,0.65,0.93,1.45,1.65\n'
    '12/30/2020,0.06,0.06,0.08,0.09,0.12,0.12,0.17,0.37,0.66,0.93,1.46,1.66\n'
    '01/03/2020,1.52,1.55,1.52,1.55,1.55,1.53,1.54,1.59,1.71,1.80,2.11,2.26\n'
    '01/02/2020,1.53,1.55,1.54,1.57,1.56,1.58,1.59,1.67,1.79,1.88,2.19,2.33\n'
)


class TestParse(unittest.TestCase):
    def test_prozent_zu_dezimal_und_sortierung(self):
        rows = tr.parse_treasury_csv(_CSV)
        self.assertEqual([r["date"] for r in rows],
                         ["2020-01-02", "2020-01-03", "2020-12-30", "2020-12-31"])  # aufsteigend
        self.assertAlmostEqual(rows[-1]["raten"]["10 Yr"], 0.0093)                  # 0.93 % -> 0.0093
        self.assertAlmostEqual(rows[0]["raten"]["30 Yr"], 0.0233)

    def test_kaputtes_datum_und_na_uebersprungen(self):
        csv = _CSV + "notadate,1,2,3,4,5,6,7,8,9,10,11,12\n" + '02/14/2020,N/A,,0.10,,,,,,,1.50,,\n'
        rows = tr.parse_treasury_csv(csv)
        self.assertNotIn(None, [r["date"] for r in rows])                          # kaputte Zeile raus
        feb = [r for r in rows if r["date"] == "2020-02-14"][0]
        self.assertNotIn("1 Mo", feb["raten"])                                     # N/A -> Tenor fehlt (kein Fake-0)
        self.assertAlmostEqual(feb["raten"]["10 Yr"], 0.015)

    def test_leeres_csv(self):
        self.assertEqual(tr.parse_treasury_csv(""), [])
        self.assertEqual(tr.parse_treasury_csv("Date,\"10 Yr\"\n"), [])            # nur Header


class TestPIT(unittest.TestCase):
    def setUp(self):
        self.rows = tr.parse_treasury_csv(_CSV)

    def test_exakter_handelstag(self):
        self.assertAlmostEqual(tr.rf_am_stichtag(self.rows, "2020-01-03"), 0.0180)

    def test_juengster_vor_stichtag_kein_lookahead(self):
        # Stichtag zwischen 01/03 und 12/30 -> jüngster Handelstag ON-OR-BEFORE = 2020-01-03 (kein Look-Ahead
        # auf die Dezember-Werte).
        self.assertAlmostEqual(tr.rf_am_stichtag(self.rows, "2020-06-15"), 0.0180)

    def test_wochenende_nimmt_letzten_handelstag(self):
        self.assertAlmostEqual(tr.rf_am_stichtag(self.rows, "2021-01-01"), 0.0093)  # -> 2020-12-31

    def test_vor_erstem_datum_none(self):
        self.assertIsNone(tr.rf_am_stichtag(self.rows, "2019-12-31"))               # nichts ≤ Stichtag -> None (fail-closed)

    def test_tenor_wahl(self):
        self.assertAlmostEqual(tr.rf_am_stichtag(self.rows, "2020-12-31", tenor="2 Yr"), 0.0013)


class TestIso(unittest.TestCase):
    def test_gueltig(self):
        self.assertEqual(tr._iso("03/07/2018"), "2018-03-07")

    def test_kaputt(self):
        for bad in ("", "2018-03-07", "13/01/2020", "03/32/2020", "3/7/20", None):
            self.assertIsNone(tr._iso(bad), bad)


if __name__ == "__main__":
    unittest.main()
