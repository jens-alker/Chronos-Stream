"""
test_fundamentals_backfill.py — reiner Kern + Loop-Mechanik des Fundamentals-Backfills (offline, KEIN
Live-Abruf). Prüft: die Rückstands-Berechnung (gemappt−gecacht), Determinismus/Sortierung, den Deckel je
Lauf, den Tageslimit-Fail-fast (kein Weiter-Spinnen), das Doppel-Skip nach Restore, die No-Data-Zählung und
die Wiederaufnehmbarkeit (ein zweiter Lauf zieht nur den echten Rest).

Der Fetcher ist injiziert (Fake), damit der Test die ECHTE Loop-Logik fährt, ohne EODHD zu rufen — die
Live-Naht (`fundamentals_cache.hole` × `_fetch_full_fundamentals`) ist separat geprüft/geteilt und gilt hier
als vorausgesetzt.

Ausführen:  python3 System/integration/tests/test_fundamentals_backfill.py
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

import fundamentals_backfill as fb                                    # noqa: E402


class _Tageslimit(RuntimeError):
    pass


class FakeCache:
    """Ein injizierbarer Cache-Stand + Fetcher-Fake: `gecacht` = Menge bereits gecachter Symbole; `antworten`
    steuert je Symbol das Fetch-Ergebnis ('data' | 'no_data' | 'tageslimit' | 'transient')."""
    def __init__(self, gecacht=None, antworten=None):
        self.gecacht = set(gecacht or [])
        self.antworten = antworten or {}
        self.rufe = []

    def ist_gecacht(self, sid):
        return sid in self.gecacht

    def hole(self, sid):
        self.rufe.append(sid)
        art = self.antworten.get(sid, "data")
        if art == "tageslimit":
            raise _Tageslimit("EODHD-Tageslimit erschöpft")
        if art == "transient":
            raise RuntimeError("EODHD nicht erreichbar: rc=28")
        data = {} if art == "no_data" else {"General": {"Code": sid}, "Financials": {}}
        self.gecacht.add(sid)                            # echter Cache-Effekt (hole speichert)
        return data


class TestReinerKern(unittest.TestCase):
    def test_rueckstand_gemappt_minus_gecacht(self):
        kat_map = {"A.US": "Pharma", "B.US": "Halbleiter", "C.US": "Kupfer"}
        c = FakeCache(gecacht={"B.US"})
        offen = fb.zu_backfillende_symbole(kat_map, c.ist_gecacht)
        self.assertEqual(offen, ["A.US", "C.US"])        # sortiert, B.US (gecacht) raus

    def test_deterministisch_sortiert(self):
        kat_map = {"Z.US": "x", "A.US": "y", "M.US": "z"}
        c = FakeCache()
        self.assertEqual(fb.zu_backfillende_symbole(kat_map, c.ist_gecacht), ["A.US", "M.US", "Z.US"])

    def test_leerer_map_eintrag_uebersprungen(self):
        c = FakeCache()
        self.assertEqual(fb.zu_backfillende_symbole({"": "x", "A.US": "y"}, c.ist_gecacht), ["A.US"])

    def test_lade_kat_map_format(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"map": {"A.US": "Pharma"}, "delisted": {}, "checked": []}, f)
            p = f.name
        try:
            self.assertEqual(fb.lade_kat_map(p), {"A.US": "Pharma"})
        finally:
            os.unlink(p)

    def test_lade_kat_map_fehlt(self):
        self.assertEqual(fb.lade_kat_map("/nicht/da.json"), {})

    def test_lade_universum_vereinigt_regionen(self):
        # Jens 29.07.: US+EU+Asien. Die Regions-Maps werden zu EINEM Universum vereinigt (börsen-suffigiert,
        # kollisionsfrei). Wir monkeypatchen die Pfade auf temporäre Mini-Maps, um die Vereinigung offline
        # zu prüfen (ohne die echten großen Maps zu laden).
        import json
        import tempfile
        pfade = {}
        for region, syms in (("us", {"A.US": "Pharma"}), ("eu", {"B.LSE": "Kupfer"}),
                             ("asien", {"C.TSE": "Halbleiter"})):
            p = os.path.join(tempfile.mkdtemp(), f"{region}.json")
            with open(p, "w") as f:
                json.dump({"map": syms}, f)
            pfade[region] = p
        orig = fb.region_pfade
        fb.region_pfade = lambda: pfade
        try:
            uni = fb.lade_universum()
            self.assertEqual(set(uni), {"A.US", "B.LSE", "C.TSE"})          # alle drei Regionen vereinigt
            self.assertEqual(set(fb.lade_universum(["eu"])), {"B.LSE"})     # Teilmenge wählbar
        finally:
            fb.region_pfade = orig


class TestBackfillLoop(unittest.TestCase):
    def test_zieht_offenen_rest(self):
        c = FakeCache(antworten={"A.US": "data", "B.US": "no_data", "C.US": "data"})
        offen = ["A.US", "B.US", "C.US"]
        b = fb.backfill(offen, c.hole, c.ist_gecacht, _Tageslimit)
        self.assertEqual(b["n_offen_start"], 3)
        self.assertEqual(b["n_gezogen"], 2)              # A, C = echte Daten
        self.assertEqual(b["n_no_data"], 1)              # B = No-Data-Marker
        self.assertFalse(b["tageslimit_erreicht"])
        self.assertEqual(b["n_rest"], 0)                 # alles gecacht

    def test_deckel_pro_lauf(self):
        c = FakeCache(antworten={s: "data" for s in ["A.US", "B.US", "C.US", "D.US"]})
        offen = ["A.US", "B.US", "C.US", "D.US"]
        b = fb.backfill(offen, c.hole, c.ist_gecacht, _Tageslimit, max_pro_lauf=2)
        self.assertEqual(c.rufe, ["A.US", "B.US"])       # nur 2 gezogen
        self.assertEqual(b["n_gezogen"], 2)
        self.assertEqual(b["n_rest"], 2)                 # C, D bleiben offen

    def test_tageslimit_fail_fast(self):
        # B.US löst das Tageslimit aus → C.US/D.US werden NICHT mehr gerufen (kein Spinnen).
        c = FakeCache(antworten={"A.US": "data", "B.US": "tageslimit", "C.US": "data", "D.US": "data"})
        offen = ["A.US", "B.US", "C.US", "D.US"]
        b = fb.backfill(offen, c.hole, c.ist_gecacht, _Tageslimit)
        self.assertEqual(c.rufe, ["A.US", "B.US"])       # nach B abgebrochen
        self.assertTrue(b["tageslimit_erreicht"])
        self.assertEqual(b["n_gezogen"], 1)              # nur A
        self.assertEqual(b["n_rest"], 3)                 # B, C, D offen

    def test_transient_zaehlt_und_faehrt_fort(self):
        c = FakeCache(antworten={"A.US": "transient", "B.US": "data"})
        offen = ["A.US", "B.US"]
        b = fb.backfill(offen, c.hole, c.ist_gecacht, _Tageslimit)
        self.assertEqual(b["n_transient"], 1)            # A transient → nicht abgehakt
        self.assertEqual(b["n_gezogen"], 1)              # B trotzdem gezogen
        self.assertEqual(b["n_rest"], 1)                 # A bleibt offen (retry nächster Lauf)

    def test_doppel_skip_nach_restore(self):
        # B.US ist zu Beginn schon gecacht (Restore) → wird übersprungen, nicht gerufen.
        c = FakeCache(gecacht={"B.US"}, antworten={"A.US": "data"})
        offen = ["A.US", "B.US"]                          # B war beim Snapshot noch offen
        b = fb.backfill(offen, c.hole, c.ist_gecacht, _Tageslimit)
        self.assertEqual(c.rufe, ["A.US"])               # B übersprungen
        self.assertEqual(b["n_rest"], 0)

    def test_nach_batch_kadenz(self):
        c = FakeCache(antworten={s: "data" for s in ["A.US", "B.US", "C.US"]})
        syncs = []
        fb.backfill(["A.US", "B.US", "C.US"], c.hole, c.ist_gecacht, _Tageslimit,
                    batch_groesse=2, nach_batch=lambda: syncs.append(1))
        # 3 Züge, batch=2 → ein Sync nach 2 + ein Schluss-Sync nach dem Rest.
        self.assertEqual(len(syncs), 2)

    def test_wiederaufnehmbar(self):
        # Lauf 1 gedeckelt auf 2, Lauf 2 zieht den Rest — nichts doppelt.
        c = FakeCache(antworten={s: "data" for s in ["A.US", "B.US", "C.US", "D.US"]})
        offen = ["A.US", "B.US", "C.US", "D.US"]
        fb.backfill(offen, c.hole, c.ist_gecacht, _Tageslimit, max_pro_lauf=2)
        offen2 = fb.zu_backfillende_symbole(
            {s: "x" for s in ["A.US", "B.US", "C.US", "D.US"]}, c.ist_gecacht)
        self.assertEqual(offen2, ["C.US", "D.US"])       # nur der echte Rest
        b2 = fb.backfill(offen2, c.hole, c.ist_gecacht, _Tageslimit)
        self.assertEqual(b2["n_rest"], 0)
        self.assertEqual(sorted(set(c.rufe)), ["A.US", "B.US", "C.US", "D.US"])  # jedes genau einmal


if __name__ == "__main__":
    unittest.main()
