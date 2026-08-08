"""
test_betrieb_aufsicht_lauf.py — der Schicht-S-Runner (v0). Deckt die Zwei-Achsen-Datenfrische ab (Jens 08.08.):
ein laufender Sammler, der nur keine NEUEN Dokumente findet, darf KEINEN Prozess-Alarm ausloesen.
"""
import os
import sys
import unittest

_SYSTEM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYSTEM not in sys.path:
    sys.path.insert(0, _SYSTEM)

import betrieb_aufsicht_lauf as L                                       # noqa: E402


class TestDatenfrischeZweiAchsen(unittest.TestCase):
    def test_laeuft_frisch_gruen(self):
        self.assertEqual(L.bewerte_datenfrische(2.0, laeuft=True), ("gruen", "ok"))

    def test_laeuft_11h_keine_warnung(self):
        # DER Bug (Jens): 11.4 h alt, aber Sammler laeuft -> GAR KEINE Warnung mehr (gruen), kein Prozess-Alarm.
        self.assertEqual(L.bewerte_datenfrische(11.4, laeuft=True), ("gruen", "ok"))

    def test_laeuft_deutlich_alt_gelb_quellen_kein_prozess_neustart(self):
        # erst deutlich alt (>24h) + laufend -> gelb + quellen_pruefen (Daten-Achse), NIE prozess_neustart.
        status, empf = L.bewerte_datenfrische(30.0, laeuft=True)
        self.assertEqual(status, "gelb")
        self.assertEqual(empf, "quellen_pruefen")
        self.assertNotEqual(empf, "prozess_neustart")

    def test_laeuft_nie_rot_allein_wegen_frische(self):
        # selbst nach Tagen: solange der Prozess lebt, ist Frische max. gelb (Daten-Achse, kein Betriebs-Alarm).
        self.assertEqual(L.bewerte_datenfrische(300.0, laeuft=True), ("gelb", "quellen_pruefen"))

    def test_prozess_tot_scharfe_staffel(self):
        # Sammler NICHT erreichbar -> die schaerfere Betriebs-Staffel greift (dann ist Neustart die richtige Aktion).
        self.assertEqual(L.bewerte_datenfrische(2.0, laeuft=False), ("gruen", "ok"))
        self.assertEqual(L.bewerte_datenfrische(10.0, laeuft=False), ("gelb", "prozess_neustart"))
        self.assertEqual(L.bewerte_datenfrische(100.0, laeuft=False), ("rot", "prozess_neustart"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
