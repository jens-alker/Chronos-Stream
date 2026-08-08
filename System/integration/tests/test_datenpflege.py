#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_datenpflege.py — der asynchrone Datenauffrischer (Feinkonzept Datenpflege §8, BAU-SPEC v2).

Realdaten-nah (Schein-Test-Riegel): die Fundamentals-Fixtures nutzen das echte VERSCHACHTELTE
EODHD-Voll-Dump-Schema (Financials.<Block>.quarterly mit date/filing_date), die EOD-Fixtures das echte
EOD-Zeilenschema, der Kalender-Parser laeuft gegen eine BYTE-REALE `/calendar/earnings`-Antwort
(Live-Smoke 2026-08-06, fixtures/earnings_kalender_2026-08-03.json). Die W10-Naht-Tests:
  (1) Refetch-liefert-Altes -> Backoff (kein ewig-faellig-Quota-Leck K3.1);
  (2) Delisted/{}-No-Data -> Ruhezustand/TTL-Recheck (K3.2);
  (3) sync_restore byte-gleicher Dumps (mtime NEU) -> Faelligkeit aus dem LOG unveraendert (K3);
  (4) Kurs-Voll-Refetch ERSETZT die EOD-Serie (kein Bar-Merge, K2) — durch den ECHTEN fetch_eod_cached;
plus Kadenz-/Latenz-Schaetzer (W7/W4), Budget-in-EINHEITEN + TageslimitErreicht-Fail-fast (W6),
tick-fail-safe und der mechanische Analysten-Firewall-Quell-Scan (G8).

Ausfuehren:  python3 System/integration/tests/test_datenpflege.py
"""
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INT = os.path.dirname(_HERE)
_SYS = os.path.dirname(_INT)
for _p in (os.path.join(_SYS, "connectors"), _INT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import datenpflege as dp                                                # noqa: E402
import eod_cache                                                        # noqa: E402
import eodhd_prices as ep                                               # noqa: E402
import fundamentals_cache as fc                                         # noqa: E402
import fundamentals_drive as fd                                         # noqa: E402
from retro_kat_map_breit import TageslimitErreicht                      # noqa: E402

_FIXTURE_KALENDER = os.path.join(_HERE, "fixtures", "earnings_kalender_2026-08-03.json")


def _dump(filings, shares=100):
    """Echtes EODHD-Voll-Dump-Schema (VERSCHACHTELT, wie der Live-Pfad): drei Statement-Bloecke mit
    quarterly {date, filing_date, ...}. `filings`: [(quartalsende, filing_date), ...]."""
    q = {d: {"date": d, "filing_date": f, "freeCashFlow": "10", "totalRevenue": "100"}
         for d, f in filings}
    return {"General": {"Code": "TEST"},
            "Financials": {"Balance_Sheet": {"quarterly": dict(q)},
                           "Cash_Flow": {"quarterly": dict(q)},
                           "Income_Statement": {"quarterly": dict(q)}},
            "SharesStats": {"SharesOutstanding": shares}}


# Echte Quartals-Rhythmik: Abstaende 84/91/91 Tage -> Median-Kadenz 91; Latenzen 46/40/40/39 -> Median 40.
_FILINGS = [("2018-12-31", "2019-02-15"), ("2019-03-31", "2019-05-10"),
            ("2019-06-30", "2019-08-09"), ("2019-09-30", "2019-11-08")]
_FILINGS_NEU = _FILINGS + [("2019-12-31", "2020-02-14")]


def _bar(datum, close):
    return {"date": datum, "open": close, "high": close, "low": close,
            "close": close, "adjusted_close": close, "volume": 1000}


def _alt_mtime(pfad, tage):
    ts = time.time() - tage * 86400
    os.utime(pfad, (ts, ts))

def _alt_ingest(symbol, cache_dir, tage):
    """DB-Cache-Eintrag altern: t_ingest zurueckdatieren -> loest die TTL im DB-Backend aus."""
    ns = fc._namespace(cache_dir)
    with fc._conn() as c:
        c.execute("UPDATE cache_eintrag SET t_ingest=datetime('now', ?) WHERE namespace=? AND symbol=?",
                  (f"-{int(tage)} days", ns, symbol))
        c.commit()



class _FakeDrive:
    """Minimaler Drive-Ersatz (wie test_fundamentals_drive) fuer den echten sync_hoch/sync_restore-Pfad."""
    def __init__(self):
        self.files, self._n, self.folder = {}, 0, "ordner1"

    def ordner_finden_oder_anlegen(self, at, name=None):
        return self.folder

    def liste_ordner(self, at, parent_id, name_praefix=None):
        return {v["name"]: fid for fid, v in self.files.items()
                if v["parent"] == parent_id and (name_praefix is None or v["name"].startswith(name_praefix))}

    def datei_anlegen(self, at, name, inhalt_bytes, parent_id, mime="application/gzip"):
        self._n += 1
        fid = f"f{self._n}"
        self.files[fid] = {"name": name, "content": inhalt_bytes, "parent": parent_id}
        return fid

    def datei_lesen(self, at, file_id):
        return self.files[file_id]["content"]

    def datei_loeschen(self, at, file_id):
        self.files.pop(file_id, None)
        return True


# ------------------------------------------------------------------ #
# Kalender: Parser gegen die BYTE-REALE Antwort + Fallback-Verhalten
# ------------------------------------------------------------------ #
class TestKalenderParser(unittest.TestCase):
    def test_echtes_schema(self):
        with open(_FIXTURE_KALENDER, encoding="utf-8") as f:
            data = json.load(f)
        rows = ep.parse_earnings_kalender(data)
        self.assertEqual(len(rows), len(data["earnings"]))            # alle echten Zeilen tragen code+datum
        for r in rows:
            self.assertEqual(set(r), {"symbol", "report_date"})       # G8: NUR Symbol + Datum, sonst NICHTS
            self.assertRegex(r["report_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(any(r["symbol"].endswith(".US") for r in rows))

    def test_kaputte_zeilen_fallen_raus(self):
        data = {"earnings": [{"code": "A.US", "report_date": "2026-08-03"},
                             {"code": "", "report_date": "2026-08-03"},          # kein Symbol
                             {"code": "B.US", "report_date": "0000-00-00"},      # degeneriertes Datum
                             {"code": "C.US"},                                    # kein Datum
                             "kein-dict", None]}
        rows = ep.parse_earnings_kalender(data)
        self.assertEqual(rows, [{"symbol": "A.US", "report_date": "2026-08-03"}])

    def test_liste_und_leer(self):
        self.assertEqual(ep.parse_earnings_kalender({}), [])
        self.assertEqual(ep.parse_earnings_kalender(None), [])
        self.assertEqual(ep.parse_earnings_kalender(
            [{"code": "A.US", "report_date": "2026-08-03"}]),
            [{"symbol": "A.US", "report_date": "2026-08-03"}])

    def test_fetch_ohne_endpoint_leere_liste(self):
        # Endpoint/Netz weg -> leere Liste (Kadenz-Fallback traegt), NIE Exception zum Aufrufer.
        alt = ep._curl_json
        ep._curl_json = lambda url, timeout: (_ for _ in ()).throw(RuntimeError("404"))
        try:
            self.assertEqual(ep.fetch_earnings_kalender("2026-08-01", "2026-08-02"), [])
        finally:
            ep._curl_json = alt

    def test_fetch_ohne_key_leere_liste(self):
        alt = ep._token
        ep._token = lambda a=None: (_ for _ in ()).throw(RuntimeError("Kein EODHD-Key"))
        try:
            self.assertEqual(ep.fetch_earnings_kalender("2026-08-01", "2026-08-02"), [])
        finally:
            ep._token = alt


class TestAnalystenFirewall(unittest.TestCase):
    """G8 (mechanisch, test_feld_audit-Stil): der Kalender-Konsum liest AUSSCHLIESSLICH Symbol + Datum.
    Die Analysten-Felder der echten Antwort duerfen in datenpflege.py und den beiden Kalender-Funktionen
    NIRGENDS als Feldzugriff auftauchen (die verworfene Earnings-Surprise-Kruecke laege einen Zugriff
    entfernt)."""
    _VERBOTEN = re.compile(r"(?i)epsestimate|epsactual|[\"'](estimate|actual|difference|percent)[\"']")

    def test_datenpflege_quelle_sauber(self):
        quelle = open(os.path.join(_INT, "datenpflege.py"), encoding="utf-8").read()
        self.assertIsNone(self._VERBOTEN.search(quelle),
                          "datenpflege.py greift auf ein verbotenes Kalender-/Analysten-Feld zu")

    def test_kalender_funktionen_sauber(self):
        for fn in (ep.parse_earnings_kalender, ep.fetch_earnings_kalender):
            self.assertIsNone(self._VERBOTEN.search(inspect.getsource(fn)),
                              f"{fn.__name__} greift auf ein verbotenes Analysten-Feld zu")


# ------------------------------------------------------------------ #
# W7/W4: Kadenz-/Latenz-Schaetzer aus echter filing_date-Sequenz
# ------------------------------------------------------------------ #
class TestKadenzSchaetzer(unittest.TestCase):
    def test_kadenz_aus_echter_sequenz(self):
        self.assertEqual(dp.kadenz_tage(_dump(_FILINGS)), 91)          # Median(84,91,91)

    def test_unter_drei_filings_fallback(self):
        self.assertEqual(dp.kadenz_tage(_dump(_FILINGS[:2])), 100)     # W7: min. 3, sonst ~100 Tage
        self.assertEqual(dp.kadenz_tage({}), 100)
        self.assertEqual(dp.kadenz_tage(None), 100)

    def test_amendment_nicht_monoton_robust(self):
        # Ein rueckdatiertes Amendment (nicht-monoton eingestreut) kippt den Median nicht auf Unsinn.
        filings = _FILINGS + [("2019-01-15", "2019-03-01")]            # Amendment zwischen den Quartalen
        k = dp.kadenz_tage(_dump(filings))
        self.assertTrue(60 <= k <= 100, f"Kadenz {k} ausserhalb plausibler Quartals-Rhythmik")

    def test_letztes_filing_max(self):
        self.assertEqual(dp.letztes_filing(_dump(_FILINGS)), "2019-11-08")
        self.assertIsNone(dp.letztes_filing({}))

    def test_latenz_median(self):
        self.assertEqual(dp.filing_latenz_tage(_dump(_FILINGS)), 40)   # Median(39,40,40,46)
        self.assertEqual(dp.filing_latenz_tage({}), dp._LATENZ_FALLBACK_TAGE)

    def test_rohe_filing_dates_alle_statements(self):
        # W7: auch ein Filing, das NUR im Balance_Sheet steht (FCF-los), zaehlt.
        d = _dump(_FILINGS[:2])
        d["Financials"]["Balance_Sheet"]["quarterly"]["2019-06-30"] = {
            "date": "2019-06-30", "filing_date": "2019-08-09"}
        self.assertEqual(len(dp.rohe_filing_dates(d)), 3)


# ------------------------------------------------------------------ #
# K3/W4: Faelligkeit (rein)
# ------------------------------------------------------------------ #
class TestFaelligkeit(unittest.TestCase):
    _E = {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
          "fehlversuche": 0, "ruhe": False}

    def test_kadenz_faellig(self):
        self.assertFalse(dp.ist_faellig(dict(self._E), "2020-02-06"))
        self.assertTrue(dp.ist_faellig(dict(self._E), "2020-02-07"))

    def test_ruhe_blockt_kadenz_kalender_weckt(self):
        e = dict(self._E, ruhe=True, naechste_faelligkeit=None)
        self.assertFalse(dp.ist_faellig(e, "2021-01-01"))                            # Ruhe: Kadenz aus
        self.assertTrue(dp.ist_faellig(e, "2021-01-01", "2020-11-01", latenz_tage=14))  # Kalender weckt

    def test_kalender_respektiert_latenz_und_altes_filing(self):
        # Kadenz-Pfad isoliert AUS (naechste_faelligkeit weit in der Zukunft) -> nur der Kalender zaehlt.
        e = dict(self._E, naechste_faelligkeit="2020-12-31")
        # report_date + Latenz noch nicht erreicht -> nicht faellig (W4: Filing kommt spaeter).
        self.assertFalse(dp.ist_faellig(e, "2020-03-05", "2020-03-01", latenz_tage=14))
        self.assertTrue(dp.ist_faellig(e, "2020-03-15", "2020-03-01", latenz_tage=14))
        # report_date VOR dem letzten gesehenen Filing = schon im Dump -> nicht faellig.
        self.assertFalse(dp.ist_faellig(e, "2020-01-01", "2019-10-01", latenz_tage=14))

    def test_unparsebares_jetzt_fail_closed(self):
        self.assertFalse(dp.ist_faellig(dict(self._E), "kein-datum"))


# ------------------------------------------------------------------ #
# Fundamentals-Refresh: Log-Init, Backoff (W10-1), No-Data (W10-2), Budget/Tageslimit (W6)
# ------------------------------------------------------------------ #
class TestFundamentalsRefresh(unittest.TestCase):
    def setUp(self):
        self.cd = tempfile.mkdtemp(prefix="dpf_")
        fc._DB_PFAD = os.path.join(self.cd, 'cache.db')  # DB-Isolation (07.08.)

    def tearDown(self):
        shutil.rmtree(self.cd, ignore_errors=True)

    def _kein_fetch(self, sym):
        raise AssertionError(f"unerwarteter Live-Fetch fuer {sym}")

    def test_log_init_ohne_fetch(self):
        # Erst-Kontakt: Log-Eintrag aus dem Dump (NULL Extra-Call), nichts faellig -> 0 Einheiten.
        fc.speichere("AAA.US", _dump(_FILINGS), self.cd)
        b, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2019-12-01", log={},
                                         fetch_fn=self._kein_fetch, cache_dir=self.cd)
        self.assertEqual(b["n_log_init"], 1)
        self.assertEqual(b["n_faellig"], 0)
        self.assertEqual(b["einheiten_verbraucht"], 0)
        self.assertEqual(log["AAA.US"]["letztes_filing"], "2019-11-08")
        self.assertEqual(log["AAA.US"]["naechste_faelligkeit"], "2020-02-07")   # 2019-11-08 + 91

    def test_ungecachte_symbole_uebersprungen(self):
        # Erst-Load ist Backfill-Territorium (KEINE zweite Definition) — kein Fetch, kein Log-Eintrag.
        b, log = dp.fundamentals_refresh(["NEU.US"], 100, jetzt="2020-01-01", log={},
                                         fetch_fn=self._kein_fetch, cache_dir=self.cd)
        self.assertEqual(b["n_uebersprungen_ungecacht"], 1)
        self.assertEqual(log, {})

    def test_w10_1_refetch_liefert_altes_backoff_kein_dauer_refetch(self):
        # W10-1/K3.1: Refetch bringt KEIN neues Filing -> wachsendes Backoff statt ewig-faellig.
        fc.speichere("AAA.US", _dump(_FILINGS), self.cd)
        calls = []

        def fetch(sym):
            calls.append(sym)
            return _dump(_FILINGS)                              # dasselbe alte Quartal
        log = {}
        b, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-10", log=log,
                                         fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(b["n_refetcht"], 1)
        self.assertEqual(b["n_backoff"], 1)
        self.assertEqual(log["AAA.US"]["fehlversuche"], 1)
        self.assertEqual(log["AAA.US"]["naechste_faelligkeit"], "2020-02-17")   # jetzt + 7
        # DERSELBE Tag nochmal: NICHT mehr faellig -> kein zweiter Fetch (das Quota-Leck ist zu).
        b2, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-10", log=log,
                                          fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(len(calls), 1)
        self.assertEqual(b2["n_faellig"], 0)
        # Naechster Zyklus (+14), dann (+28), dann Ruhe.
        _, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-18", log=log,
                                         fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(log["AAA.US"]["naechste_faelligkeit"], "2020-03-03")   # +14
        _, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-03-04", log=log,
                                         fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(log["AAA.US"]["naechste_faelligkeit"], "2020-04-01")   # +28
        b5, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-04-02", log=log,
                                          fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(b5["n_ruhe_neu"], 1)
        self.assertTrue(log["AAA.US"]["ruhe"])                  # K3.1: Ruhezustand, nur noch Kalender
        b6, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2021-01-01", log=log,
                                          fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(b6["n_faellig"], 0)
        self.assertEqual(len(calls), 4)

    def test_erfolg_neues_filing_reset(self):
        fc.speichere("AAA.US", _dump(_FILINGS), self.cd)
        log = {"AAA.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                          "fehlversuche": 2, "ruhe": False}}
        b, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-15", log=log,
                                         fetch_fn=lambda s: _dump(_FILINGS_NEU), cache_dir=self.cd)
        self.assertEqual(b["n_erfolg"], 1)
        e = log["AAA.US"]
        self.assertEqual(e["letztes_filing"], "2020-02-14")     # Erfolg = NEUES Filing sichtbar (K3)
        self.assertEqual(e["fehlversuche"], 0)
        self.assertFalse(e["ruhe"])
        # neuer Anker + Kadenz aus dem NEUEN Dump (Abstaende 84/91/91/98 -> Median 91)
        self.assertEqual(e["naechste_faelligkeit"], "2020-05-15")
        # und der Cache traegt den neuen Dump
        self.assertEqual(dp.letztes_filing(fc.lade("AAA.US", self.cd)), "2020-02-14")

    def test_kalender_weckt_ruhe_und_erfolg_hebt_sie(self):
        fc.speichere("AAA.US", _dump(_FILINGS), self.cd)
        log = {"AAA.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": None,
                          "fehlversuche": 4, "ruhe": True}}
        kal = {"AAA.US": "2020-01-05"}
        # Latenz aus dem Dump = 40 Tage -> vor 2020-02-14 nicht faellig.
        b0, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-10", log=log,
                                          kalender_map=kal, fetch_fn=self._kein_fetch, cache_dir=self.cd)
        self.assertEqual(b0["n_faellig"], 0)
        b1, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-20", log=log,
                                          kalender_map=kal, fetch_fn=lambda s: _dump(_FILINGS_NEU),
                                          cache_dir=self.cd)
        self.assertEqual(b1["n_erfolg"], 1)
        self.assertFalse(log["AAA.US"]["ruhe"])

    def test_w10_2_no_data_ttl_recheck(self):
        # W10-2/K3.2: {}-No-Data wird per TTL re-gecheckt (Neu-Notierung bekommt irgendwann Daten).
        fc.speichere("NEU.US", {}, self.cd)
        calls = []

        def fetch_leer(sym):
            calls.append(sym)
            return {}
        # mtime frisch -> nicht faellig, KEIN Fetch.
        b0, _ = dp.fundamentals_refresh(["NEU.US"], 100, jetzt="2020-01-01", log={},
                                        fetch_fn=self._kein_fetch, cache_dir=self.cd)
        self.assertEqual(b0["n_faellig"], 0)
        # mtime aelter als TTL -> Recheck; liefert wieder {} -> Marker bleibt, TTL startet neu.
        _alt_ingest("NEU.US", self.cd, 200)
        b1, log = dp.fundamentals_refresh(["NEU.US"], 100, jetzt="2020-01-01", log={},
                                          fetch_fn=fetch_leer, cache_dir=self.cd)
        self.assertEqual(b1["n_no_data_recheck"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(fc.lade("NEU.US", self.cd), {})
        self.assertEqual(log, {})                               # {} traegt keinen Filing-Anker
        # sofortiger zweiter Lauf: mtime wieder frisch -> Ruhe bis zur naechsten TTL.
        b2, _ = dp.fundamentals_refresh(["NEU.US"], 100, jetzt="2020-01-01", log={},
                                        fetch_fn=self._kein_fetch, cache_dir=self.cd)
        self.assertEqual(b2["n_faellig"], 0)
        # Recheck liefert jetzt echte Daten -> Log-Anker initialisiert (Neu-Notierung angekommen).
        _alt_ingest("NEU.US", self.cd, 200)
        b3, log3 = dp.fundamentals_refresh(["NEU.US"], 100, jetzt="2020-01-01", log={},
                                           fetch_fn=lambda s: _dump(_FILINGS), cache_dir=self.cd)
        self.assertEqual(b3["n_erfolg"], 1)
        self.assertEqual(log3["NEU.US"]["letztes_filing"], "2019-11-08")

    def test_realer_dump_nie_mit_no_data_ueberschrieben(self):
        # K1-Riegel: ein transientes {} darf den letzten echten Stand nicht vernichten.
        fc.speichere("AAA.US", _dump(_FILINGS), self.cd)
        log = {"AAA.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                          "fehlversuche": 0, "ruhe": False}}
        _, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-10", log=log,
                                         fetch_fn=lambda s: {}, cache_dir=self.cd)
        self.assertEqual(dp.letztes_filing(fc.lade("AAA.US", self.cd)), "2019-11-08")  # Dump unversehrt
        self.assertEqual(log["AAA.US"]["fehlversuche"], 1)      # zaehlt als „kein neues Quartal" -> Backoff

    def test_w6_budget_in_einheiten(self):
        # 3 faellige Fundamentals x 10 Einheiten, Budget 25 -> genau 2 gezogen, resumierbar.
        log = {}
        for s in ("AAA.US", "BBB.US", "CCC.US"):
            fc.speichere(s, _dump(_FILINGS), self.cd)
            log[s] = {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                      "fehlversuche": 0, "ruhe": False}
        calls = []

        def fetch(sym):
            calls.append(sym)
            return _dump(_FILINGS_NEU)
        b, log = dp.fundamentals_refresh(["AAA.US", "BBB.US", "CCC.US"], 25, jetzt="2020-02-10",
                                         log=log, fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(b["einheiten_verbraucht"], 20)
        self.assertEqual(calls, ["AAA.US", "BBB.US"])           # deterministisch sortiert
        self.assertTrue(b["budget_erschoepft"])
        self.assertEqual(b["rest_einheiten"], 5)
        # naechster Tick (frisches Budget): NUR das offene Symbol ist noch faellig -> resumierbar.
        b2, log = dp.fundamentals_refresh(["AAA.US", "BBB.US", "CCC.US"], 25, jetzt="2020-02-10",
                                          log=log, fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(calls[2:], ["CCC.US"])
        self.assertEqual(b2["n_faellig"], 1)

    def test_w6_tageslimit_fail_fast(self):
        log = {}
        for s in ("AAA.US", "BBB.US", "CCC.US"):
            fc.speichere(s, _dump(_FILINGS), self.cd)
            log[s] = {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                      "fehlversuche": 0, "ruhe": False}
        calls = []

        def fetch(sym):
            calls.append(sym)
            raise TageslimitErreicht("EODHD-Tageslimit erschoepft")
        b, _ = dp.fundamentals_refresh(["AAA.US", "BBB.US", "CCC.US"], 100, jetzt="2020-02-10",
                                       log=log, fetch_fn=fetch, cache_dir=self.cd)
        self.assertTrue(b["tageslimit_erreicht"])
        self.assertEqual(calls, ["AAA.US"])                     # fail-fast: KEIN Weiter-Spinnen
        self.assertEqual(b["einheiten_verbraucht"], 10)

    def test_transienter_fehler_bleibt_faellig(self):
        fc.speichere("AAA.US", _dump(_FILINGS), self.cd)
        log = {"AAA.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                          "fehlversuche": 0, "ruhe": False}}
        b, log = dp.fundamentals_refresh(["AAA.US"], 100, jetzt="2020-02-10", log=log,
                                         fetch_fn=lambda s: (_ for _ in ()).throw(RuntimeError("Netz")),
                                         cache_dir=self.cd)
        self.assertEqual(b["n_fehler"], 1)
        self.assertEqual(log["AAA.US"]["fehlversuche"], 0)      # transient != „kein neues Quartal"
        self.assertEqual(log["AAA.US"]["naechste_faelligkeit"], "2020-02-07")   # bleibt faellig (Retry)


# ------------------------------------------------------------------ #
# W10-3: Restore-Festigkeit — Faelligkeit haengt am LOG, nicht an der Datei-mtime
# ------------------------------------------------------------------ #
class TestRestoreFest(unittest.TestCase):
    def setUp(self):
        self.cd = tempfile.mkdtemp(prefix="dpr_")
        fc._DB_PFAD = os.path.join(self.cd, 'cache.db')  # DB-Isolation (07.08.)

    def tearDown(self):
        shutil.rmtree(self.cd, ignore_errors=True)

    def test_sync_restore_aendert_faelligkeit_nicht(self):
        # Zwei Symbole: FAELLIG (naechste in der Vergangenheit) + NICHT faellig (in der Zukunft).
        fc.speichere("DUE.US", _dump(_FILINGS), self.cd)
        fc.speichere("OK.US", _dump(_FILINGS_NEU), self.cd)
        log = {"DUE.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                          "fehlversuche": 0, "ruhe": False},
               "OK.US": {"letztes_filing": "2020-02-14", "naechste_faelligkeit": "2020-05-15",
                         "fehlversuche": 0, "ruhe": False}}
        dp.speichere_log(log, self.cd)
        vorher = {s: fc.lade(s, self.cd) for s in ("DUE.US", "OK.US")}
        # Der ECHTE Drive-Round-Trip (sync_hoch -> Eintraege altern -> sync_restore holt die DB NEU).
        drive = _FakeDrive()
        fd.sync_hoch("at", cache_dir=self.cd, drive=drive)
        for s in vorher:
            _alt_ingest(s, self.cd, 400)
        fd.sync_restore("at", cache_dir=self.cd, drive=drive)
        for s, alt in vorher.items():
            self.assertEqual(fc.lade(s, self.cd), alt)                           # inhaltlich restauriert (DB-Sync)
        log2 = dp.lade_log(self.cd)                              # Log ueberlebt den Restore unangetastet
        self.assertEqual(log2, log)
        # Faelligkeit UNVERAENDERT: DUE bleibt faellig (mtime-Logik saehe „frisch geschrieben" = nie
        # faellig — genau die 26.07.-Signatur), OK bleibt nicht-faellig.
        calls = []
        b, _ = dp.fundamentals_refresh(["DUE.US", "OK.US"], 100, jetzt="2020-02-10", log=log2,
                                       fetch_fn=lambda s: calls.append(s) or _dump(_FILINGS_NEU),
                                       cache_dir=self.cd)
        self.assertEqual(calls, ["DUE.US"])
        self.assertEqual(b["n_faellig"], 1)

    def test_log_roundtrip_atomar(self):
        log = {"AAA.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                          "fehlversuche": 1, "ruhe": False}}
        dp.speichere_log(log, self.cd)
        self.assertEqual(dp.lade_log(self.cd), log)
        self.assertFalse(os.path.exists(dp._log_pfad(self.cd) + ".tmp"))
        # korrupter Log -> {} (re-initialisierbar, kein Crash)
        with open(dp._log_pfad(self.cd), "w", encoding="utf-8") as f:
            f.write("{kaputt")
        self.assertEqual(dp.lade_log(self.cd), {})


# ------------------------------------------------------------------ #
# K2/W10-4: Kurs-Refresh — Voll-Refetch ERSETZT, stale-bewusst, Budget/Tageslimit
# ------------------------------------------------------------------ #
class TestKursRefresh(unittest.TestCase):
    _JETZT = "2020-06-18"                                       # Donnerstag; letzter Montag = 2020-06-15

    def setUp(self):
        self.cd = tempfile.mkdtemp(prefix="dpe_")
        fc._DB_PFAD = os.path.join(self.cd, 'cache.db')  # DB-Isolation (07.08.)

    def tearDown(self):
        shutil.rmtree(self.cd, ignore_errors=True)

    def test_letzter_montag(self):
        self.assertEqual(dp.letzter_montag("2020-06-18").isoformat(), "2020-06-15")
        self.assertEqual(dp.letzter_montag("2020-06-15").isoformat(), "2020-06-15")   # Montag selbst
        self.assertIsNone(dp.letzter_montag("kaputt"))

    def test_eod_ist_frisch_faelle(self):
        # (a) Bar deckt den letzten Montag -> frisch.
        eod_cache.speichere("FRISCH.US", [_bar("2020-06-15", 10.0)], self.cd)
        self.assertTrue(dp.eod_ist_frisch("FRISCH.US", self._JETZT, self.cd))
        # (b) alte Bars + alte Datei -> stale (Refetch faellig).
        eod_cache.speichere("STALE.US", [_bar("2020-05-01", 10.0)], self.cd)
        _alt_ingest("STALE.US", self.cd, 30)
        self.assertFalse(dp.eod_ist_frisch("STALE.US", self._JETZT, self.cd))
        # (c) alte Bars, aber EBEN voll-refetcht (mtime frisch) = inaktiv/delistet -> frisch
        #     (sonst wuerde ein delistetes Symbol JEDE Woche erneut gezogen — K3.1 auf der Kurs-Seite).
        eod_cache.speichere("DEAD.US", [_bar("2015-01-05", 10.0)], self.cd)
        self.assertTrue(dp.eod_ist_frisch("DEAD.US", self._JETZT, self.cd))
        # (d) [] = No-Data-Marker -> nicht anfassen; (e) ungecacht -> stale.
        eod_cache.speichere("LEER.US", [], self.cd)
        self.assertTrue(dp.eod_ist_frisch("LEER.US", self._JETZT, self.cd))
        self.assertFalse(dp.eod_ist_frisch("NIE.US", self._JETZT, self.cd))

    def test_w10_4_voll_refetch_ersetzt_serie_echter_pfad(self):
        # K2: durch den ECHTEN `fetch_eod_cached`-Pfad — die neue Voll-History ERSETZT die alte
        # (kein Bar-Merge: die alte Adjustierungsbasis verschwindet KOMPLETT, inkl. nicht mehr
        # gelieferter Alt-Bars).
        alt_serie = [_bar("2020-01-06", 100.0), _bar("2020-05-01", 100.0)]
        neu_serie = [_bar("2020-01-06", 50.0), _bar("2020-05-01", 50.0), _bar("2020-06-15", 51.0)]
        eod_cache.speichere("SPLIT.US", alt_serie, self.cd)
        _alt_ingest("SPLIT.US", self.cd, 30)
        alt_dir, alt_full = eod_cache._CACHE_DIR, ep._fetch_eod_full
        eod_cache._CACHE_DIR = self.cd
        ep._fetch_eod_full = lambda s, api_token=None, timeout=30: list(neu_serie)
        try:
            rows = ep.fetch_eod_cached("SPLIT.US", to_date=self._JETZT, max_alter_tage=7)
        finally:
            eod_cache._CACHE_DIR, ep._fetch_eod_full = alt_dir, alt_full
        self.assertEqual(eod_cache.lade("SPLIT.US", self.cd), neu_serie)   # ERSETZT, nichts gemischt
        self.assertEqual([r["adjusted_close"] for r in rows], [50.0, 50.0, 51.0])

    def test_kurs_refresh_budget_und_ersatz(self):
        # 3 stale Symbole, Budget 2 -> genau 2 Voll-Refetches (je 1 Einheit), Re-Cache ersetzt.
        for s in ("A.US", "B.US", "C.US"):
            eod_cache.speichere(s, [_bar("2020-05-01", 10.0)], self.cd)
            _alt_ingest(s, self.cd, 30)
        calls = []

        def fetch(sym):
            calls.append(sym)
            neu = [_bar("2020-06-15", 20.0)]
            eod_cache.speichere(sym, neu, self.cd)              # wie fetch_eod_cached: Voll-Re-Cache
            return neu
        b = dp.kurs_refresh(["A.US", "B.US", "C.US"], 2, jetzt=self._JETZT,
                            fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(b["n_stale"], 3)
        self.assertEqual(calls, ["A.US", "B.US"])
        self.assertEqual(b["einheiten_verbraucht"], 2)
        self.assertTrue(b["budget_erschoepft"])
        self.assertEqual(eod_cache.lade("A.US", self.cd), [_bar("2020-06-15", 20.0)])  # ersetzt
        # frische Symbole werden gar nicht erst angefasst
        b2 = dp.kurs_refresh(["A.US"], 10, jetzt=self._JETZT, fetch_fn=fetch, cache_dir=self.cd)
        self.assertEqual(b2["n_stale"], 0)

    def test_kurs_tageslimit_fail_fast(self):
        for s in ("A.US", "B.US"):
            eod_cache.speichere(s, [_bar("2020-05-01", 10.0)], self.cd)
            _alt_ingest(s, self.cd, 30)
        calls = []

        def fetch(sym):
            calls.append(sym)
            raise RuntimeError("EODHD: keine JSON-Antwort: You have reached your daily API requests limit")
        b = dp.kurs_refresh(["A.US", "B.US"], 10, jetzt=self._JETZT, fetch_fn=fetch, cache_dir=self.cd)
        self.assertTrue(b["tageslimit_erreicht"])
        self.assertEqual(calls, ["A.US"])                       # fail-fast

    def test_frische_fn_traegt_prefetch_parallel(self):
        # Der stale-bewusste ist_gecacht_fn traegt den bestehenden Batch-Pfad (prefetch_eod_parallel):
        # frisch = uebersprungen, stale = gezogen. KEINE zweite Batch-Definition.
        eod_cache.speichere("FRISCH.US", [_bar("2020-06-15", 10.0)], self.cd)
        eod_cache.speichere("STALE.US", [_bar("2020-05-01", 10.0)], self.cd)
        _alt_ingest("STALE.US", self.cd, 30)
        gezogen = []
        r = ep.prefetch_eod_parallel(
            ["FRISCH.US", "STALE.US"], fetch_fn=lambda s: gezogen.append(s),
            ist_gecacht_fn=lambda s: dp.eod_ist_frisch(s, self._JETZT, self.cd))
        self.assertEqual(gezogen, ["STALE.US"])
        self.assertEqual(r["uebersprungen"], 1)


# ------------------------------------------------------------------ #
# tick: Budget-Teilung Fundamentals -> Kurse, fail-safe, Tageslimit-Durchgriff
# ------------------------------------------------------------------ #
class TestTick(unittest.TestCase):
    def setUp(self):
        self.fcd = tempfile.mkdtemp(prefix="dptf_")
        self.ecd = tempfile.mkdtemp(prefix="dpte_")
        fc._DB_PFAD = os.path.join(self.fcd, 'cache.db')  # EINE DB, fcd/ecd = Namespaces darin (07.08.)

    def tearDown(self):
        shutil.rmtree(self.fcd, ignore_errors=True)
        shutil.rmtree(self.ecd, ignore_errors=True)

    def test_budget_teilung_fundamentals_dann_kurse(self):
        # 1 faelliges Fundamentals-Symbol (10) + 2 stale Kurs-Symbole (je 1); Budget 11 -> 10 + 1.
        fc.speichere("AAA.US", _dump(_FILINGS), self.fcd)
        dp.speichere_log({"AAA.US": {"letztes_filing": "2019-11-08",
                                     "naechste_faelligkeit": "2020-02-07",
                                     "fehlversuche": 0, "ruhe": False}}, self.fcd)
        for s in ("AAA.US", "BBB.US"):
            eod_cache.speichere(s, [_bar("2020-01-06", 10.0)], self.ecd)
            _alt_ingest(s, self.ecd, 30)
        eod_calls = []
        r = dp.tick(11, jetzt="2020-02-10", symbole=["AAA.US", "BBB.US"],
                    fund_fetch_fn=lambda s: _dump(_FILINGS_NEU),
                    eod_fetch_fn=lambda s: eod_calls.append(s),
                    cache_dir=self.fcd, eod_cache_dir=self.ecd)
        self.assertTrue(r["ok"])
        self.assertEqual(r["fundamentals"]["einheiten_verbraucht"], 10)
        self.assertEqual(r["kurse"]["einheiten_verbraucht"], 1)
        self.assertEqual(r["einheiten_verbraucht"], 11)
        self.assertEqual(len(eod_calls), 1)                     # Rest-Budget deckte genau 1 EOD-Einheit
        # der Log wurde persistiert (resumierbar) und traegt den Erfolg
        self.assertEqual(dp.lade_log(self.fcd)["AAA.US"]["letztes_filing"], "2020-02-14")

    def test_kalender_zeilen_werden_gemappt(self):
        # Kalender ({symbol, report_date}-Zeilen) weckt ein Ruhe-Symbol (G8: mehr traegt er nicht).
        fc.speichere("AAA.US", _dump(_FILINGS), self.fcd)
        dp.speichere_log({"AAA.US": {"letztes_filing": "2019-11-08", "naechste_faelligkeit": None,
                                     "fehlversuche": 4, "ruhe": True}}, self.fcd)
        r = dp.tick(100, jetzt="2020-02-20", symbole=["AAA.US"],
                    kalender=[{"symbol": "AAA.US", "report_date": "2020-01-05"}],
                    fund_fetch_fn=lambda s: _dump(_FILINGS_NEU),
                    cache_dir=self.fcd, eod_cache_dir=self.ecd)
        self.assertTrue(r["ok"])
        self.assertEqual(r["fundamentals"]["n_erfolg"], 1)

    def test_tageslimit_stoppt_auch_kurse(self):
        fc.speichere("AAA.US", _dump(_FILINGS), self.fcd)
        dp.speichere_log({"AAA.US": {"letztes_filing": "2019-11-08",
                                     "naechste_faelligkeit": "2020-02-07",
                                     "fehlversuche": 0, "ruhe": False}}, self.fcd)
        eod_cache.speichere("AAA.US", [_bar("2020-01-06", 10.0)], self.ecd)
        _alt_ingest("AAA.US", self.ecd, 30)
        eod_calls = []

        def fund_limit(sym):
            raise TageslimitErreicht("Tageslimit")
        r = dp.tick(100, jetzt="2020-02-10", symbole=["AAA.US"], fund_fetch_fn=fund_limit,
                    eod_fetch_fn=lambda s: eod_calls.append(s),
                    cache_dir=self.fcd, eod_cache_dir=self.ecd)
        self.assertTrue(r["tageslimit_erreicht"])
        self.assertEqual(eod_calls, [])                         # Quota weg -> KEIN Kurs-Nachschieben

    def test_fail_safe_kippt_nie_den_aufrufer(self):
        # fataler Eingabefehler -> Bericht statt Exception (der Scraper-Sibling darf nie sterben).
        r = dp.tick(100, jetzt="2020-01-01", symbole=42,
                    cache_dir=self.fcd, eod_cache_dir=self.ecd)
        self.assertFalse(r["ok"])
        self.assertIn("TypeError", r["fehler_fatal"])
        # transiente Fetch-Fehler: ok=True, gezaehlt, nichts geworfen.
        fc.speichere("AAA.US", _dump(_FILINGS), self.fcd)
        dp.speichere_log({"AAA.US": {"letztes_filing": "2019-11-08",
                                     "naechste_faelligkeit": "2020-02-07",
                                     "fehlversuche": 0, "ruhe": False}}, self.fcd)
        r2 = dp.tick(100, jetzt="2020-02-10", symbole=["AAA.US"],
                     fund_fetch_fn=lambda s: (_ for _ in ()).throw(RuntimeError("Netz")),
                     cache_dir=self.fcd, eod_cache_dir=self.ecd)
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["fundamentals"]["n_fehler"], 1)


class TestVerlustfreierAbbruch(unittest.TestCase):
    """F131 (Fable-M6): ein control_fn-Stop bricht die Schleife nach dem aktuellen Symbol sauber ab, der
    Backoff-Log ist am Checkpoint PERSISTIERT (kein Quota-Leck durch verlorene fehlversuche), und ein
    zweiter Lauf macht verlustfrei weiter. Realdaten-nah: echter Cache + echter Log auf Platte."""
    def setUp(self):
        self.cd = tempfile.mkdtemp(prefix="dpf_ctl_")
        fc._DB_PFAD = os.path.join(self.cd, 'cache.db')  # DB-Isolation (07.08.)

    def tearDown(self):
        shutil.rmtree(self.cd, ignore_errors=True)

    def test_stop_persistiert_log_und_bricht_sauber_ab(self):
        # zwei gecachte, faellige Symbole; control_fn stoppt VOR dem zweiten.
        for s in ("AAA.US", "BBB.US"):
            fc.speichere(s, _dump(_FILINGS), self.cd)
        dp.speichere_log({s: {"letztes_filing": "2019-11-08", "naechste_faelligkeit": "2020-02-07",
                              "fehlversuche": 0, "ruhe": False} for s in ("AAA.US", "BBB.US")}, self.cd)
        gesehen = []

        def fetch(sym):
            gesehen.append(sym)
            return _dump(_FILINGS)                              # altes Quartal -> Backoff-Update

        zustand = {"n": 0}

        def control():
            zustand["n"] += 1
            return "run" if zustand["n"] == 1 else "stop"      # erstes Symbol ok, dann Stop

        b, _log = dp.fundamentals_refresh(["AAA.US", "BBB.US"], 100, jetzt="2020-02-10", log=None,
                                          fetch_fn=fetch, cache_dir=self.cd, control_fn=control)
        self.assertTrue(b["abgebrochen"])
        self.assertEqual(len(gesehen), 1)                      # nur EIN Symbol verarbeitet
        # M6: der Backoff des verarbeiteten Symbols ist auf PLATTE (nicht nur im RAM verloren).
        log_platte = dp.lade_log(self.cd)
        self.assertEqual(log_platte["AAA.US"]["fehlversuche"], 1)
        # zweiter Lauf ohne Stop: macht bei BBB weiter (AAA ist im Backoff nicht mehr faellig).
        b2, _ = dp.fundamentals_refresh(["AAA.US", "BBB.US"], 100, jetzt="2020-02-10",
                                        log=dp.lade_log(self.cd), fetch_fn=fetch, cache_dir=self.cd)
        self.assertIn("BBB.US", gesehen)                       # BBB jetzt verarbeitet -> nichts verloren
        self.assertFalse(b2["abgebrochen"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
