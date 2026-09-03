"""
test_eod_cache.py — EOD-Preis-Cache (symmetrisch zum Fundamentals-Cache) + `fetch_eod_cached`-Slice +
die KRITISCHE Namespace-Isolation im geteilten Drive-Ordner (Fundamentals- und EOD-Shards koexistieren,
ohne einander zu überschreiben/löschen).

Realdaten-nah: die Fixtures nutzen das echte EODHD-EOD-Zeilenschema {date, open, high, low, close,
adjusted_close, volume}. Ausführen:  python3 System/connectors/tests/test_eod_cache.py
"""
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.dirname(_HERE)
if _CONNECTORS not in sys.path:
    sys.path.insert(0, _CONNECTORS)

import eod_cache
import eodhd_prices as ep
import fundamentals_cache as fc


def _bar(datum, close, adj=None):
    """Eine EOD-Zeile im echten EODHD-Schema."""
    return {"date": datum, "open": close, "high": close, "low": close,
            "close": close, "adjusted_close": adj if adj is not None else close, "volume": 1000}


_VOLL = [_bar("2015-06-01", 10.0), _bar("2016-01-04", 12.0), _bar("2018-07-02", 20.0),
         _bar("2020-03-16", 15.0), _bar("2022-01-03", 30.0)]


class TestEodSlice(unittest.TestCase):
    def test_fenster_inklusive(self):
        s = ep._eod_slice(_VOLL, from_date="2016-01-01", to_date="2020-12-31")
        self.assertEqual([r["date"] for r in s], ["2016-01-04", "2018-07-02", "2020-03-16"])

    def test_offene_grenzen(self):
        self.assertEqual(len(ep._eod_slice(_VOLL)), 5)                       # None,None = alles
        self.assertEqual([r["date"] for r in ep._eod_slice(_VOLL, from_date="2020-01-01")],
                         ["2020-03-16", "2022-01-03"])

    def test_grenze_inklusiv(self):
        s = ep._eod_slice(_VOLL, from_date="2016-01-04", to_date="2016-01-04")
        self.assertEqual(len(s), 1)                                          # exakter Tag inklusive

    def test_zeilen_ohne_datum_raus_reihenfolge_bleibt(self):
        roh = [_bar("2016-01-04", 12.0), {"close": 9.9}, _bar("2018-07-02", 20.0)]
        s = ep._eod_slice(roh)
        self.assertEqual([r["date"] for r in s], ["2016-01-04", "2018-07-02"])


class TestEodCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eodc_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_cache_first_zieht_einmal(self):
        rufe = []

        def fetch(sym):
            rufe.append(sym)
            return _VOLL
        a = eod_cache.hole("AAPL.US", fetch, cache_dir=self.dir)
        b = eod_cache.hole("AAPL.US", fetch, cache_dir=self.dir)             # zweiter Ruf: aus dem Cache
        self.assertEqual(a, b)
        self.assertEqual(rufe, ["AAPL.US"])                                 # genau EIN Live-Abruf
        self.assertTrue(eod_cache.ist_gecacht("AAPL.US", cache_dir=self.dir))

    def test_no_data_marker_leere_liste(self):
        rufe = []

        def fetch(sym):
            rufe.append(sym)
            return []                                                        # No-Data (z. B. delistet ohne Kurse)
        self.assertEqual(eod_cache.hole("DEAD.US", fetch, cache_dir=self.dir), [])
        self.assertEqual(eod_cache.hole("DEAD.US", fetch, cache_dir=self.dir), [])
        self.assertEqual(rufe, ["DEAD.US"])                                 # No-Data wird gecacht, nie wieder gezogen

    def test_harter_fehler_wird_nicht_gecacht(self):
        def fetch(sym):
            raise RuntimeError("EODHD nicht erreichbar")
        with self.assertRaises(RuntimeError):
            eod_cache.hole("X.US", fetch, cache_dir=self.dir)
        self.assertFalse(eod_cache.ist_gecacht("X.US", cache_dir=self.dir))  # nicht abgehakt → Retry nächster Lauf


class TestFetchEodCached(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fec_")
        self._orig_dir = eod_cache._CACHE_DIR
        self._orig_full = ep._fetch_eod_full
        eod_cache._CACHE_DIR = self.dir
        self.rufe = []

        def fake_full(symbol, api_token=None, timeout=30):
            self.rufe.append(symbol)
            return _VOLL
        ep._fetch_eod_full = fake_full

    def tearDown(self):
        eod_cache._CACHE_DIR = self._orig_dir
        ep._fetch_eod_full = self._orig_full
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_voll_history_einmal_dann_lokaler_slice(self):
        s1 = ep.fetch_eod_cached("AAPL.US", from_date="2016-01-01", to_date="2019-01-01")
        s2 = ep.fetch_eod_cached("AAPL.US", from_date="2020-01-01")           # anderes Fenster, SELBER Cache
        self.assertEqual([r["date"] for r in s1], ["2016-01-04", "2018-07-02"])
        self.assertEqual([r["date"] for r in s2], ["2020-03-16", "2022-01-03"])
        self.assertEqual(self.rufe, ["AAPL.US"])                             # trotz zweier Fenster nur EIN Live-Abruf

    def test_coverage_refetch_gegen_stille_truncation(self):
        # QS-Gemini-B2: to_date reicht über den letzten gecachten Bar (2022-01-03) hinaus.
        ep.fetch_eod_cached("AAPL.US", from_date="2016-01-01")               # cacht _VOLL (endet 2022-01-03)
        self.rufe.clear()
        # Default False: KEIN Refetch → stiller (hier akzeptierter, weil historischer) Slice, kein Live-Abruf.
        ep.fetch_eod_cached("AAPL.US", to_date="2023-06-01")
        self.assertEqual(self.rufe, [])                                      # Default schont Quota (delistet-Fall)
        # voll_wenn_unvollstaendig=True: erkennt die Lücke → EIN frischer Voll-Abruf, der neue Bars nachliefert.
        ep._fetch_eod_full = lambda symbol, api_token=None, timeout=30: (
            self.rufe.append(symbol) or _VOLL + [_bar("2023-01-05", 40.0)])
        s = ep.fetch_eod_cached("AAPL.US", to_date="2023-06-01", voll_wenn_unvollstaendig=True)
        self.assertEqual(self.rufe, ["AAPL.US"])                            # Refetch ausgelöst
        self.assertIn("2023-01-05", [r["date"] for r in s])                 # neuer Bar ist jetzt drin


class TestFetchEodFullStrikt(unittest.TestCase):
    """QS-Gemini-B3: nur eine echte JSON-Liste (auch leer) wird zurückgegeben/cachbar; eine dict-förmige
    Fehler-/Rate-Limit-Antwort ohne `error`-Key wirft → wird NICHT als falscher `[]`-No-Data-Marker gecacht."""
    def setUp(self):
        self._orig = ep._curl_json

    def tearDown(self):
        ep._curl_json = self._orig

    def test_liste_wird_durchgereicht(self):
        ep._curl_json = lambda url, timeout: _VOLL
        self.assertEqual(ep._fetch_eod_full("AAPL.US"), _VOLL)

    def test_leere_liste_ist_genuines_no_data(self):
        ep._curl_json = lambda url, timeout: []
        self.assertEqual(ep._fetch_eod_full("DEAD.US"), [])                 # 200 + [] = echtes No-Data

    def test_nicht_liste_wirft_nicht_gecacht(self):
        ep._curl_json = lambda url, timeout: {"message": "rate limit"}      # Dict ohne error-Key
        with self.assertRaises(RuntimeError):
            ep._fetch_eod_full("X.US")                                      # wirft → wird NICHT als [] gecacht


if __name__ == "__main__":
    unittest.main()
