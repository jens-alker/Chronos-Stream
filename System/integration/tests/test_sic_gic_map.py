"""Test sic_gic_map: die SIC→GIC-Brücke (funding-in-Modul-11) — fail-closed gegen das echte GIC-Universum."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INT = os.path.dirname(_HERE)
if _INT not in sys.path:
    sys.path.insert(0, _INT)

import sic_gic_map as M        # noqa: E402


class TestSicGicMap(unittest.TestCase):
    def test_nur_existierende_ziele(self):
        uni = {"Pharmaceuticals", "Biotechnology", "Application Software"}
        m = M.sic_gic_map(universum=uni, warnen=False)
        self.assertTrue(all(kat in uni for kat, _ in m.values()))
        self.assertEqual(m["2834"], ("Pharmaceuticals", 1))
        self.assertNotIn("3674", m)                            # Semiconductors nicht im Universum -> gedroppt

    def test_semiconductors_wird_gedroppt(self):
        # 3674 zeigt auf "Semiconductors", das im echten breiten Universum fehlt -> darf nie erscheinen.
        m = M.sic_gic_map(warnen=False)                        # echtes GIC-Universum
        self.assertNotIn("3674", m)

    def test_echtes_universum_liefert_zuordnungen(self):
        m = M.sic_gic_map(warnen=False)
        self.assertGreater(len(m), 15)                         # die meisten kuratierten SICs existieren
        self.assertEqual(m["2834"][1], 1)                      # version 1 (GIC-Konvention)
        # jedes Ziel liegt WIRKLICH im Universum:
        uni = M._gic_universum()
        self.assertTrue(all(kat in uni for kat, _ in m.values()))

    def test_cik_pfad(self):
        uni = {"Pharmaceuticals", "Steel"}
        cik_sic = {"111": "2834", "222": "3310", "333": "9999"}   # 9999 hat keine Zuordnung
        out = M.cik_sic_gic(cik_sic, universum=uni)
        self.assertEqual(out["111"], ("Pharmaceuticals", 1))
        self.assertEqual(out["222"], ("Steel", 1))
        self.assertNotIn("333", out)                           # unbekannter SIC -> raus


if __name__ == "__main__":
    unittest.main(verbosity=2)
