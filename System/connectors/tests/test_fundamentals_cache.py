"""Test fundamentals_cache: der DB-backed Cache (Jens 07.08.: alle Daten in EINE DB, keine gzip-Dateien).
Prüft die unveränderte Schnittstelle gegen die SQLite-Storage: hole/lade/speichere/ist_gecacht/bestand/
symbole/TTL/Namespace-Isolation/Migration."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONN = os.path.dirname(_HERE)
if _CONN not in sys.path:
    sys.path.insert(0, _CONN)


class TestCacheDB(unittest.TestCase):
    def setUp(self):
        # Jede Test-Methode eine frische DB (via $MTF_CACHE_DB) — isoliert, kein Real-Cache angefasst.
        self.dir = tempfile.mkdtemp()
        os.environ["MTF_CACHE_DB"] = os.path.join(self.dir, "cache.db")
        import importlib
        import fundamentals_cache
        importlib.reload(fundamentals_cache)          # _DB_PFAD aus dem Env neu lesen
        self.fc = fundamentals_cache

    def tearDown(self):
        os.environ.pop("MTF_CACHE_DB", None)

    def test_speichere_lade_roundtrip(self):
        self.fc.speichere("AAPL.US", {"General": {"Code": "AAPL"}}, cache_dir="fundamentals_cache")
        self.assertEqual(self.fc.lade("AAPL.US", "fundamentals_cache"), {"General": {"Code": "AAPL"}})
        self.assertTrue(self.fc.ist_gecacht("AAPL.US", "fundamentals_cache"))
        self.assertFalse(self.fc.ist_gecacht("MSFT.US", "fundamentals_cache"))

    def test_hole_cache_first(self):
        rufe = []

        def fetch(sym):
            rufe.append(sym)
            return {"x": 1}
        self.assertEqual(self.fc.hole("A.US", fetch, "fundamentals_cache"), {"x": 1})
        self.assertEqual(self.fc.hole("A.US", fetch, "fundamentals_cache"), {"x": 1})   # 2. mal cache-hit
        self.assertEqual(rufe, ["A.US"])              # fetch nur EINMAL

    def test_no_data_marker_gecacht(self):
        self.fc.hole("LEER.US", lambda s: {}, "fundamentals_cache")
        self.assertTrue(self.fc.ist_gecacht("LEER.US", "fundamentals_cache"))   # {} = No-Data-Marker gecacht
        self.assertEqual(self.fc.hole("LEER.US", lambda s: {"neu": 1}, "fundamentals_cache"), {})   # nie wieder gefetcht

    def test_harter_fehler_nicht_gecacht(self):
        def kaputt(s):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.fc.hole("X.US", kaputt, "fundamentals_cache")
        self.assertFalse(self.fc.ist_gecacht("X.US", "fundamentals_cache"))     # Fehler -> kein Cache-Eintrag

    def test_namespace_isolation_eod_vs_fundamentals(self):
        # eod_cache und fundamentals teilen EINE DB, sind aber per Namespace (cache_dir-Basename) getrennt.
        self.fc.speichere("AAPL.US", [{"date": "2020-01-01", "close": 1}], cache_dir="/pfad/eod_cache")
        self.fc.speichere("AAPL.US", {"General": {}}, cache_dir="/pfad/fundamentals_cache")
        self.assertEqual(self.fc.lade("AAPL.US", "/pfad/eod_cache"), [{"date": "2020-01-01", "close": 1}])
        self.assertEqual(self.fc.lade("AAPL.US", "/pfad/fundamentals_cache"), {"General": {}})   # kollidiert NICHT

    def test_ttl_verfall(self):
        self.fc.speichere("T.US", {"a": 1}, "fundamentals_cache")
        import time
        jetzt = time.time()
        self.assertIsNotNone(self.fc.lade("T.US", "fundamentals_cache", max_alter_tage=90, _jetzt=jetzt))
        self.assertIsNone(self.fc.lade("T.US", "fundamentals_cache", max_alter_tage=90,
                                       _jetzt=jetzt + 100 * 86400))              # 100 Tage alt -> abgelaufen

    def test_bestand_und_symbole(self):
        self.fc.speichere("A.US", {"a": 1}, "fundamentals_cache")
        self.fc.speichere("B.US", {"b": 1}, "fundamentals_cache")
        n, bytes_ = self.fc.bestand("fundamentals_cache")
        self.assertEqual(n, 2)
        self.assertGreater(bytes_, 0)
        self.assertEqual(self.fc.symbole("fundamentals_cache"), ["A.US", "B.US"])

    def test_migration_gzip_zu_db(self):
        import gzip
        import json
        alt = os.path.join(self.dir, "eod_cache", "AA")
        os.makedirs(alt)
        with gzip.open(os.path.join(alt, "AAPL.US.json.gz"), "wt", encoding="utf-8") as f:
            json.dump([{"date": "2019-01-01"}], f)
        n = self.fc.migriere_gzip_zu_db(os.path.join(self.dir, "eod_cache"), cache_dir="eod_cache")
        self.assertEqual(n, 1)
        self.assertEqual(self.fc.lade("AAPL.US", "eod_cache"), [{"date": "2019-01-01"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
