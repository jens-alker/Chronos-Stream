"""
test_scraper_harvester.py — Tests für Modul 1 (scraper.py): die bisher ungetestete
Produzenten-Seite. Deckt die Best-in-Class-Nachrüstung ab:

  * robuste Feed-Parser (ElementTree, namespace-/CDATA-/Entity-fest) + Regex-Fallback
  * Abstract-Anreicherung (arXiv <summary>, OpenAlex inverted-index, EDGAR-Felder)
  * schema-constrained decoding (Ollama JSON-Schema-Pfad)
  * Semantik-Dedup (Kosinus) + nicht-destruktive Near-Dup-Markierung
  * Relevanz-Feedback-Loop (Korrekturen protokollieren + als Few-Shot spiegeln)
  * Migrations-Idempotenz (läuft gefahrlos mehrfach auf der Bestands-DB)

Realitätsnahe Fixtures (Guardrail „keine Schein-Tests"): die XML/JSON-Blöcke sind
formattreu zu den echten API-Antworten (arXiv Atom, RSS 2.0, OpenAlex, EDGAR FTS).
LLM-frei/deterministisch — der LLM-Weg wird über einen Fake-Aufruf getestet.

Ausführen:  python3 System/tests/test_scraper_harvester.py
"""
import os
import sqlite3
import sys
import unittest
import urllib.error

_SYSTEM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYSTEM not in sys.path:
    sys.path.insert(0, _SYSTEM)

import scraper as s                                                     # noqa: E402


def _mem_db():
    """Frische In-Memory-Sammler-DB mit dem echten Schema (für Migrations-/DB-Tests)."""
    s.DB = sqlite3.connect(":memory:", check_same_thread=False)
    s.DB.row_factory = sqlite3.Row
    s.DB.executescript(s.SCHEMA)
    s.DB.commit()
    return s.DB


# ------------------------------------------------------------------
#  Feed-Parsing (#4) + Abstract-Anreicherung (#1)
# ------------------------------------------------------------------
ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Datacenter Electricity Demand under AI Expansion</title>
    <summary>  We model grid-scale electricity demand driven by hyperscale
    datacenter buildout and find a 40% capex acceleration in 2025. </summary>
    <published>2024-03-05T10:00:00Z</published>
    <link href="http://arxiv.org/abs/2403.00001v1" rel="alternate"/>
  </entry>
  <entry>
    <title>arXiv Query Result Placeholder</title>
    <summary>should be skipped</summary>
    <published>2024-03-05T10:00:00Z</published>
  </entry>
</feed>"""

RSS_20 = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title><![CDATA[Chip & substrate shortage worsens]]></title>
<link>https://example.com/a</link>
<description><![CDATA[<p>Suppliers warn of <b>bottleneck</b> into 2027.</p>]]></description>
<pubDate>Tue, 05 Mar 2024 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><title>Grid storage tender awarded</title>
  <summary>Utility signs 500MWh order.</summary>
  <updated>2024-02-01T00:00:00Z</updated>
  <link href="https://ex.org/x" rel="alternate"/></entry>
</feed>"""


class TestFeedParsing(unittest.TestCase):
    def test_arxiv_zieht_abstract(self):
        # #1: der Abstract (<summary>) MUSS mit in den Text — vorher nur Titel.
        items = s._feed_items(ARXIV_ATOM)
        self.assertEqual(len(items), 2)
        title = s._clean(s._child_text(items[0], "title"))
        abstract = s._clean(s._child_text(items[0], "summary"))
        self.assertIn("capex acceleration", abstract)
        self.assertEqual(s._child_link(items[0]), "http://arxiv.org/abs/2403.00001v1")
        self.assertEqual(s._norm_date(s._child_text(items[0], "published")), "2024-03-05")

    def test_rss_cdata_html_entity_rfc822(self):
        docs = s._parse_feed(RSS_20, "news", limit=10)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["title"], "Chip & substrate shortage worsens")
        self.assertIn("bottleneck", docs[0]["text"])         # HTML gestrippt, Text erhalten
        self.assertNotIn("<b>", docs[0]["text"])
        self.assertEqual(docs[0]["published_at"], "2024-03-05")
        self.assertEqual(docs[0]["url"], "https://example.com/a")

    def test_atom_updated_und_href(self):
        docs = s._parse_feed(ATOM_FEED, "funding", limit=10)
        self.assertEqual(docs[0]["source_type"], "funding")
        self.assertEqual(docs[0]["url"], "https://ex.org/x")
        self.assertEqual(docs[0]["published_at"], "2024-02-01")

    def test_kaputtes_xml_faellt_auf_regex(self):
        # Unabgeschlossenes XML mit unmaskiertem & -> ET scheitert -> Regex rettet.
        broken = ('<rss><channel><item><title>Broken AT&T earnings beat</title>'
                  '<link>x</link></item>')
        docs = s._parse_feed(broken, "news")
        self.assertEqual(len(docs), 1)
        self.assertIn("AT&T", docs[0]["title"])

    def test_norm_date_formate(self):
        self.assertEqual(s._norm_date("2024-03-05T10:00:00Z"), "2024-03-05")
        self.assertEqual(s._norm_date("20240305"), "2024-03-05")
        self.assertEqual(s._norm_date("Tue, 05 Mar 2024 08:00:00 GMT"), "2024-03-05")
        self.assertIsNone(s._norm_date("kein datum"))
        self.assertIsNone(s._norm_date(""))

    def test_leerer_titel_wird_uebersprungen(self):
        empty = "<rss><channel><item><title></title><link>x</link></item></channel></rss>"
        self.assertEqual(s._parse_feed(empty, "news"), [])


class TestAbstractAnreicherung(unittest.TestCase):
    def test_openalex_inverted_index(self):
        inv = {"Grid": [0], "storage": [2], "demand": [1], "rises": [3]}
        self.assertEqual(s._openalex_abstract(inv), "Grid demand storage rises")

    def test_openalex_leer_robust(self):
        self.assertEqual(s._openalex_abstract(None), "")
        self.assertEqual(s._openalex_abstract({}), "")
        self.assertEqual(s._openalex_abstract("nonsense"), "")

    def test_edgar_context_reich_statt_name(self):
        src = {"display_names": ["VoltGrid Inc. (CIK 0001234567)"],
               "file_description": "Form D", "sics": [3690], "inc_states": ["DE"]}
        txt = s._edgar_context(src, "Form D — Frühfinanzierung:")
        self.assertIn("VoltGrid", txt)
        self.assertIn("SIC 3690", txt)
        self.assertIn("Form D — Frühfinanzierung:", txt)

    def test_edgar_context_leer_faellt_nicht(self):
        self.assertEqual(s._edgar_context({}), "")


# ------------------------------------------------------------------
#  Harvester end-to-end (echte h_*-Funktionen gegen gemockte HTTP-Antwort)
# ------------------------------------------------------------------
class TestHarvesterEndToEnd(unittest.TestCase):
    def setUp(self):
        self._orig_get = s._get

    def tearDown(self):
        s._get = self._orig_get

    def test_h_arxiv_zieht_abstract_in_text(self):
        s._get = lambda u, **k: ARXIV_ATOM
        out, cur = s.h_arxiv({}, None)
        self.assertEqual(len(out), 1)                    # Placeholder-Eintrag verworfen
        self.assertEqual(out[0]["source_type"], "paper")
        self.assertIn("capex acceleration", out[0]["text"])   # #1: Abstract im Text
        self.assertNotEqual(out[0]["text"], out[0]["title"])  # nicht mehr Titel-only

    def test_h_rss_generisch(self):
        s._get = lambda u, **k: RSS_20
        out, cur = s.h_rss({"endpoint": "http://x", "source_type": "news"}, None)
        self.assertEqual(len(out), 1)
        self.assertIn("bottleneck", out[0]["text"])
        self.assertIsNone(cur)

    def test_h_gnews_topic_braucht_begriff(self):
        s._get = lambda u, **k: RSS_20
        out, _ = s.h_gnews_topic({"endpoint": "private credit"}, None)
        self.assertEqual(len(out), 1)
        with self.assertRaises(RuntimeError):
            s.h_gnews_topic({"endpoint": ""}, None)      # ohne Begriff -> klarer Fehler


# ------------------------------------------------------------------
#  Schema-constrained decoding (#2)
# ------------------------------------------------------------------
class TestSchemaDecoding(unittest.TestCase):
    def setUp(self):
        self._orig = s._ollama_chat
        _mem_db()                          # log()/q() im Fallback-/Salvage-Pfad brauchen DB

    def tearDown(self):
        s._ollama_chat = self._orig
        s.DB = None

    def test_schema_wird_als_format_uebergeben(self):
        seen = {}

        def fake(mdl, prompt, mx, fmt):
            seen["fmt"] = fmt
            return '{"fakten": []}'
        s._ollama_chat = fake
        out = s.local_json("x", schema=s.FACTS_SCHEMA)
        self.assertEqual(out, {"fakten": []})
        self.assertIs(seen["fmt"], s.FACTS_SCHEMA)        # Schema durchgereicht, nicht 'json'

    def test_fallback_auf_json_bei_400(self):
        # Ältere Ollama kennt dict-`format` nicht -> transparent auf 'json' zurück.
        calls = []

        def fake(mdl, prompt, mx, fmt):
            calls.append(fmt)
            if fmt != "json":
                raise urllib.error.HTTPError("u", 400, "bad request", {}, None)
            return '{"ratings":[0.9]}'
        s._ollama_chat = fake
        out = s.local_json("x", schema=s.RELEVANCE_SCHEMA)
        self.assertEqual(out, {"ratings": [0.9]})
        self.assertEqual(calls, [s.RELEVANCE_SCHEMA, "json"])

    def test_ohne_schema_bleibt_json(self):
        seen = {}

        def fake(mdl, prompt, mx, fmt):
            seen["fmt"] = fmt
            return "{}"
        s._ollama_chat = fake
        s.local_json("x")
        self.assertEqual(seen["fmt"], "json")

    def test_salvage_bleibt_netz_bei_abschnitt(self):
        # Selbst wenn (Fallback-)JSON abgeschnitten ist, rettet _salvage_facts.
        def fake(mdl, prompt, mx, fmt):
            return '{"fakten":[{"subjekt":"TSMC","beziehung":"baut","objekt":"Fab"},'
        s._ollama_chat = fake
        out = s.local_json("x")            # kein Schema -> Salvage-Pfad
        self.assertIn("fakten", out)
        self.assertEqual(out["fakten"][0]["subjekt"], "TSMC")


# ------------------------------------------------------------------
#  1c-Stall-Fix: "Modell antwortete unbrauchbar" != "Modell weg"
# ------------------------------------------------------------------
# Realdaten-nah gegen den ECHTEN Ollama->local_json->ask_json->extract_facts-Pfad:
# ein abgeschnittenes Modell-JSON (die Signatur aus scraper.db: 1254x
# "abgeschnitten", 34x bei char 6000) darf 1c NICHT als Ausfall werten — sonst
# pausiert es das Gift-Dokument ewig und der Rueckstand (40k offen) leert nie.
_ABGESCHNITTEN = '{"fakten":[{"subjekt":"' + "X" * 40      # unrettbar: erstes Objekt offen


class TestFactsStallFix(unittest.TestCase):
    def setUp(self):
        _mem_db()
        self._orig = {k: getattr(s, k) for k in
                      ("_ollama_chat", "local_available", "cfg_int", "set_status")}
        self._orig_sleep = s.time.sleep
        self._routing = dict(s.ROUTING)
        self._frontier = dict(s.FRONTIER)
        self._facts = dict(s.FACTS)
        s.local_available = lambda: (True, ["m"])     # Ollama laeuft
        s.ROUTING["facts"] = "local"
        s.FRONTIER["allowed"] = False                 # Heim-Lage: kein Cloud-Fallback
        s.set_status = lambda *a, **k: None

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(s, k, v)
        s.time.sleep = self._orig_sleep
        s.ROUTING.clear(); s.ROUTING.update(self._routing)
        s.FRONTIER.clear(); s.FRONTIER.update(self._frontier)
        s.FACTS.clear(); s.FACTS.update(self._facts)
        s.DB = None

    def _add(self, i):
        s.q("INSERT INTO documents(id,source_type,title,text,published_at) "
            "VALUES(?,?,?,?,?)", (i, "paper", f"Doc {i}",
                                  "TSMC baut eine neue Fab in Arizona.", "2024-01-01"),
            fetch=False)

    # --- Seam: extract_facts trennt die DREI Faelle sauber ---
    def test_unlesbar_wirft_unlesbare_nicht_modellweg(self):
        s._ollama_chat = lambda *a, **k: _ABGESCHNITTEN
        with self.assertRaises(s.UnlesbareModellantwort):     # NICHT ModellWeg!
            s.extract_facts({"id": 1, "title": "x", "text": "y"})

    def test_echter_ausfall_wirft_modellweg(self):
        s.local_available = lambda: (False, [])
        with self.assertRaises(s.ModellWeg):
            s.extract_facts({"id": 1, "title": "x", "text": "y"})

    def test_verbindungsfehler_wirft_modellweg(self):
        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")
        s._ollama_chat = boom
        with self.assertRaises(s.ModellWeg):                  # Transportfehler = Ausfall
            s.extract_facts({"id": 1, "title": "x", "text": "y"})

    def test_kaputter_api_umschlag_ist_ausfall(self):
        # _ollama_chat wirft rohen JSONDecodeError (kaputter API-UMSCHLAG, nicht
        # Modell-Inhalt) -> muss als echter Ausfall (ModellWeg) gelten, NICHT als
        # 'unbrauchbar'. Genau dafuer ist UnlesbareModellantwort ein eigener Typ.
        def bad_env(*a, **k):
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        s._ollama_chat = bad_env
        with self.assertRaises(s.ModellWeg):
            s.extract_facts({"id": 1, "title": "x", "text": "y"})

    def test_teilweise_salvage_liefert_fakten(self):
        s._ollama_chat = lambda *a, **k: (
            '{"fakten":[{"subjekt":"TSMC","beziehung":"baut","objekt":"Fab",'
            '"modus":"wird","signalart":"technologie","latenz":"lang"},{"subjekt":"AS')
        out = s.extract_facts({"id": 1, "title": "x", "text": "y"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["subjekt"], "TSMC")

    def test_ask_json_kontrakt_dict_vs_none(self):
        s._ollama_chat = lambda *a, **k: _ABGESCHNITTEN
        self.assertEqual(s.ask_json("facts", "p", schema=s.FACTS_SCHEMA), {})   # unbrauchbar
        s.local_available = lambda: (False, [])
        self.assertIsNone(s.ask_json("facts", "p", schema=s.FACTS_SCHEMA))      # Ausfall
        s.local_available = lambda: (True, ["m"])
        s._ollama_chat = lambda *a, **k: '{"themes":[{"na'
        self.assertIsNone(s.ask_json("ontology", "p", schema=s.ONTOLOGY_SCHEMA))  # andere: None

    # --- Loop: vereinzelt DRAIN, systemisch KEIN Verbrennen ---
    def test_run_extraction_vereinzelt_erledigt_dok(self):
        # DER Kern-Fix: ein Gift-Dokument wird als erledigt (0 Fakten) vermerkt,
        # nicht ewig als 'Modell weg' neu versucht -> der Rueckstand leert.
        s._ollama_chat = lambda *a, **k: _ABGESCHNITTEN
        for i in (1, 2):
            self._add(i)
        s._run_extraction(limit=2)
        self.assertEqual(s.q("SELECT COUNT(*) c FROM facts_done")[0]["c"], 2)   # beide erledigt
        self.assertEqual(s.q("SELECT COUNT(*) c FROM facts")[0]["c"], 0)

    def test_run_extraction_systemisch_verbrennt_korpus_nicht(self):
        # Modell liefert NUR noch Muell -> nach `grenze` in Folge PAUSIEREN, statt
        # den ganzen Rueckstand als '0 Fakten' zu verbrennen (der 2.347-Dok-Fall).
        s._ollama_chat = lambda *a, **k: _ABGESCHNITTEN
        s.cfg_int = lambda k, d=None: (3 if k == "facts_unbrauchbar_grenze"
                                       else self._orig["cfg_int"](k, d))
        s.time.sleep = lambda *a, **k: s.FACTS.__setitem__("running", False)  # Pause -> Stopp
        for i in range(1, 6):
            self._add(i)
        s._run_extraction()
        erledigt = s.q("SELECT COUNT(*) c FROM facts_done")[0]["c"]
        self.assertEqual(erledigt, 2)                  # grenze 3: 2 uebersprungen, 3. loest Pause
        self.assertLess(erledigt, 5)                   # NIE der ganze Korpus
        self.assertFalse(s.FACTS["running"])

    def test_probe_spiegelt_echten_aufruf_think_false(self):
        # Der Gesundheits-Ping muss dieselbe Anfrage wie die echte Extraktion
        # senden (think=False bei qwen3), sonst meldet er faelschlich 500/'haengt'
        # und pausiert Sammler + 1c, obwohl das Modell laeuft.
        import json as J
        import urllib.request as U
        seen = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake_open(req, timeout=None):
            seen["body"] = req.data
            return _Resp()

        orig_open = U.urlopen
        s.local_available = lambda force=False: (True, ["qwen3:30b"])
        s.LOCAL["model"] = "qwen3:30b"
        U.urlopen = fake_open
        try:
            lage, _ = s._ollama_probe()
        finally:
            U.urlopen = orig_open
        self.assertEqual(lage, "ok")
        payload = J.loads(seen["body"])
        self.assertIs(payload.get("think"), False)          # Thinking AUS
        self.assertNotEqual(payload["options"]["num_predict"], 1)   # nicht 1

    def test_schemata_sind_begrenzt(self):
        fk = s.FACTS_SCHEMA["properties"]["fakten"]
        self.assertEqual(fk["maxItems"], 3)
        self.assertEqual(fk["items"]["properties"]["subjekt"]["maxLength"], 200)
        self.assertEqual(fk["items"]["properties"]["beziehung"]["maxLength"], 120)
        self.assertEqual(s.ONTOLOGY_SCHEMA["properties"]["themes"]["maxItems"], 3)

    def test_prevent_sleep_failt_nie(self):
        # Schlaf-Sperre ist Windows-only; ausserhalb (und bei Fehlern) no-op,
        # aber NIE ein Start-Crash.
        s._prevent_sleep()          # darf keine Exception werfen


# ------------------------------------------------------------------
#  Semantik-Dedup (#3) — nicht-destruktiv, resumierbar
# ------------------------------------------------------------------
_VOCAB = ["chip", "substrate", "shortage", "grid", "storage", "battery", "ai", "demand"]


def _fake_embed(text):
    """Deterministisches Fake-Embedding (Wort-Set-Vektor) — testet die Dedup-Logik
    ohne Ollama. Near-identischer Text -> near-identischer Vektor -> Kosinus ~1."""
    t = (text or "").lower()
    return [1.0 if w in t else 0.0 for w in _VOCAB]


class TestSemantikDedup(unittest.TestCase):
    def setUp(self):
        _mem_db()
        self._orig_embed, self._orig_active = s.embed_text, s.any_model_active
        self._orig_store = s._store_embedding
        s.embed_text = _fake_embed
        s.any_model_active = lambda: True            # kein echtes Ollama im Test

    def tearDown(self):
        s.embed_text, s.any_model_active = self._orig_embed, self._orig_active
        s._store_embedding = self._orig_store
        s.DB = None

    def _add(self, source_type, title, text, pub="2024-01-01"):
        return s.q("INSERT INTO documents(source_type,title,text,published_at) "
                   "VALUES(?,?,?,?)", (source_type, title, text, pub), fetch=False)

    def test_cosine(self):
        self.assertAlmostEqual(s._cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(s._cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(s._cosine([], [1]), 0.0)
        self.assertEqual(s._cosine([1, 2], [1]), 0.0)      # Längen ungleich

    def test_near_dup_markiert_statt_loescht(self):
        a = self._add("news", "Chip substrate shortage worsens", "chip substrate shortage")
        self.assertIsNone(s.dedup_dokument(a, "news", "chip substrate shortage"))  # Kanon
        b = self._add("news", "Chip substrate shortage deepens", "chip substrate shortage")
        self.assertEqual(s.dedup_dokument(b, "news", "chip substrate shortage"), a)
        # NICHT gelöscht — beide Dokumente bleiben, b trägt nur dup_of=a:
        self.assertEqual(s.q("SELECT COUNT(*) c FROM documents")[0]["c"], 2)
        self.assertEqual(s.q("SELECT dup_of FROM documents WHERE id=?", (b,))[0]["dup_of"], a)
        # nur der Kanon trägt ein Embedding:
        self.assertEqual(s.q("SELECT COUNT(*) c FROM doc_embeddings")[0]["c"], 1)

    def test_blocking_auf_source_type(self):
        a = self._add("news", "x", "chip substrate shortage")
        s.dedup_dokument(a, "news", "chip substrate shortage")
        b = self._add("paper", "y", "chip substrate shortage")
        self.assertIsNone(s.dedup_dokument(b, "paper", "chip substrate shortage"))

    def test_unter_schwelle_kein_dup(self):
        a = self._add("news", "x", "chip substrate shortage")
        s.dedup_dokument(a, "news", "chip substrate shortage")
        b = self._add("news", "y", "grid storage battery")
        self.assertIsNone(s.dedup_dokument(b, "news", "grid storage battery"))

    def test_backfill_resumierbar_und_idempotent(self):
        for i in range(3):
            self._add("news", f"Chip substrate shortage {i}", "chip substrate shortage")
        s._run_dedup_backfill()
        self.assertEqual(
            s.q("SELECT COUNT(*) c FROM documents WHERE dup_of IS NOT NULL")[0]["c"], 2)
        self.assertEqual(s.q("SELECT COUNT(*) c FROM doc_embeddings")[0]["c"], 1)
        self.assertEqual(s._offene_embeddings(), 0)      # nichts mehr offen -> idempotent
        s._run_dedup_backfill()                          # zweiter Lauf: no-op
        self.assertEqual(s.q("SELECT COUNT(*) c FROM doc_embeddings")[0]["c"], 1)

    def test_backfill_stoppt_sauber_ohne_embed_modell(self):
        s.embed_text = lambda t: None                    # Embed-Modell weg (überall)
        self._add("news", "x", "chip")
        s._run_dedup_backfill()
        self.assertEqual(s.q("SELECT COUNT(*) c FROM doc_embeddings")[0]["c"], 0)
        self.assertFalse(s.DEDUP["running"])             # kein Endloslauf

    def test_qs_major1_leeres_dokument_stoppt_nicht(self):
        # Realdaten-nah: das ECHTE embed_text gibt bei Leerinhalt None (vor dem Netz)
        # — das darf NICHT als 'Modell weg' den ganzen Backfill blockieren.
        def echt_embed(text):
            t = (text or "").strip()
            return _fake_embed(t) if t else None
        s.embed_text = echt_embed
        s.q("INSERT INTO documents(source_type,title,text,published_at) "
            "VALUES('news','','','2024-01-01')", fetch=False)          # inhaltsleer
        gut = self._add("news", "Grid storage battery", "grid storage battery")
        s._run_dedup_backfill()
        self.assertEqual(s._offene_embeddings(), 0)      # beide abgearbeitet, kein Hänger
        self.assertEqual(s.DEDUP["phase"], "fertig")
        self.assertGreaterEqual(
            s.q("SELECT COUNT(*) c FROM doc_embeddings WHERE doc_id=?", (gut,))[0]["c"], 1)

    def test_qs_major3_kein_fortschritt_stoppt(self):
        # INSERT schlägt dauerhaft fehl -> Backfill muss stoppen, nicht endlos laufen.
        self._add("news", "x", "grid storage battery")
        s._store_embedding = lambda *a, **k: False
        s._run_dedup_backfill()
        self.assertFalse(s.DEDUP["running"])

    def test_qs_b4_zeitfenster(self):
        a = self._add("news", "A", "chip substrate shortage", pub="2024-01-01")
        s.dedup_dokument(a, "news", "chip substrate shortage", "2024-01-01")
        # außerhalb des 21-Tage-Fensters -> KEIN Dup (obwohl identischer Inhalt):
        b = self._add("news", "B", "chip substrate shortage", pub="2024-06-01")
        self.assertIsNone(s.dedup_dokument(b, "news", "chip substrate shortage", "2024-06-01"))
        # innerhalb des Fensters -> Dup von a:
        c = self._add("news", "C", "chip substrate shortage", pub="2024-01-10")
        self.assertEqual(s.dedup_dokument(c, "news", "chip substrate shortage", "2024-01-10"), a)


class TestRelevanzFeedback(unittest.TestCase):
    def setUp(self):
        _mem_db()

    def tearDown(self):
        s.DB = None

    def _doc(self, title, rel=0.5, st="paper"):
        return s.q("INSERT INTO documents(source_type,title,text,relevance,published_at) "
                   "VALUES(?,?,?,?,?)", (st, title, title, rel, "2024-01-01"), fetch=False)

    def test_korrektur_wird_als_label_protokolliert(self):
        did = self._doc("Grundlagenbiologie NK-Zellen", rel=0.5)
        alt, neu = s.lerne_relevanz(did, 0.1)
        self.assertEqual((alt, neu), (0.5, 0.1))
        rows = s.q("SELECT * FROM relevanz_urteil")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["neu_score"], 0.1)
        self.assertEqual(s.q("SELECT relevance FROM documents WHERE id=?", (did,))[0]["relevance"], 0.1)

    def test_keine_aenderung_kein_label(self):
        did = self._doc("x", rel=0.5)
        s.lerne_relevanz(did, 0.5)                    # identisch -> kein Label
        self.assertEqual(s.q("SELECT COUNT(*) c FROM relevanz_urteil")[0]["c"], 0)

    def test_anker_balanciert_und_im_prompt(self):
        # Korrekturen über die Skala verteilt -> Anker spannen sie auf.
        for t, r in [("Chip substrate shortage", 0.9), ("Battery pilot line", 0.8),
                     ("NBA free agency", 0.0), ("Horoskop heute", 0.1),
                     ("Materialphysik ohne Anwendung", 0.5)]:
            did = self._doc(t, rel=0.5)
            s.lerne_relevanz(did, r)
        anker = s._relevanz_anker(max_n=6)
        scores = [sc for _, sc in anker]
        self.assertTrue(any(sc >= 0.7 for sc in scores))   # hoch dabei
        self.assertTrue(any(sc < 0.4 for sc in scores))    # niedrig dabei
        prompt = s._relevanz_prompt()
        self.assertIn("KALIBRIERUNG", prompt)
        self.assertIn("Chip substrate shortage", prompt)

    def test_prompt_ohne_korrekturen_ist_basis(self):
        self.assertEqual(s._relevanz_prompt(), s.RELEVANCE_PROMPT)


class TestMigration(unittest.TestCase):
    def test_ensure_column_idempotent(self):
        s.DB = sqlite3.connect(":memory:", check_same_thread=False)
        s.DB.row_factory = sqlite3.Row
        s.DB.executescript("CREATE TABLE documents(id INTEGER PRIMARY KEY, title TEXT);")
        s.DB.commit()
        self.assertTrue(s._ensure_column("documents", "dup_of", "INTEGER"))    # ergänzt
        self.assertFalse(s._ensure_column("documents", "dup_of", "INTEGER"))   # schon da
        s._migrate_schema()
        s._migrate_schema()                              # zweimal = kein Fehler
        cols = [r["name"] for r in s.q("PRAGMA table_info(documents)")]
        self.assertIn("dup_of", cols)
        s.DB = None


class TestStateLight(unittest.TestCase):
    """Der leichte Live-Status (Jens 08.08.): billige Zaehler + Prozess-Flags, KEIN _state_build (72k Docs)."""

    def test_state_light_billig_und_vollstaendig(self):
        _mem_db()
        s.DB.execute("INSERT INTO documents(source_type,title,published_at) VALUES('news','t','2026-01-01')")
        s.DB.execute("INSERT INTO facts(doc_id,subjekt,beziehung,objekt) VALUES(1,'s','b','o')")
        s.DB.commit()
        st = s._state_light()
        self.assertTrue(st["leicht"])
        for k in ("collector", "facts", "dedup", "ollama", "routing", "n_docs", "n_facts", "log"):
            self.assertIn(k, st)
        self.assertEqual(st["n_docs"], 1)
        self.assertEqual(st["n_facts"], 1)
        # KEINE schweren Schluessel (die _state_build hat, _state_light bewusst nicht)
        self.assertNotIn("fact_samples", st)
        self.assertNotIn("docs", st)
        s.DB = None


class TestOllamaStartResolver(unittest.TestCase):
    """Der robuste Ollama-Start-Befehl (Jens 08.08.): ein veralteter `ollama_restart_cmd` aus einer
    Altinstallation darf NIE ein fehlendes File feuern (das loeste ein Windows-'nicht gefunden'-Popup aus)
    -> fail-closed auf die mitgelieferte Ollama_Start.bat neben scraper.py."""

    def setUp(self):
        self._alt = dict(s.CONFIG)

    def tearDown(self):
        s.CONFIG.clear(); s.CONFIG.update(self._alt)

    def _bundled_ok(self):
        # der Bat-Fallback greift nur auf Windows (m4). Auf posix (Testhost) ist der Fallback ['ollama','serve'].
        return os.name == "nt"

    def test_pfad_token_erkennung(self):
        self.assertIsNone(s._fehlender_pfad("ollama serve"))               # kein path-artiges Token
        self.assertIsNone(s._fehlender_pfad(""))
        # ein fehlender Pfad wird gefunden (quotiert, mit Leerzeichen)
        self.assertEqual(s._fehlender_pfad(r'"A:\Alt\Ollama Start.bat"'), r"A:\Alt\Ollama Start.bat")
        # cmd-Schalter /c /k gelten NICHT als Pfad
        self.assertFalse(s._ist_pfad_token("/c"))
        self.assertTrue(s._ist_pfad_token(r"A:\x\y.bat"))

    def test_leer_faellt_auf_fallback(self):
        s.CONFIG["ollama_restart_cmd"] = ""
        cmd, shell, note = s._ollama_start_command()
        self.assertFalse(shell)
        if self._bundled_ok():
            self.assertTrue(cmd[0].endswith("Ollama_Start.bat"))
            self.assertEqual(note, "")                             # leere config, Bat da -> kein Hinweis noetig
        else:
            self.assertEqual(cmd, ["ollama", "serve"])             # posix (m4): kein .bat-Start

    def test_altpfad_wird_nicht_gefeuert(self):
        s.CONFIG["ollama_restart_cmd"] = r"A:\Claude\Macro Research\Ollama Start.bat"
        cmd, shell, note = s._ollama_start_command()
        self.assertIn("fehlende Datei", note)
        self.assertNotIn("Macro Research", str(cmd))               # der Altpfad wird NICHT gefeuert

    def test_wrapper_mit_totem_pfad_wird_nicht_gefeuert(self):
        # Fable-M1: der tote Pfad steckt HINTER `start` -> darf trotzdem nicht gefeuert werden
        s.CONFIG["ollama_restart_cmd"] = r'start "" "A:\Alt\Ollama Start.bat"'
        cmd, shell, note = s._ollama_start_command()
        self.assertIn("fehlende Datei", note)
        self.assertNotIn("Alt", str(cmd))

    def test_blosses_kommando_unveraendert(self):
        s.CONFIG["ollama_restart_cmd"] = "ollama serve"
        cmd, shell, note = s._ollama_start_command()
        self.assertEqual(cmd, "ollama serve"); self.assertTrue(shell); self.assertEqual(note, "")

    def test_existierende_datei_wird_genutzt(self):
        sysdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bat = os.path.join(sysdir, "Ollama_Start.bat")
        s.CONFIG["ollama_restart_cmd"] = f'"{bat}"'
        cmd, shell, note = s._ollama_start_command()
        self.assertEqual(cmd, f'"{bat}"'); self.assertTrue(shell); self.assertEqual(note, "")

    def test_restart_respektiert_manuell_gestoppt_latch(self):
        # der sicherheitsrelevante Latch: nach manuellem Stopp startet der Guard NICHT auto (kein Spawn)
        gerufen = []
        alt_spawn = s._ollama_spawn
        s._ollama_spawn = lambda *a, **k: gerufen.append(a)
        try:
            s.OLLAMA["manuell_gestoppt"] = True
            self.assertFalse(s._ollama_restart())
            self.assertEqual(gerufen, [])                          # kein Spawn
        finally:
            s._ollama_spawn = alt_spawn
            s.OLLAMA["manuell_gestoppt"] = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
