"""Test epo_abstract_backfill: der reine Kern (Ref-Parse, Stumpf-Auswahl, nicht-destruktives Anhängen) mit
injiziertem Fetch/Parse — offline, ohne scraper.db/EPO. Der Live-Fetch ist der bestehende epo_ops-Konnektor."""
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INT = os.path.dirname(_HERE)
if _INT not in sys.path:
    sys.path.insert(0, _INT)

import epo_abstract_backfill as B        # noqa: E402


class TestPatentIdZuRef(unittest.TestCase):
    def test_zerlegung(self):
        self.assertEqual(B._patent_id_zu_ref("KR20180138273A"), "KR.20180138273.A")
        self.assertEqual(B._patent_id_zu_ref("EP3276660A1"), "EP.3276660.A1")
        self.assertEqual(B._patent_id_zu_ref("US10123456B2"), "US.10123456.B2")

    def test_unzerlegbar_ist_none(self):
        self.assertIsNone(B._patent_id_zu_ref(""))
        self.assertIsNone(B._patent_id_zu_ref("kaputt"))
        self.assertIsNone(B._patent_id_zu_ref(None))


class TestBackfill(unittest.TestCase):
    def _db(self):
        db = os.path.join(tempfile.mkdtemp(), "s.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, source_type TEXT, title TEXT, text TEXT, "
                  "url TEXT, published_at TEXT)")
        rows = [
            (1, "epo", "T1", "T1 H01L", "KR20180138273A", "2018-01-01"),      # Stumpf -> anreichern
            (2, "patent", "T2", "T2 H02M", "EP3276660A1", "2018-02-01"),      # Stumpf (normalisiert) -> anreichern
            (3, "epo", "T3", "x" * 400, "US10123456B2", "2018-03-01"),        # schon lang -> NICHT in der Menge
            (4, "epo", "T4", "T4 H01L", "kaputt_id", "2018-04-01"),           # unparsebare url -> übersprungen
            (5, "paper", "P", "kurz", "arxiv:1", "2018-05-01"),               # kein Patent -> ignoriert
        ]
        for r in rows:
            c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", r)
        c.commit()
        c.close()
        return db

    def test_zu_backfillende_nur_kurze_patente(self):
        ids = [r[0] for r in B.zu_backfillende(self._db())]
        self.assertEqual(ids, [1, 2, 4])                        # 3 (lang) + 5 (kein Patent) draußen

    def test_backfill_haengt_abstract_an_nicht_destruktiv(self):
        db = self._db()
        abstracts = {"KR.20180138273.A": "novel thin film dispersion method",
                     "EP.3276660.A1": "tilting solar cell module mechanism"}

        def fake_fetch(ref, token):
            return {"ref": ref}                                 # roh; parse_fn liest daraus

        def fake_parse(js):
            ref = js.get("ref")
            pid = ref.replace(".", "") if ref else ""           # "KR.20180138273.A" -> "KR20180138273A"
            return {"patent_id": pid, "abstract": abstracts.get(ref, "")}

        r = B.backfill(db, token="tok", fetch_fn=fake_fetch, parse_fn=fake_parse, drossel_sek=0,
                       fortschritt=False)
        self.assertEqual(r["n_angereichert"], 2)
        self.assertEqual(r["n_unparsebar"], 1)                  # doc 4 (kaputt_id)
        c = sqlite3.connect(db)
        t1 = c.execute("SELECT text FROM documents WHERE id=1").fetchone()[0]
        self.assertEqual(t1, "T1 H01L novel thin film dispersion method")   # angehängt, Titel+IPC bleibt
        t3 = c.execute("SELECT text FROM documents WHERE id=3").fetchone()[0]
        self.assertEqual(t3, "x" * 400)                        # unberührt (war nicht in der Menge)
        c.close()

    def test_backfill_ohne_abstract_laesst_text(self):
        db = self._db()
        r = B.backfill(db, token="tok", fetch_fn=lambda ref, tok: {}, drossel_sek=0,
                       parse_fn=lambda js: {"patent_id": "", "abstract": ""}, fortschritt=False)
        self.assertEqual(r["n_angereichert"], 0)
        self.assertEqual(r["n_kein_abstract"], 2)              # doc 1+2 (doc 4 unparsebar)
        c = sqlite3.connect(db)
        self.assertEqual(c.execute("SELECT text FROM documents WHERE id=1").fetchone()[0], "T1 H01L")
        c.close()

    def test_control_stop_bricht_verlustfrei_ab(self):
        db = self._db()
        r = B.backfill(db, token="tok", fetch_fn=lambda ref, tok: {}, parse_fn=lambda js: {"abstract": "x"},
                       control_fn=lambda: "stop", drossel_sek=0, fortschritt=False)
        self.assertEqual(r["n_angereichert"], 0)               # vor dem ersten Doc gestoppt

    def test_idempotent_kein_doppel_anhaengen(self):
        # Fable Blocker-2: ein zweiter Lauf darf den (auch kurzen) Abstract NICHT erneut anhängen (Marker).
        db = self._db()
        fetch, parse = self._abstract_fakes({"KR.20180138273.A": "short abs", "EP.3276660.A1": "short abs2"})
        B.backfill(db, token="t", fetch_fn=fetch, parse_fn=parse, drossel_sek=0, fortschritt=False)
        c = sqlite3.connect(db)
        t1_nach1 = c.execute("SELECT text FROM documents WHERE id=1").fetchone()[0]
        c.close()
        r2 = B.backfill(db, token="t", fetch_fn=fetch, parse_fn=parse, drossel_sek=0, fortschritt=False)
        c = sqlite3.connect(db)
        t1_nach2 = c.execute("SELECT text FROM documents WHERE id=1").fetchone()[0]
        c.close()
        self.assertEqual(t1_nach1, "T1 H01L short abs")
        self.assertEqual(t1_nach2, t1_nach1)                   # unverändert — NICHT doppelt angehängt
        self.assertEqual(r2["n_offen"], 0)                     # 2. Lauf: nichts mehr offen (Marker)

    def test_facts_done_wird_invalidiert(self):
        # Fable Blocker-1: nach Anreicherung muss die facts_done-Zeile weg sein, sonst re-extrahiert 1c nie.
        db = self._db()
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE facts_done (doc_id INTEGER PRIMARY KEY, n_facts INTEGER)")
        c.execute("INSERT INTO facts_done VALUES (1,0)")       # Stumpf war schon 1c-'erledigt' (0 Fakten)
        c.execute("INSERT INTO facts_done VALUES (2,0)")
        c.commit()
        c.close()
        fetch, parse = self._abstract_fakes({"KR.20180138273.A": "new abstract prose here"})   # nur doc 1 hat abstract
        B.backfill(db, token="t", fetch_fn=fetch, parse_fn=parse, drossel_sek=0, fortschritt=False)
        c = sqlite3.connect(db)
        self.assertIsNone(c.execute("SELECT 1 FROM facts_done WHERE doc_id=1").fetchone())   # invalidiert -> 1c re-extrahiert
        self.assertIsNotNone(c.execute("SELECT 1 FROM facts_done WHERE doc_id=2").fetchone())  # ohne Abstract: unberührt
        c.close()

    def test_kein_abstract_wird_markiert_nicht_neu_abgerufen(self):
        # Fable Blocker-2b: ein abstract-loses Patent wird markiert -> beim nächsten Lauf NICHT erneut abgerufen.
        db = self._db()
        rufe = []

        def fetch(ref, tok):
            rufe.append(ref)
            return {}

        parse = lambda js: {"patent_id": "", "abstract": ""}
        B.backfill(db, token="t", fetch_fn=fetch, parse_fn=parse, drossel_sek=0, fortschritt=False)
        n1 = len(rufe)
        B.backfill(db, token="t", fetch_fn=fetch, parse_fn=parse, drossel_sek=0, fortschritt=False)
        self.assertEqual(len(rufe), n1)                        # 2. Lauf ruft NICHTS erneut ab (Marker)

    def test_token_refresh_nach_n(self):
        # Fable Major-3: das Token wird alle token_refresh_n Docs erneuert (sonst faulten späte Abrufe).
        db = self._db()
        geholt = []

        def token_fn():
            geholt.append(1)
            return f"tok{len(geholt)}"

        fetch, parse = self._abstract_fakes({"KR.20180138273.A": "a", "EP.3276660.A1": "b"})
        B.backfill(db, token=None, token_fn=token_fn, token_refresh_n=1, fetch_fn=fetch, parse_fn=parse,
                   drossel_sek=0, fortschritt=False)
        self.assertGreaterEqual(len(geholt), 2)                # initial + mind. 1 Refresh über die Docs

    def _abstract_fakes(self, ref_zu_abstract):
        def fetch(ref, tok):
            return {"ref": ref}

        def parse(js):
            ref = js.get("ref", "")
            return {"patent_id": ref.replace(".", ""), "abstract": ref_zu_abstract.get(ref, "")}
        return fetch, parse


if __name__ == "__main__":
    unittest.main(verbosity=2)
