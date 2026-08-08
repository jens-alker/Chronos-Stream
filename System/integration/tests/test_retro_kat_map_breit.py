"""
test_retro_kat_map_breit.py — Offline-Tests der breiten survivorship-freien Symbol→Kategorie-Map.

Netz/EODHD sind NICHT Teil des Tests — geprüft wird der reine Kern: die Branchen→Kategorie-Lookup
(crisp mappbare Kategorien treffen, grobe Branchen fallen weg), die HYBRID-Assemblierung (breite Map ∪
kuratierte Gold-Ticker; Transformatoren/Stromnetz NUR aus Gold), und der ehrliche Survivorship-Bericht
(delisteter Anteil je Kategorie). Deterministisch, standardbibliotheksrein.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INT = os.path.dirname(_HERE)
_SYS = os.path.dirname(_INT)
for _p in (os.path.join(_SYS, "connectors"), _INT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from retro_kat_map_breit import (klassifiziere, klassifiziere_fein, baue_breite_map, kombiniere,   # noqa: E402
                                 kategorie_bericht, _CRISP_KATEGORIEN, _FEIN_KATEGORIEN,
                                 klassifiziere_gic_direkt,
                                 _merge_ergebnis, lade_persistent, _speichere_persistent)


class TestGicDirekt(unittest.TestCase):
    """GicSubIndustry-direkt-Modus: VIELE Kategorien für entkorrelierte Folds (Power, Jens 24.07.).
    Reality-like: gefüttert mit dem echten EODHD-`General::`-Ausgabeschema {gic, desc, mcap}."""

    def test_gic_wird_kategorie(self):
        self.assertEqual(klassifiziere_gic_direkt("Aerospace & Defense"), "Aerospace & Defense")
        self.assertEqual(klassifiziere_gic_direkt("  Oil & Gas  Exploration  "), "Oil & Gas Exploration")

    def test_leere_platzhalter_fallen_weg(self):
        for g in ("", None, "N/A", "Unknown", "-"):
            self.assertIsNone(klassifiziere_gic_direkt(g), f"{g!r} darf keine Kategorie werden")

    def test_breite_map_direkt_liefert_viele_kategorien(self):
        # Reality-like: echte EODHD-General-Zeilen (gic+desc). Direkt-Modus -> je Branche eine Kategorie,
        # PLUS die kuratierten Themen (Halbleiter bleibt Halbleiter, nicht 'Semiconductors').
        klass = {
            "NVDA.US": {"gic": "Semiconductors", "desc": "designs GPUs"},          # Thema -> Halbleiter
            "JPM.US": {"gic": "Diversified Banks", "desc": "bank"},
            "XOM.US": {"gic": "Integrated Oil & Gas", "desc": "oil major"},
            "BA.US": {"gic": "Aerospace & Defense", "desc": "aircraft"},
            "KO.US": {"gic": "Soft Drinks & Non-alcoholic Beverages", "desc": "beverages"},
            "LEER.US": {"gic": "", "desc": "no classification"},                    # fällt weg
        }
        direkt = baue_breite_map(klass, gic_direkt=True)
        self.assertEqual(direkt["NVDA.US"], "Halbleiter")                          # Thema bleibt kuratiert
        self.assertEqual(direkt["JPM.US"], "Diversified Banks")                    # GicSubIndustry direkt
        self.assertEqual(direkt["BA.US"], "Aerospace & Defense")
        self.assertNotIn("LEER.US", direkt)                                        # leere GicSubIndustry raus
        self.assertGreaterEqual(len(set(direkt.values())), 5)                      # viele Kategorien
        # Gegenprobe: OHNE gic_direkt bleibt nur das Thema (5-Kategorien-Verhalten, rückwärtskompatibel).
        eng = baue_breite_map(klass, gic_direkt=False)
        self.assertEqual(set(eng.values()), {"Halbleiter"})


class TestCrispLookup(unittest.TestCase):
    def test_crisp_branchen_treffen(self):
        self.assertEqual(klassifiziere("Semiconductors"), "Halbleiter")
        self.assertEqual(klassifiziere("Semiconductor Materials & Equipment"), "Halbleiter")
        self.assertEqual(klassifiziere("Copper"), "Kupfer")
        self.assertEqual(klassifiziere("Data Center REITs"), "Rechenzentrum")

    def test_grobe_branchen_nicht_crisp(self):
        self.assertIsNone(klassifiziere("Independent Power Producers & Energy Traders"))  # GEV
        self.assertIsNone(klassifiziere("Construction & Engineering"))                    # PWR
        self.assertIsNone(klassifiziere(""))
        self.assertIsNone(klassifiziere(None))


class TestFeinKlassifikator(unittest.TestCase):
    """Trafo/Netz: GIC-Kandidat + Keyword-Guard, an echten EODHD-Descriptions kalibriert."""

    def test_trafo_produkt_keywords(self):
        # Hyundai Electric: 'power and distribution transformers... switchgears... circuit breakers'
        self.assertEqual(klassifiziere_fein("Electrical Components & Equipment",
                                            "manufactures power and distribution transformers and switchgears"),
                         "Transformatoren")

    def test_B1_trafo_zuerst_trotz_netz_woerter(self):
        # QS-Claude-B1 (der ernste Bug): ein Trafo-Hersteller, dessen VOLLE Description auch 'grid'/
        # 'transmission' nennt (die Mehrheit — Hitachi Energy etc.), MUSS Transformatoren bleiben,
        # nicht fälschlich Stromnetz. Produkt-Keyword (transformer/switchgear) gewinnt vor Netz-Wörtern.
        self.assertEqual(
            klassifiziere_fein("Electrical Components & Equipment",
                               "manufactures power transformers and switchgear for the electrical grid, "
                               "HVDC transmission and cables"),
            "Transformatoren")

    def test_netz_echte_namen(self):
        # Nexans: 'cables... grid... transmission'
        self.assertEqual(klassifiziere_fein("Electrical Components & Equipment",
                                            "manufactures cables for the energy grid and power transmission"),
                         "Stromnetz")
        # Quanta (Construction & Engineering) + strikt-elektrisches Netz-Keyword
        self.assertEqual(klassifiziere_fein("Construction & Engineering",
                                            "design and construction of electric power transmission lines"),
                         "Stromnetz")

    def test_wortgrenzen_kein_teilstring_falschpositiv(self):
        self.assertIsNone(klassifiziere_fein("Electrical Components & Equipment",
                                             "manufactures hybrid powertrains for Ingrid Motors"))

    def test_B2_kontext_falschpositive(self):
        # 'transmission' bare (Getriebe/Daten) darf im Elektro-Pfad NICHT ziehen (nur 'power transmission').
        self.assertIsNone(klassifiziere_fein("Electrical Components & Equipment",
                                             "provides data transmission and signal processing modules"))
        # 'distribution network' (Gas) im Engineering-Pfad -> None (strikt elektrisch verlangt).
        self.assertIsNone(klassifiziere_fein("Construction & Engineering",
                                             "builds and operates the gas distribution network"))

    def test_leere_description_kein_fein(self):
        # QS-Claude-B3: valider Fein-GIC aber LEERE Description -> None (fällt auf Gold zurück).
        self.assertIsNone(klassifiziere_fein("Electrical Components & Equipment", ""))

    def test_lookalikes_fallen_weg(self):
        self.assertIsNone(klassifiziere_fein("Construction & Engineering",
                                             "engineering, procurement, and construction project management"))  # Fluor
        self.assertIsNone(klassifiziere_fein("Electrical Components & Equipment",
                                             "manufactures connectors and sensors"))
        self.assertIsNone(klassifiziere_fein("Independent Power Producers & Energy Traders",
                                             "generate, transfer, convert electricity"))                        # GEV
        self.assertIsNone(klassifiziere_fein("Multi-Utilities", "transmission and distribution of electricity"))  # NG

    def test_B7_casefold_gic(self):
        # GIC-Casing/Spacing-Drift darf keinen stillen Miss erzeugen.
        self.assertEqual(klassifiziere_fein("electrical  components & equipment",
                                            "manufactures transformers"), "Transformatoren")


class TestPersistenz(unittest.TestCase):
    """Wiederaufnehmbarer Ergebnis-Store (Cache-Persistenz vor dem Voll-Batch)."""

    def test_merge_treffer_und_checked(self):
        store = {"map": {}, "delisted": {}, "checked": set()}
        _merge_ergebnis(store, "XLNX.US", "Halbleiter", True)     # Treffer, delistet
        _merge_ergebnis(store, "ZZZ.US", None, False)            # geprüft, kein Treffer
        self.assertEqual(store["map"], {"XLNX.US": "Halbleiter"})
        self.assertEqual(store["delisted"], {"XLNX.US": True})
        self.assertEqual(store["checked"], {"XLNX.US", "ZZZ.US"})   # BEIDE geprüft (Resumability)

    def test_round_trip_persistiert_und_resumebar(self):
        store = {"map": {"NVDA.US": "Halbleiter"}, "delisted": {"NVDA.US": False},
                 "checked": {"NVDA.US", "AAPL.US"}}
        pfad = os.path.join(_INT, "tests", ".tmp_persistent.json")
        try:
            _speichere_persistent(store, pfad)
            wieder = lade_persistent(pfad)
        finally:
            if os.path.exists(pfad):
                os.remove(pfad)
        self.assertEqual(wieder["map"], {"NVDA.US": "Halbleiter"})
        self.assertEqual(wieder["checked"], {"NVDA.US", "AAPL.US"})  # geprüfte Ticker überleben -> resumebar
        self.assertNotIn("AAPL.US", wieder["map"])                   # nur Treffer in der Map


class TestFetchFehlerNichtGecacht(unittest.TestCase):
    """QS-Claude-B4/B5: ein Abruf-Fehler wird NICHT als leer gecacht (retry beim nächsten Lauf)."""

    def test_fehler_nicht_gecacht(self):
        import retro_kat_map_breit as mod
        from retro_kat_map_breit import KlassifikationFehler, klassifikation_map_live
        ruft = {"n": 0}

        def fake(sid, api_token=None, timeout=20):
            ruft["n"] += 1
            raise KlassifikationFehler("simulierter Auth-Fehler")

        orig, mod.fetch_klassifikation = mod.fetch_klassifikation, fake
        pfad = os.path.join(_INT, "tests", ".tmp_cache_test.json")
        try:
            r1 = klassifikation_map_live(["X.US"], cache_pfad=pfad)
            r2 = klassifikation_map_live(["X.US"], cache_pfad=pfad)
        finally:
            mod.fetch_klassifikation = orig
            if os.path.exists(pfad):
                os.remove(pfad)
        self.assertEqual(r1, {})                     # Fehler -> nicht in der Map
        self.assertEqual(ruft["n"], 2)               # beim 2. Lauf ERNEUT versucht (nicht als leer gecacht)


class TestTageslimit(unittest.TestCase):
    """Das EODHD-Tageslimit ist NICHT transient (hält den ganzen Tag) → sofort abbrechen, nicht durch alle
    Rest-Symbole spinnen (je ~14 s Backoff = Stunden für nichts). Realdaten-nah: exakter EODHD-Antwort-Text."""

    def test_tageslimit_sofort_ohne_retry(self):
        import retro_kat_map_breit as mod
        from retro_kat_map_breit import TageslimitErreicht
        ruft = {"n": 0}

        class FakeOut:
            returncode = 0
            # echter EODHD-Antwortkörper bei erschöpfter Tagesquota
            stdout = "You exceeded your daily API requests limit.  Please contact support@eodhistoricaldata.com"

        def fake_run(*a, **k):
            ruft["n"] += 1
            return FakeOut()

        orig, mod.subprocess.run = mod.subprocess.run, fake_run
        try:
            with self.assertRaises(TageslimitErreicht):
                mod._fetch_full_fundamentals("AAPL.US", api_token="x")
        finally:
            mod.subprocess.run = orig
        self.assertEqual(ruft["n"], 1)               # GENAU ein Call — kein Backoff/Retry (nicht-transient)


class TestKeineFundamentaldaten(unittest.TestCase):
    """Quota-Fix (jetzt auf dem Voll-Dump-Fetch): ein Symbol mit LEERER/Not-Found-Antwort = kein
    Fundamentaldatensatz (delisteter Schwanz) → `{}` (No-Data-Marker, KEIN Wurf), damit Cache/Aufrufer es
    NIE WIEDER abrufen. Generisches/transientes Fehler-Objekt bleibt fail-loud (QS-B4)."""

    def _fetch_mit_body(self, body_str):
        import retro_kat_map_breit as mod

        class FakeOut:
            returncode = 0
            stdout = body_str

        def fake_run(*a, **k):
            self._n += 1
            return FakeOut()

        self._n = 0
        orig, mod.subprocess.run = mod.subprocess.run, fake_run
        try:
            return mod._fetch_full_fundamentals("X.US", api_token="x")
        finally:
            mod.subprocess.run = orig

    def test_leeres_objekt_ist_no_data(self):
        # {}/[]/null/"" nach den Fehler-Checks = No-Data-Symbol → {}-Marker, KEIN Wurf, genau ein Call
        for body in ("{}", "[]", "null", '""'):
            self.assertEqual(self._fetch_mit_body(body), {}, f"body={body!r}")
            self.assertEqual(self._n, 1)

    def test_not_found_objekt_ist_no_data(self):
        # QS-Gemini-B2: eindeutiger Not-Found (nicht-leer, aber 404 / "not found") → {}-Marker, KEIN Wurf
        for body in ('{"code":404,"message":"Not Found"}',
                     '{"message":"Symbol not found"}',
                     '{"error":"no data available"}'):
            self.assertEqual(self._fetch_mit_body(body), {}, f"body={body!r}")

    def test_fehler_objekt_bleibt_fail_loud(self):
        # NICHT-leeres, GENERISCHES Objekt ohne General (transient/unbekannt) → weiter B4-Wurf (nie cachen)
        import retro_kat_map_breit as mod
        from retro_kat_map_breit import KlassifikationFehler
        for body in ('{"error":"unexpected"}', '{"message":"Internal Server Error"}', '{"code":500}'):
            with self.assertRaises(KlassifikationFehler, msg=body):
                self._fetch_mit_body(body)

    def test_voll_dump_mit_general_wird_durchgereicht(self):
        # eine echte (verschachtelte) Voll-Antwort mit General-Block wird als kompletter dict zurückgegeben
        d = self._fetch_mit_body('{"General":{"GicSubIndustry":"Semiconductors"},"Highlights":{}}')
        self.assertEqual(d["General"]["GicSubIndustry"], "Semiconductors")


class TestKlassifikationExtractor(unittest.TestCase):
    """`klassifikation_aus_fundamentals` ist verschachtelungs-tolerant: liest sowohl den Voll-Dump
    (`General.GicSubIndustry`) als auch die alte gefilterte Form (`General::GicSubIndustry`)."""

    def test_voll_dump_nested(self):
        import retro_kat_map_breit as mod
        full = {"General": {"GicSubIndustry": "Semiconductors", "Description": "chips"},
                "Highlights": {"MarketCapitalization": 1e12}}
        self.assertEqual(mod.klassifikation_aus_fundamentals(full),
                         {"gic": "Semiconductors", "desc": "chips", "mcap": 1e12})

    def test_alte_flache_form(self):
        import retro_kat_map_breit as mod
        flat = {"General::GicSubIndustry": "Copper", "General::Description": "mining",
                "Highlights::MarketCapitalization": 5e9}
        self.assertEqual(mod.klassifikation_aus_fundamentals(flat)["gic"], "Copper")

    def test_no_data_leere_felder(self):
        import retro_kat_map_breit as mod
        self.assertEqual(mod.klassifikation_aus_fundamentals({}),
                         {"gic": "", "desc": "", "mcap": 0.0})

    def test_no_data_landet_in_checked_nicht_in_map(self):
        # der eigentliche Quota-Fix: No-Data → geprüft-markiert (nie wieder gezogen), nicht gemappt
        import retro_kat_map_breit as mod
        store = {"map": {}, "delisted": {}, "checked": set()}
        felder = mod.klassifikation_aus_fundamentals({})           # No-Data-Dump → leere Felder
        kat = mod._klassifiziere_symbol(felder, gic_direkt=True)   # leeres gic → keine Kategorie
        self.assertIsNone(kat)
        mod._merge_ergebnis(store, "X.US", kat, False)
        self.assertIn("X.US", store["checked"])
        self.assertNotIn("X.US", store["map"])


class TestDriveSyncHook(unittest.TestCase):
    """Der Drive-Sync hängt als `nach_commit`-Callback in der Commit-Kadenz des Grinds — er MUSS feuern
    (Reclaim-Sicherheit des Voll-Dump-Caches). Mock: Mini-Universum, kein Netz, kein echtes git."""

    def test_nach_commit_feuert(self):
        import retro_kat_map_breit as mod
        gerufen = {"n": 0}
        uni = [{"symbol_id": "AAA.US", "delisted": False}, {"symbol_id": "BBB.US", "delisted": False}]
        orig = (mod.universum_kandidaten, mod.fetch_klassifikation, mod._git_persist)
        mod.universum_kandidaten = lambda *a, **k: (uni, 0, 0)
        mod.fetch_klassifikation = lambda sid, **k: {"gic": "X", "desc": "", "mcap": 0.0}
        mod._git_persist = lambda *a, **k: None                # kein echtes git im Test
        pfad = os.path.join(_INT, "tests", ".tmp_store_hook.json")
        try:
            mod.klassifiziere_universum(exchanges=["US"], gic_direkt=True, commit_alle=1, speicher_alle=1,
                                        pfad=pfad, nach_commit=lambda: gerufen.__setitem__("n", gerufen["n"] + 1))
        finally:
            (mod.universum_kandidaten, mod.fetch_klassifikation, mod._git_persist) = orig
            if os.path.exists(pfad):
                os.remove(pfad)
        self.assertGreater(gerufen["n"], 0)                    # der Sync-Hook wurde aufgerufen

    def test_drive_setup_ohne_creds_inaktiv(self):
        import retro_kat_map_breit as mod
        alt = {k: os.environ.pop(k, None) for k in
               ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")}
        try:
            self.assertIsNone(mod._drive_setup())              # ohne Creds sauber None (kein Abbruch/Lärm)
        finally:
            for k, v in alt.items():
                if v is not None:
                    os.environ[k] = v


class TestBreiteMap(unittest.TestCase):
    def test_crisp_und_fein_kombiniert(self):
        klass = {"NVDA.US": {"gic": "Semiconductors", "desc": ""},
                 "FCX.US": {"gic": "Copper", "desc": ""},
                 "ABBN.SW": {"gic": "Electrical Components & Equipment", "desc": "power transformers and switchgear"},
                 "NEX.PA": {"gic": "Electrical Components & Equipment", "desc": "cables for the grid"},
                 "XOM.US": {"gic": "Integrated Oil & Gas", "desc": "oil"}}
        m = baue_breite_map(klass)
        self.assertEqual(m, {"NVDA.US": "Halbleiter", "FCX.US": "Kupfer",
                             "ABBN.SW": "Transformatoren", "NEX.PA": "Stromnetz"})
        self.assertNotIn("XOM.US", m)

    def test_rueckwaertskompatibel_gic_string(self):
        # ein reiner gic-String wird als {gic} behandelt (nur crisp greift)
        self.assertEqual(baue_breite_map({"NVDA.US": "Semiconductors"}), {"NVDA.US": "Halbleiter"})


class TestHybrid(unittest.TestCase):
    def test_gold_kategorien_kommen_dazu(self):
        breit = {"XLNX.US": "Halbleiter"}                     # delisteter Halbleiter (survivorship-frei)
        gold = {"ABBN.SW": "Transformatoren", "PWR.US": "Stromnetz", "NVDA.US": "Halbleiter"}
        h = kombiniere(breit, gold_map=gold)
        self.assertEqual(h["XLNX.US"], "Halbleiter")          # breit erhalten
        self.assertEqual(h["ABBN.SW"], "Transformatoren")     # kuratiert nur aus Gold
        self.assertEqual(h["PWR.US"], "Stromnetz")
        self.assertEqual(h["NVDA.US"], "Halbleiter")          # Gold-Ticker der breiten Kategorie ergänzt
        # crisp- und feine Kategorien sind disjunkt (verschiedene Klassifikationswege)
        self.assertTrue(_CRISP_KATEGORIEN.isdisjoint(_FEIN_KATEGORIEN))


class TestBericht(unittest.TestCase):
    def test_survivorship_anteil(self):
        hybrid = {"NVDA.US": "Halbleiter", "XLNX.US": "Halbleiter", "FCX.US": "Kupfer"}
        universum = {"NVDA.US": {"delisted": False}, "XLNX.US": {"delisted": True},
                     "FCX.US": {"delisted": False}}
        b = kategorie_bericht(hybrid, universum)
        self.assertEqual(b["n_symbole"], 3)
        self.assertEqual(b["je_kategorie"], {"Halbleiter": 2, "Kupfer": 1})
        self.assertEqual(b["delistet_je_kategorie"], {"Halbleiter": 1})   # der delistete Halbleiter zählt
        self.assertEqual(b["crisp_kategorien"], ["Halbleiter", "Kupfer", "Rechenzentrum"])
        self.assertEqual(b["feine_kategorien"], ["Stromnetz", "Transformatoren"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
