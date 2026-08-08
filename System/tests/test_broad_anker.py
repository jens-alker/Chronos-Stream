"""
test_broad_anker.py — die breiten Such-Anker sind GUI/DB-editierbar (Jens 29.07.): `broad_anker`/
`set_broad_anker`/`seed_broad_anker` über die `meta`-Tabelle, mit `BROAD` nur noch als Seed-Default/Fallback.
Offline gegen eine in-memory-DB (kein Server, kein Modell). Ausführen:
  python3 System/tests/test_broad_anker.py
"""
import os
import sqlite3
import sys
import threading
import unittest

_SYS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYS not in sys.path:
    sys.path.insert(0, _SYS)

import scraper


class TestBroadAnker(unittest.TestCase):
    def setUp(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        self._db, self._lock = scraper.DB, scraper.LOCK
        scraper.DB, scraper.LOCK = con, threading.Lock()

    def tearDown(self):
        scraper.DB, scraper.LOCK = self._db, self._lock

    def test_fallback_ohne_meta_ist_BROAD(self):
        self.assertEqual(scraper.broad_anker(), scraper.BROAD)     # kein meta-Eintrag -> Seed-Default

    def test_seed_macht_sichtbar(self):
        scraper.seed_broad_anker()
        rows = scraper.q("SELECT value FROM meta WHERE key='such_anker'")
        self.assertTrue(rows and rows[0]["value"])                 # jetzt in der DB (GUI-sichtbar)
        self.assertEqual(scraper.broad_anker(), scraper.BROAD)

    def test_set_und_lesen(self):
        eff = scraper.set_broad_anker(["ai capex", "datacenter", "copper"])
        self.assertEqual(eff, ["ai capex", "datacenter", "copper"])
        self.assertEqual(scraper.broad_anker(), ["ai capex", "datacenter", "copper"])

    def test_leere_eingabe_reset_auf_seed(self):
        scraper.set_broad_anker(["x"])
        eff = scraper.set_broad_anker([])                          # leer -> Reset (nie leerer Anker-Satz)
        self.assertEqual(eff, scraper.BROAD)
        self.assertEqual(scraper.broad_anker(), scraper.BROAD)

    def test_whitespace_und_leer_gefiltert(self):
        self.assertEqual(scraper.set_broad_anker(["  ", "", "  gültig  "]), ["gültig"])

    def test_kaputtes_meta_faellt_auf_BROAD(self):
        scraper.q("INSERT OR REPLACE INTO meta(key,value) VALUES('such_anker','nicht-json')", fetch=False)
        self.assertEqual(scraper.broad_anker(), scraper.BROAD)     # fail-closed
        scraper.q("INSERT OR REPLACE INTO meta(key,value) VALUES('such_anker','[]')", fetch=False)
        self.assertEqual(scraper.broad_anker(), scraper.BROAD)     # leere Liste -> Fallback

    def test_seed_idempotent_ueberschreibt_nicht(self):
        scraper.set_broad_anker(["custom"])
        scraper.seed_broad_anker()                                 # darf ein bestehendes Set NICHT überschreiben
        self.assertEqual(scraper.broad_anker(), ["custom"])


if __name__ == "__main__":
    unittest.main()
