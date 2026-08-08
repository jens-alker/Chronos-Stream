"""test_markt_db_aufbau — der Runner, der markt_db aus den Caches aufbaut (Jens 07.08.). Orchestrier-Kern
offline (injizierter aufbau_fn/kat_map_fn); der Live-Pfad ist markt_db.aufbau_aus_caches über die echten Caches."""
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INT = os.path.dirname(_HERE)
_SYS = os.path.dirname(_INT)
for _p in (_INT, _SYS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import markt_db_aufbau as R        # noqa: E402
import betrieb_aufsicht as B       # noqa: E402


class TestLadeKatMap(unittest.TestCase):
    def test_liest_map_feld(self):
        import json
        p = os.path.join(tempfile.mkdtemp(), "gic.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"map": {"AAPL.US": "Tech", "BMW.XETRA": "Auto"}, "delisted": {"BMW.XETRA": True}}, f)
        m = R.lade_symbol_kategorie_map(p)
        self.assertEqual(m, {"AAPL.US": "Tech", "BMW.XETRA": "Auto"})   # volle Map, KEIN survivorship-Filter

    def test_fehlende_datei(self):
        self.assertEqual(R.lade_symbol_kategorie_map("/gibts/nicht.json"), {})


class TestLauf(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "markt.db")
        self.ops = os.path.join(tempfile.mkdtemp(), "ops.db")

    def _aufbau(self, **erwartung):
        def fn(dbp, kat_map=None, t_ingest=None, melde_fn=None, abbrechen_fn=None):
            if melde_fn:
                melde_fn("EOD-Kurse", 50, 100)
                melde_fn("EOD-Kurse", 100, 100)
            return {"n_eod_symbole": 100, "n_bars": 5000, "n_fundamentals": 80, "n_meta": 90,
                    "abgebrochen": False, "bestand_eod": 100, "bestand_fundamentals": 80, **erwartung}
        return fn

    def test_fertig_und_fortschritt_in_ops_db(self):
        r = R.lauf(self.db, ops_db=self.ops, aufbau_fn=self._aufbau(),
                   kat_map_fn=lambda p: {"A.US": "Tech"})
        self.assertEqual(r["status"], "fertig")
        self.assertEqual(r["bestand_eod"], 100)
        c = sqlite3.connect(self.ops)
        row = {x["prozess"]: x for x in B.lies_prozess_status(
            c, "2099-01-01T00:00:00", 300, {R.PROZESS_NAME})}[R.PROZESS_NAME]
        self.assertEqual(row["zustand"], "fertig")                     # am Ende fertig (gruen), nicht still
        c.close()

    def test_kat_map_wird_durchgereicht(self):
        gesehen = {}
        def fn(dbp, kat_map=None, **kw):
            gesehen["kat"] = kat_map
            return {"abgebrochen": False, "n_eod_symbole": 1, "n_bars": 1, "n_fundamentals": 0,
                    "n_meta": 1, "bestand_eod": 1}
        R.lauf(self.db, ops_db=self.ops, aufbau_fn=fn, kat_map_fn=lambda p: {"X.US": "K"})
        self.assertEqual(gesehen["kat"], {"X.US": "K"})

    def test_stop_wunsch_bricht_verlustfrei_ab(self):
        def fn(dbp, kat_map=None, t_ingest=None, melde_fn=None, abbrechen_fn=None):
            self.assertTrue(abbrechen_fn())                            # der Runner reicht den Stop als abbrechen_fn durch
            return {"abgebrochen": True, "n_eod_symbole": 2, "n_bars": 4, "n_fundamentals": 0,
                    "n_meta": 0, "bestand_eod": 2}
        r = R.lauf(self.db, ops_db=self.ops, control_fn=lambda: "stop", aufbau_fn=fn, kat_map_fn=lambda p: {})
        self.assertEqual(r["status"], "abgebrochen")

    def test_korrupt_zaehler_wird_sichtbar(self):
        # „dashboard sieht den fehler nicht": ein korrupter Cache-EINTRAG (DB-BLOB) muss als n_korrupt auftauchen.
        import importlib
        sys.path.insert(0, os.path.join(_SYS, "connectors"))
        os.environ["MTF_CACHE_DB"] = os.path.join(tempfile.mkdtemp(), "cache.db")
        self.addCleanup(lambda: os.environ.pop("MTF_CACHE_DB", None))
        import fundamentals_cache as fc
        importlib.reload(fc)
        d = "eod_cache"
        with fc._conn() as c:                                    # korrupten (nicht-gzip) BLOB direkt einschleusen
            c.execute("INSERT INTO cache_eintrag(namespace,symbol,data,t_ingest) VALUES(?,?,?,datetime('now'))",
                      (d, "X.US", b"kein-gueltiges-gzip"))
            c.commit()

        def fn(dbp, kat_map=None, t_ingest=None, melde_fn=None, abbrechen_fn=None):
            fc.lade("X.US", d)                                    # löst den Korrupt-Zähler aus (bad BLOB)
            if melde_fn:
                melde_fn("EOD-Kurse", 1, 1)
            return {"abgebrochen": False, "n_eod_symbole": 0, "n_bars": 0, "n_fundamentals": 0,
                    "n_meta": 0, "bestand_eod": 0}
        r = R.lauf(self.db, ops_db=self.ops, aufbau_fn=fn, kat_map_fn=lambda p: {})
        self.assertGreaterEqual(r["n_korrupt"], 1)               # im Ergebnis sichtbar (nicht nur Konsole)

    def test_ops_db_fehler_kippt_den_lauf_nicht(self):
        # Fail-safe: ops-DB = Verzeichnis -> status_haken faellt still auf No-op zurueck, der Lauf lebt.
        r = R.lauf(self.db, ops_db=tempfile.mkdtemp(), aufbau_fn=self._aufbau(), kat_map_fn=lambda p: {})
        self.assertEqual(r["status"], "fertig")


if __name__ == "__main__":
    unittest.main(verbosity=2)
