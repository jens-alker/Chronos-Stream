"""
test_markt_db.py — Tests der einheitlichen Markt-DB (DB A, E2: Jens „auf jeden Fall eine einheitliche DB").

Prüft die strukturierte EOD-Tabelle (OHLCV, Zeitfenster/PIT), den Fundamentals-Payload-Round-Trip, die
generische Sensor-Serie (getrennt je Quelle, PIT über t_disclosed) und die idempotente Cache-Migration.
Offline, nur temporäre sqlite3-Datei, standardbibliotheksrein.

Ausführen:  python3 System/connectors/tests/test_markt_db.py
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.dirname(_HERE)
if _CONNECTORS not in sys.path:
    sys.path.insert(0, _CONNECTORS)

import markt_db as MD                                                  # noqa: E402


class TestMarktDB(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, self.path)
        self.conn = MD.oeffne(self.path)
        self.addCleanup(self.conn.close)

    def test_eod_strukturiert_und_fenster(self):
        bars = [{"date": "2021-01-04", "open": 10, "high": 11, "low": 9, "close": 10.5,
                 "adjusted_close": 10.4, "volume": 1000},
                {"date": "2021-01-05", "open": 10.5, "high": 12, "low": 10, "close": 11.8,
                 "adjusted_close": 11.7, "volume": 1500},
                {"date": "2021-01-06", "open": 11.8, "high": 12.5, "low": 11, "close": 12.0,
                 "adjusted_close": 11.9, "volume": 900}]
        self.assertEqual(MD.schreibe_eod(self.conn, "AAA", bars, t_ingest="2021-02-01"), 3)
        alle = MD.lade_eod(self.conn, "AAA")
        self.assertEqual(len(alle), 3)
        self.assertEqual(alle[0]["close"], 10.5)                      # chronologisch, strukturiert
        # PIT-Fenster: bis 2021-01-05 -> nur zwei Bars
        fenster = MD.lade_eod(self.conn, "AAA", bis="2021-01-05")
        self.assertEqual([b["datum"] for b in fenster], ["2021-01-04", "2021-01-05"])

    def test_eod_idempotent(self):
        b = [{"date": "2021-01-04", "close": 10}]
        MD.schreibe_eod(self.conn, "AAA", b)
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-04", "close": 99}])   # gleicher Tag -> replace
        rows = MD.lade_eod(self.conn, "AAA")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 99)

    def test_eod_adjclose_alias(self):
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-04", "adjClose": 10.4, "close": 10.5}])
        self.assertEqual(MD.lade_eod(self.conn, "AAA")[0]["adjusted_close"], 10.4)

    def test_eod_ohne_datum_faellt_raus(self):
        self.assertEqual(MD.schreibe_eod(self.conn, "AAA", [{"close": 10}]), 0)

    def test_fundamentals_round_trip(self):
        payload = {"Financials": {"Cash_Flow": {"quarterly": {"2020-12-31": {"freeCashFlow": "5e8"}}}}}
        MD.schreibe_fundamentals(self.conn, "AAA", payload, t_ingest="2021-02-01")
        self.assertEqual(MD.lade_fundamentals(self.conn, "AAA"), payload)      # nested Payload erhalten
        self.assertIsNone(MD.lade_fundamentals(self.conn, "ZZZ"))

    def test_sensor_pit(self):
        reihe = [{"report_date": "2021-06-01", "mass": 0.2}, {"report_date": "2021-06-15", "mass": 0.5},
                 {"report_date": "2021-07-01", "mass": 0.9}]
        self.assertEqual(MD.schreibe_sensor(self.conn, "cot", "HG", reihe, "report_date"), 3)
        # PIT bis 2021-06-15 inklusive -> zwei Zeilen; die 200er ... äh 0.9er (Juli) fehlt
        pit = MD.lade_sensor(self.conn, "cot", "HG", bis="2021-06-15", inklusive=True)
        self.assertEqual([r["mass"] for r in pit], [0.2, 0.5])
        # exklusiv -> nur die vor dem Stichtag
        pit_ex = MD.lade_sensor(self.conn, "cot", "HG", bis="2021-06-15", inklusive=False)
        self.assertEqual([r["mass"] for r in pit_ex], [0.2])

    def test_sensor_disclosure_lag_kein_look_ahead(self):
        # QS-M1/B2: report_date 06-01, aber veröffentlicht (t_disclosed) erst 06-04 -> ein PIT-Schnitt
        # am 06-02 darf die Zeile NICHT liefern (2 Tage Zukunftswissen wäre das Leck).
        reihe = [{"report_date": "2021-06-01", "t_disclosed": "2021-06-04", "mass": 0.7}]
        MD.schreibe_sensor(self.conn, "cot", "HG", reihe, "report_date")
        self.assertEqual(MD.lade_sensor(self.conn, "cot", "HG", bis="2021-06-02"), [])   # noch nicht wissbar
        self.assertEqual([r["mass"] for r in MD.lade_sensor(self.conn, "cot", "HG", bis="2021-06-04")], [0.7])

    def test_sensor_revisions_vintage(self):
        # QS-B3: dieselbe Beobachtung (datum 06-01), First-Print am 06-04, Revision am 07-01.
        MD.schreibe_sensor(self.conn, "cot", "HG",
                           [{"report_date": "2021-06-01", "t_disclosed": "2021-06-04", "mass": 0.7}], "report_date")
        MD.schreibe_sensor(self.conn, "cot", "HG",
                           [{"report_date": "2021-06-01", "t_disclosed": "2021-07-01", "mass": 0.9}], "report_date")
        # zum 06-15 war NUR der First-Print (0.7) bekannt — nicht die Juli-Revision
        self.assertEqual([r["mass"] for r in MD.lade_sensor(self.conn, "cot", "HG", bis="2021-06-15")], [0.7])
        # zum 07-05 gilt die Revision (0.9)
        self.assertEqual([r["mass"] for r in MD.lade_sensor(self.conn, "cot", "HG", bis="2021-07-05")], [0.9])

    def test_sensor_getrennt_je_quelle(self):
        MD.schreibe_sensor(self.conn, "cot", "HG", [{"report_date": "2021-06-01", "m": 1}], "report_date")
        MD.schreibe_sensor(self.conn, "kreditspread", "HG", [{"date": "2021-06-01", "m": 2}], "date")
        # gleiche entity 'HG', aber verschiedene Quellen -> getrennt
        self.assertEqual(MD.lade_sensor(self.conn, "cot", "HG")[0]["m"], 1)
        self.assertEqual(MD.lade_sensor(self.conn, "kreditspread", "HG")[0]["m"], 2)

    def test_migration_aus_cache_idempotent(self):
        cache = {"AAA": [{"date": "2021-01-04", "close": 10}], "BBB": [{"date": "2021-01-04", "close": 20}],
                 "LEER": None}
        ns, nb = MD.migriere_eod_aus_cache(self.conn, ["AAA", "BBB", "LEER"], lambda s: cache.get(s))
        self.assertEqual((ns, nb), (2, 2))
        MD.migriere_eod_aus_cache(self.conn, ["AAA", "BBB"], lambda s: cache.get(s))   # nochmal -> idempotent
        self.assertEqual(MD.bestand(self.conn)[0], 2)                 # zwei EOD-Symbole

    def test_migriere_melde_und_abbruch(self):
        # Fortschritts-Callback (gedrosselt) + verlustfreier Abbruch nach dem ersten Symbol.
        cache = {f"S{i}": [{"date": "2021-01-04", "close": i}] for i in range(5)}
        gemeldet = []
        MD.migriere_eod_aus_cache(self.conn, sorted(cache), lambda s: cache.get(s),
                                  melde_fn=lambda i, g: gemeldet.append((i, g)), melde_jede=1)
        self.assertEqual(gemeldet[-1], (5, 5))                        # letzte Meldung = fertig
        # Abbruch: nach dem 1. Symbol stoppen -> nur 1 migriert, Rest verlustfrei ausgelassen.
        n = [0]
        def abbr():
            n[0] += 1
            return n[0] > 1                                            # erst nach dem 1. Symbol
        conn2 = MD.oeffne(tempfile.mkdtemp() + "/m.db")
        ns, _ = MD.migriere_eod_aus_cache(conn2, sorted(cache), lambda s: cache.get(s), abbrechen_fn=abbr)
        conn2.close()
        self.assertEqual(ns, 1)

    def test_aufbau_aus_caches_fuellt_die_db(self):
        # Der bisher fehlende Produzent: Cache -> markt_db, entity_meta NUR fuer gecachte Symbole.
        eod = {"AAPL.US": [{"date": "2020-01-03", "close": 74.5}], "DEAD.US": []}
        fund = {"AAPL.US": {"General": {"Name": "Apple"}}, "NOFUND.US": None}
        katmap = {"AAPL.US": "Tech Hardware", "DEAD.US": "X", "FREMD.US": "Y"}
        p = os.path.join(tempfile.mkdtemp(), "markt.db")
        r = MD.aufbau_aus_caches(p, kat_map=katmap, eod_symbole=list(eod), fund_symbole=list(fund),
                                 eod_lade=lambda s: eod.get(s), fund_lade=lambda s: fund.get(s),
                                 t_ingest="2026-08-07")
        self.assertEqual(r["n_bars"], 1)
        self.assertEqual(r["n_fundamentals"], 1)                       # NOFUND (None) uebersprungen
        c = MD.oeffne(p)
        self.assertEqual(MD.lade_querschnitt(c, "2020-01-03")["AAPL.US"], ("2020-01-03", 74.5))
        # entity_meta: AAPL + DEAD sind im Cache (auch DEAD als No-Data-Marker) -> Meta; FREMD nicht im Cache -> keine.
        self.assertEqual(c.execute("SELECT kategorie FROM entity_meta WHERE symbol='AAPL.US'").fetchone()[0],
                         "Tech Hardware")
        self.assertIsNone(c.execute("SELECT 1 FROM entity_meta WHERE symbol='FREMD.US'").fetchone())
        c.close()

    def test_aufbau_symbole_als_generator(self):
        # Regressions-Riegel: Symbol-GENERATOREN dürfen entity_meta nicht leer laufen lassen (Doppelkonsum).
        eod = {"A.US": [{"date": "2020-01-03", "close": 1}]}
        p = os.path.join(tempfile.mkdtemp(), "markt.db")
        r = MD.aufbau_aus_caches(p, kat_map={"A.US": "K"},
                                 eod_symbole=(s for s in eod), fund_symbole=(s for s in []),
                                 eod_lade=lambda s: eod.get(s), fund_lade=lambda s: None)
        self.assertEqual(r["n_meta"], 1)                              # NICHT 0 (Generator korrekt materialisiert)

    def test_aufbau_abbruch_verlustfrei(self):
        eod = {"A.US": [{"date": "2020-01-03", "close": 1}]}
        p = os.path.join(tempfile.mkdtemp(), "markt.db")
        r = MD.aufbau_aus_caches(p, kat_map={"A.US": "K"}, eod_symbole=list(eod), fund_symbole=[],
                                 eod_lade=lambda s: eod.get(s), fund_lade=lambda s: None,
                                 abbrechen_fn=lambda: True)             # sofort abbrechen
        self.assertTrue(r["abgebrochen"])
        self.assertEqual(r["n_fundamentals"], 0)                       # Fundamentals-Phase nicht mehr gelaufen
        self.assertEqual(r["n_meta"], 0)                               # entity_meta bei Abbruch ausgelassen

    def test_querschnitt_pit_letzter_bar(self):
        # zwei Symbole, unterschiedliche Handelstage -> Querschnitt nimmt je Symbol den letzten Bar <= Stichtag
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-04", "close": 10},
                                           {"date": "2021-01-06", "close": 12}])
        MD.schreibe_eod(self.conn, "BBB", [{"date": "2021-01-05", "close": 20}])   # kein Bar am 06.
        quer = MD.lade_querschnitt(self.conn, "2021-01-06", feld="close")
        self.assertEqual(quer["AAA"], ("2021-01-06", 12))
        self.assertEqual(quer["BBB"], ("2021-01-05", 20))          # letzter <= Stichtag (PIT)

    def test_querschnitt_max_stale_filtert(self):
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-06", "close": 12}])
        MD.schreibe_eod(self.conn, "DELISTED", [{"date": "2020-01-01", "close": 5}])   # ein Jahr alt
        quer = MD.lade_querschnitt(self.conn, "2021-01-06", max_stale_tage=30)
        self.assertIn("AAA", quer)
        self.assertNotIn("DELISTED", quer)                        # zu alt -> raus (Aktivitäts-Filter)

    def test_querschnitt_kein_look_ahead(self):
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-06", "close": 12},
                                           {"date": "2021-01-08", "close": 99}])
        quer = MD.lade_querschnitt(self.conn, "2021-01-06")
        self.assertEqual(quer["AAA"], ("2021-01-06", 12))         # der 08er-Bar (Zukunft) zählt nie

    def test_querschnitt_nach_kategorie(self):
        for sym, kat, close in [("AAA", "halbleiter", 10), ("BBB", "halbleiter", 20), ("CCC", "solar", 30)]:
            MD.schreibe_eod(self.conn, sym, [{"date": "2021-01-06", "close": close}])
            MD.schreibe_entity_meta(self.conn, sym, {"kategorie": kat, "sektor": "tech"})
        gruppen = MD.querschnitt_nach_kategorie(self.conn, "2021-01-06", ebene="kategorie")
        self.assertEqual(set(gruppen["halbleiter"].values()), {10, 20})
        self.assertEqual(gruppen["solar"], {"CCC": 30})
        # Sektor-Ebene fasst beide Halbleiter + Solar zu 'tech' zusammen
        sekt = MD.querschnitt_nach_kategorie(self.conn, "2021-01-06", ebene="sektor")
        self.assertEqual(len(sekt["tech"]), 3)

    def test_querschnitt_ohne_meta_landet_unter_none(self):
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-06", "close": 10}])   # keine entity_meta
        gruppen = MD.querschnitt_nach_kategorie(self.conn, "2021-01-06")
        self.assertIn(None, gruppen)                              # transparent, nie still verworfen
        self.assertEqual(gruppen[None], {"AAA": 10})

    def test_migration_fundamentals_und_sensor(self):
        fund = {"AAA": {"x": 1}, "BBB": {"y": 2}, "LEER": None}
        n = MD.migriere_fundamentals_aus_cache(self.conn, ["AAA", "BBB", "LEER"], lambda s: fund.get(s))
        self.assertEqual(n, 2)
        self.assertEqual(MD.lade_fundamentals(self.conn, "AAA"), {"x": 1})
        sens = {"HG": [{"report_date": "2021-06-01", "m": 1}, {"report_date": "2021-06-08", "m": 2}]}
        ne, nz = MD.migriere_sensor_aus_cache(self.conn, "cot", ["HG", "XX"],
                                              lambda e: sens.get(e), "report_date")
        self.assertEqual((ne, nz), (1, 2))
        self.assertEqual([r["m"] for r in MD.lade_sensor(self.conn, "cot", "HG")], [1, 2])

    def test_bestand(self):
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-04", "close": 10}])
        MD.schreibe_fundamentals(self.conn, "AAA", {"x": 1})
        MD.schreibe_sensor(self.conn, "cot", "HG", [{"report_date": "2021-06-01", "m": 1}], "report_date")
        self.assertEqual(MD.bestand(self.conn), (1, 1, 1))

    def test_frische(self):
        # der Leitstand-Frische-Leser (F119): jüngstes EOD-Datum + jüngste Wissenszeit
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-04", "close": 10},
                                           {"date": "2021-01-05", "close": 11}], t_ingest="2021-01-06T02:00:00")
        max_datum, max_ingest = MD.frische(self.conn)
        self.assertEqual(max_datum, "2021-01-05")
        self.assertEqual(max_ingest, "2021-01-06T02:00:00")

    def test_frische_leer(self):
        self.assertEqual(MD.frische(self.conn), (None, None))     # leere Tabelle -> fail-closed

    def test_coverage(self):
        # AAA: Preise + Fundamentals + Kategorie = vollständig; BBB: nur Preise
        MD.schreibe_eod(self.conn, "AAA", [{"date": "2021-01-04", "close": 10}])
        MD.schreibe_eod(self.conn, "BBB", [{"date": "2021-01-04", "close": 20}])
        MD.schreibe_fundamentals(self.conn, "AAA", {"x": 1})
        self.conn.execute("INSERT INTO entity_meta(symbol,kategorie) VALUES('AAA','Halbleiter')")
        self.conn.commit()
        c = MD.coverage(self.conn)
        z = c["zusammenfassung"]
        self.assertEqual(z["n"], 2)
        self.assertEqual(z["n_vollstaendig"], 1)                 # nur AAA vollständig
        self.assertEqual(z["prozent"], 50.0)
        aaa = next(s for s in c["symbole"] if s["symbol"] == "AAA")
        self.assertTrue(aaa["hat_fundamentals"] and aaa["kategorie"] == "Halbleiter")

    def test_coverage_leere_kategorie_zaehlt_nicht(self):
        # Claude-#3: eine leere Kategorie ('') darf n_kategorie NICHT aufblähen (Zähler-vs-Anzeige-Konsistenz)
        MD.schreibe_eod(self.conn, "CCC", [{"date": "2021-01-04", "close": 5}])
        self.conn.execute("INSERT INTO entity_meta(symbol,kategorie) VALUES('CCC','')")
        self.conn.commit()
        c = MD.coverage(self.conn)
        self.assertEqual(c["zusammenfassung"]["n_kategorie"], 0)  # leere Kategorie zählt nicht
        ccc = next(s for s in c["symbole"] if s["symbol"] == "CCC")
        self.assertIsNone(ccc["kategorie"])                       # als „keine Kategorie" gewertet


if __name__ == "__main__":
    unittest.main(verbosity=2)
