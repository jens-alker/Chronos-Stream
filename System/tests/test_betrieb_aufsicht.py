#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_betrieb_aufsicht.py — Injektions-Tests der prozess-unabhängigen Laufzeit-Aufsicht (Feinkonzept §8).

Deckt die vorregistrierten Falsifikations-Injektionen ab: Prozess-Tod (+ Dead-Man's-Switch), Stagnation
(inkl. Fehlalarm-Freiheit bei lebender Quelle), Quota-Erschöpfung (roh aus HTTP-Codes), Alert-Lifecycle
(gruen→rot=Push, Dedup, geheilt→rot=Re-Push), ops-DB-Schema + fail-closed. Offline, Zeit injiziert.

Ausführen:  python3 System/tests/test_betrieb_aufsicht.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_SYSTEM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYSTEM not in sys.path:
    sys.path.insert(0, _SYSTEM)

import betrieb_aufsicht as B                                          # noqa: E402


class TestLiveness(unittest.TestCase):
    def test_frischer_heartbeat_gruen(self):
        r = B.liveness_pruefung({"collector": "2026-08-01T12:00:00"}, "2026-08-01T12:00:20", {"collector": 90})
        self.assertEqual(r["collector"]["status"], "gruen")

    def test_veralteter_heartbeat_rot(self):
        # 5 min alt > 90s Grenze -> Prozess tot/hängt
        r = B.liveness_pruefung({"collector": "2026-08-01T12:00:00"}, "2026-08-01T12:05:00", {"collector": 90})
        self.assertEqual(r["collector"]["status"], "rot")
        self.assertEqual(r["collector"]["drift_art"], "technisch")

    def test_fehlender_prozess_ist_rot(self):
        # ein erwarteter Loop, der GAR NICHT heartbeatet, ist tot (Stille != grün)
        r = B.liveness_pruefung({}, "2026-08-01T12:00:00", {"grind": 3600})
        self.assertEqual(r["grind"]["status"], "rot")
        self.assertIn("nie gemeldet", r["grind"]["beleg"])

    def test_dead_mans_switch(self):
        # der Wächter selbst tot -> gemeldet (Monitor-des-Monitors)
        r = B.dead_mans_switch({"aufsicht": "2026-08-01T10:00:00"}, "2026-08-01T12:00:00", {"aufsicht": 300})
        self.assertEqual(r["aufsicht"]["status"], "rot")

    def test_dead_mans_switch_nie_getickt_rot(self):
        # QS-Injektion: der Wächter hat NIE getickt (erwartet, aber fehlt) -> rot (Stille != grün)
        r = B.dead_mans_switch({}, "2026-08-01T12:00:00", 300, erwartete={"aufsicht"})
        self.assertEqual(r["aufsicht"]["status"], "rot")
        self.assertIn("nie gemeldet", r["aufsicht"]["beleg"])

    def test_int_modus_ohne_erwartete_wirft(self):
        # QS-BLOCKER: int-Grenze OHNE erwartete Menge -> ein nie-gemeldeter Prozess wäre unsichtbar -> Pflicht-Fehler
        with self.assertRaises(ValueError):
            B.liveness_pruefung({"a": "2026-08-01T12:00:00"}, "2026-08-01T12:00:10", 90)

    def test_int_modus_nie_gemeldeter_prozess_rot(self):
        # QS-BLOCKER: erwarteter Loop fehlt in den Heartbeats komplett -> rot (nicht stumm übersprungen)
        r = B.liveness_pruefung({"a": "2026-08-01T12:00:00"}, "2026-08-01T12:00:10", 90, erwartete={"a", "b"})
        self.assertEqual(r["a"]["status"], "gruen")
        self.assertEqual(r["b"]["status"], "rot")
        self.assertIn("nie gemeldet", r["b"]["beleg"])

    def test_gemischte_tz_kein_crash(self):
        # QS-M1: naiver Heartbeat vs. tz-aware jetzt -> kein TypeError-Crash, normal bewertet
        r = B.liveness_pruefung({"collector": "2026-08-01T12:00:00"},
                                "2026-08-01T12:00:20+00:00", {"collector": 90})
        self.assertEqual(r["collector"]["status"], "gruen")

    def test_zukunfts_zeitstempel_rot(self):
        # QS-M2: Heartbeat weit in der Zukunft (Clock-Skew) -> rot, Liveness nicht verifizierbar
        r = B.liveness_pruefung({"collector": "2026-08-01T12:10:00"}, "2026-08-01T12:00:00", {"collector": 90})
        self.assertEqual(r["collector"]["status"], "rot")
        self.assertIn("Clock-Skew", r["collector"]["beleg"])

    def test_unlesbarer_zeitstempel_rot(self):
        r = B.liveness_pruefung({"collector": "nicht-iso"}, "2026-08-01T12:00:00", {"collector": 90})
        self.assertEqual(r["collector"]["status"], "rot")

    def test_fehlende_schwelle_kein_crash_rot(self):
        # QS-MINOR-4: erwarteter Loop OHNE Schwelle im dict (Config-Drift) -> rot (Lücke sichtbar), kein KeyError
        r = B.liveness_pruefung({"b": "2026-08-01T12:00:00"}, "2026-08-01T12:00:10",
                                {"a": 90}, erwartete={"a", "b"})
        self.assertEqual(r["a"]["status"], "rot")           # a fehlt im Heartbeat -> rot
        self.assertEqual(r["b"]["status"], "rot")           # b hat Heartbeat, aber keine Schwelle -> rot (Config)
        self.assertIn("Config-Lücke", r["b"]["beleg"])


class TestStagnation(unittest.TestCase):
    def test_leerlauf_rot(self):
        r = B.stagnation_pruefung([5, 3, 0, 0, 0], K=3)
        self.assertEqual(r["status"], "rot")
        self.assertEqual(r["streak"], 3)
        self.assertEqual(r["drift_art"], "technisch")

    def test_gelb_ab_halb(self):
        r = B.stagnation_pruefung([5, 0, 0], K=4)          # 2 Nullen, K/2=2 -> gelb
        self.assertEqual(r["status"], "gelb")

    def test_lebende_quelle_kein_fehlalarm(self):
        # jüngster Lauf lieferte Neues -> Streak 0 -> gruen (langsam-aber-lebend)
        r = B.stagnation_pruefung([0, 0, 0, 0, 2], K=3)
        self.assertEqual(r["status"], "gruen")
        self.assertEqual(r["streak"], 0)

    def test_dubletten_achse(self):
        # n_neu>0, aber alles Dubletten (dup_of) über K Läufe -> effektiv stagnant
        r = B.stagnation_pruefung([1, 1, 1], K=3, dup_anteil_reihe=[1.0, 1.0, 1.0])
        self.assertEqual(r["status"], "rot")
        self.assertIn("Dubletten", r["beleg"])

    def test_leere_reihe_gelb_kaltstart(self):
        # QS-B4: keine Baseline -> gelb (beobachtend), NIE grün
        r = B.stagnation_pruefung([], K=3)
        self.assertEqual(r["status"], "gelb")
        self.assertEqual(r["streak"], 0)

    def test_none_lauf_maskiert_stagnation_nicht(self):
        # QS-M6: ein gescheiterter (None-)Lauf am jüngsten Slot zählt als „nichts geliefert"
        r = B.stagnation_pruefung([0, 0, None], K=3)
        self.assertEqual(r["status"], "rot")
        self.assertEqual(r["streak"], 3)

    def test_nan_lauf_maskiert_stagnation_nicht(self):
        # QS-MINOR-6: ein NaN/kaputter Wert am jüngsten Slot darf die Stagnation NICHT maskieren (fail-closed)
        r = B.stagnation_pruefung([0, 0, float("nan")], K=3)
        self.assertEqual(r["status"], "rot")
        self.assertEqual(r["streak"], 3)

    def test_negativer_wert_ist_keine_lieferung(self):
        r = B.stagnation_pruefung([0, 0, -5], K=3)
        self.assertEqual(r["streak"], 3)


class TestQuota(unittest.TestCase):
    def test_429_erschoepft_rot(self):
        r = B.quota_pruefung({"gemini": {"http_code": 429}})
        self.assertEqual(r["gemini"]["status"], "rot")
        self.assertEqual(r["gemini"]["empfehlung"], "ressource_umschalten")

    def test_402_gesperrt_rot(self):
        r = B.quota_pruefung({"cerebras": {"http_code": 402}})
        self.assertEqual(r["cerebras"]["status"], "rot")

    def test_body_fehlercode_rot(self):
        # 200 OK, aber Body trägt den Limit-Fehlercode (die Semantik-Kanal-2-Falle)
        r = B.quota_pruefung({"groq": {"http_code": 200, "body_fehlercode": 429}})
        self.assertEqual(r["groq"]["status"], "rot")

    def test_proaktiv_gelb_vor_anschlag(self):
        r = B.quota_pruefung({"mistral": {"http_code": 200, "rest_budget": 5, "budget_total": 100}})
        self.assertEqual(r["mistral"]["status"], "gelb")          # 5% < 10% -> Vorwarnung

    def test_ok_gruen(self):
        r = B.quota_pruefung({"ollama": {"http_code": 200, "rest_budget": 900, "budget_total": 1000}})
        self.assertEqual(r["ollama"]["status"], "gruen")

    def test_keine_belege_gelb_nicht_gruen(self):
        # QS-M3 fail-closed: die Signal-Beschaffung selbst schlug fehl -> gelb (unbestimmt), NIE grün
        r = B.quota_pruefung({"gemini": {"http_code": None, "body_fehlercode": None, "rest_budget": None}})
        self.assertEqual(r["gemini"]["status"], "gelb")

    def test_body_402_rot(self):
        r = B.quota_pruefung({"cerebras": {"http_code": 200, "body_fehlercode": 402}})
        self.assertEqual(r["cerebras"]["status"], "rot")

    def test_unbekannter_body_fehler_unter_200_nicht_gruen(self):
        # QS-MAJOR-1 (headline fail-closed): ein Fehlercode im 200-Body, der NICHT in {429,402,400,401,403}
        # liegt, darf NIEMALS grün lesen — genau die Semantik-Kanal-2-Falle
        r = B.quota_pruefung({"groq": {"http_code": 200, "body_fehlercode": 503}})
        self.assertNotEqual(r["groq"]["status"], "gruen")

    def test_403_gesperrt_rot_provider_deaktivieren(self):
        # QS-MAJOR-1: harter Auth/Forbidden-Code -> rot + provider_deaktivieren (nicht gelb/reparatur)
        r = B.quota_pruefung({"groq": {"http_code": 403}})
        self.assertEqual(r["groq"]["status"], "rot")
        self.assertEqual(r["groq"]["empfehlung"], "provider_deaktivieren")

    def test_401_gesperrt_rot(self):
        r = B.quota_pruefung({"openrouter": {"http_code": 401}})
        self.assertEqual(r["openrouter"]["status"], "rot")

    def test_string_http_code_kein_crash(self):
        # QS-MINOR-5: die Live-Naht liefert '429' als String -> robust zu int, kein TypeError, rot
        r = B.quota_pruefung({"groq": {"http_code": "429"}})
        self.assertEqual(r["groq"]["status"], "rot")

    def test_drift_art_keine_bei_quota(self):
        # QS-B1: Quota-Erschöpfung ist ein Ressource-Ereignis, kein Drift
        r = B.quota_pruefung({"g": {"http_code": 429}})
        self.assertEqual(r["g"]["drift_art"], "keine")


class TestAlertLifecycle(unittest.TestCase):
    def test_gruen_zu_rot_pusht(self):
        self.assertEqual(B.alert_uebergang(None, "rot"), ("aktiv", True))

    def test_rot_zu_rot_dedup_kein_push(self):
        self.assertEqual(B.alert_uebergang("aktiv", "rot"), ("aktiv", False))

    def test_rot_zu_gruen_heilt_kein_push(self):
        self.assertEqual(B.alert_uebergang("aktiv", "gruen"), ("geheilt", False))

    def test_geheilt_zu_rot_re_pusht(self):
        # wiederkehrender Ausfall -> erneuter Push (kein Übersehen)
        self.assertEqual(B.alert_uebergang("geheilt", "rot"), ("aktiv", True))

    def test_gelb_de_eskaliert_kein_push(self):
        # QS-B2: gelb aus aktiv -> beobachtend (runtergestuft, offen, kein Push) — NICHT im aktiv gefangen
        self.assertEqual(B.alert_uebergang("aktiv", "gelb"), ("beobachtend", False))
        self.assertEqual(B.alert_uebergang(None, "gelb"), (None, False))

    def test_beobachtend_zu_rot_pusht(self):
        # ein runtergestufter Alert, der wieder voll ausfällt -> re-eskaliert + Push
        self.assertEqual(B.alert_uebergang("beobachtend", "rot"), ("aktiv", True))

    def test_unbekannter_status_wirft(self):
        # QS-m4 fail-closed: kein still verschlucktes rot durch Casing/Tippfehler
        with self.assertRaises(ValueError):
            B.alert_uebergang("aktiv", "ROT")

    def test_quittiert_ueberlebt_gelb_blip(self):
        # QS-MAJOR-2: ein gelb-Blip ist KEINE Heilung -> hebt den Mensch-Ack NICHT auf
        self.assertEqual(B.alert_uebergang("quittiert", "gelb"), ("quittiert", False))
        # ein folgendes rot re-pusht daher NICHT (der Ack hält bis zur echten Heilung)
        self.assertEqual(B.alert_uebergang("quittiert", "rot"), ("quittiert", False))

    def test_quittiert_heilt_erst_bei_gruen(self):
        self.assertEqual(B.alert_uebergang("quittiert", "gruen"), ("geheilt", False))


class TestCanary(unittest.TestCase):
    _SCHEMA = {"pflicht_felder": ["kategorie", "staerke"], "ordinal_feld": "staerke",
               "ordinal_werte": {"keine", "schwach", "mittel", "stark"}}

    def test_auth_rot(self):
        r = B.canary_pruefung({"http_code": 401}, self._SCHEMA)
        self.assertEqual(r["status"], "rot")
        self.assertEqual(r["empfehlung"], "provider_deaktivieren")

    def test_deprecation_rot(self):
        r = B.canary_pruefung({"http_code": 404, "modell_existiert": False}, self._SCHEMA)
        self.assertEqual(r["status"], "rot")

    def test_last_uebergabe_gelb_nicht_rot(self):
        # F110-Scope: 429/402/5xx sind NICHT Canary -> gelb (an §3.5), Fähigkeit unbestimmt (nie grün)
        r = B.canary_pruefung({"http_code": 429}, self._SCHEMA)
        self.assertEqual(r["status"], "gelb")

    def test_format_kippe_rot(self):
        r = B.canary_pruefung({"http_code": 200, "geparst": {"kategorie": "energie"}}, self._SCHEMA)  # staerke fehlt
        self.assertEqual(r["status"], "rot")
        self.assertIn("staerke", r["beleg"])

    def test_dezimalkonfidenz_312_kippe_rot(self):
        # das Modell liefert plötzlich eine Dezimalkonfidenz statt der Ordinalklasse -> 3.12-Kippe
        r = B.canary_pruefung({"http_code": 200, "geparst": {"kategorie": "energie", "staerke": 0.87}}, self._SCHEMA)
        self.assertEqual(r["status"], "rot")
        self.assertIn("3.12", r["beleg"])

    def test_ordinal_ok_gruen(self):
        r = B.canary_pruefung({"http_code": 200, "geparst": {"kategorie": "energie", "staerke": "stark"}}, self._SCHEMA)
        self.assertEqual(r["status"], "gruen")

    def test_keine_antwort_gelb_fail_closed(self):
        self.assertEqual(B.canary_pruefung(None, self._SCHEMA)["status"], "gelb")


class TestDokumentKontrakte(unittest.TestCase):
    """Realdaten-nahe: gegen das ECHTE sammler_db-Schema (nicht gegen bequeme Fixtures)."""
    def setUp(self):
        from connectors.sammler_db import SCHEMA_DOCUMENTS, SCHEMA_FACTS
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA_DOCUMENTS + SCHEMA_FACTS)

    def _doc(self, **kw):
        f = {"source_type": "paper", "title": "T", "published_at": "2026-01-01",
             "relevance": 0.5, "trust": 0.5}
        f.update(kw)
        self.conn.execute("INSERT INTO documents(source_type,title,published_at,relevance,trust) "
                          "VALUES(?,?,?,?,?)", (f["source_type"], f["title"], f["published_at"],
                                                f["relevance"], f["trust"]))
        self.conn.commit()

    def _fact(self, **kw):
        f = {"doc_id": 1, "subjekt": "s", "beziehung": "b", "objekt": "o", "modus": "ist",
             "signalart": "technologie", "reife": "stark", "reife_score": 0.8}
        f.update(kw)
        self.conn.execute("INSERT INTO facts(doc_id,subjekt,beziehung,objekt,modus,signalart,reife,reife_score) "
                          "VALUES(?,?,?,?,?,?,?,?)", (f["doc_id"], f["subjekt"], f["beziehung"], f["objekt"],
                                                      f["modus"], f["signalart"], f["reife"], f["reife_score"]))
        self.conn.commit()

    def test_leer_gelb_kaltstart(self):
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertEqual(r["status"], "gelb")            # documents leer -> beobachtend, NIE grün

    def test_konform_gruen(self):
        self._doc(); self._fact()
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertEqual(r["status"], "gruen")
        self.assertEqual(r["verstoesse"], [])

    def test_legacy_source_type_rot(self):
        # ein Produzent schreibt die QUELL-Konvention (arxiv) statt der Reifegrad-Konvention -> Verstoß
        self._doc(source_type="arxiv")
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertEqual(r["status"], "rot")
        self.assertTrue(any(v["feld"] == "source_type" for v in r["verstoesse"]))

    def test_reife_stufenlabel_ist_kein_verstoss(self):
        # KORREKTUR (Realdaten-Befund 2026-08-06, Wächter am echten Produzenten): `reife` ist ein MATURITAETS-
        # STUFEN-Label (scraper._reife_label: Grundlagenforschung..Patent/Prototyp..Markt), KEINE ordinale
        # Staerke — der Kontrakt (contracts.py) constraint reife NICHT auf ORDINAL_STAERKE. Ein gueltiges
        # Stufen-Label darf NICHT geflaggt werden (die alte `reife ∈ ORDINAL_STAERKE`-Prüfung war ein
        # Schein-Test gegen Synthetik-Fakten -> flaggte am echten Produzenten 30153 valide Fakten falsch).
        # Die harte Invariante bleibt reife_score ∈ [0,1] (separat geprueft).
        self._doc(); self._fact(reife="Patent/Prototyp")
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertFalse(any(v["feld"] == "reife" for v in r["verstoesse"]))

    def test_relevance_ausserhalb_einheit_rot(self):
        self._doc(relevance=1.5)
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertTrue(any(v["feld"] == "relevance" for v in r["verstoesse"]))

    def test_leeres_published_at_rot(self):
        # published_at='' passiert NOT NULL, ist aber leer -> Offenlegungszeit fehlt (realer Produzenten-Bug)
        self._doc(published_at="")
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertTrue(any(v["feld"] == "published_at" for v in r["verstoesse"]))

    def test_beobachter_blockiert_nicht(self):
        # der Beobachter SCHREIBT/LÖSCHT nichts an der scraper.db (liest nur)
        self._doc(source_type="arxiv")
        B.pruefe_dokument_kontrakte(self.conn)
        n = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.assertEqual(n, 1)                           # unverändert -> beobachtet, nicht durchgesetzt

    def test_vereinzelte_verstoesse_gelb_nicht_rot(self):
        # Jens 08.08.: 1 malformte Zeile in ~200 (0,5 %) ist DATENQUALITÄT (gelb), NICHT Betriebs-Kritisch (rot).
        for i in range(200):
            self._doc(title=f"t{i}", published_at=f"2026-01-{(i % 28) + 1:02d}")
        self._doc(title="leer", published_at="")         # ein vereinzelter Randfall
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertEqual(r["status"], "gelb")
        self.assertEqual(r["empfehlung"], "datenqualitaet_pruefen")
        self.assertTrue(any(v["feld"] == "published_at" for v in r["verstoesse"]))

    def test_systemische_verstoesse_rot(self):
        # bricht ein SYSTEMISCHER Anteil (>= 1 %), ist es rot (echter Betriebsfehler).
        for i in range(50):
            self._doc(title=f"bad{i}", published_at="")  # leeres published_at -> Verstoss; Titel eindeutig
        r = B.pruefe_dokument_kontrakte(self.conn)
        self.assertEqual(r["status"], "rot")
        self.assertEqual(r["empfehlung"], "mensch_tor")


class TestOpsDB(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, self.path)
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        B.schema_anlegen(self.conn)

    def test_schema_tabellen(self):
        tab = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertTrue({"system_gesundheit", "alert_zustand", "monitor_heartbeat"} <= tab)

    def test_schreibe_gesundheit_fail_closed(self):
        with self.assertRaises(ValueError):
            B.schreibe_gesundheit(self.conn, "x", "quatsch", "m", "rot", "2026-08-01")   # ungültige kategorie
        with self.assertRaises(ValueError):
            B.schreibe_gesundheit(self.conn, "x", "technik", "m", "lila", "2026-08-01")  # ungültiger status

    def test_projektion_persistiert_und_pusht(self):
        p1 = B.projiziere_alert(self.conn, "cot", "stagnation", "rot", "2026-08-01T12:00:00")
        self.assertEqual((p1["zustand"], p1["push"]), ("aktiv", True))
        p2 = B.projiziere_alert(self.conn, "cot", "stagnation", "rot", "2026-08-01T12:01:00")
        self.assertEqual((p2["zustand"], p2["push"]), ("aktiv", False))       # Dedup
        p3 = B.projiziere_alert(self.conn, "cot", "stagnation", "gruen", "2026-08-01T12:02:00")
        self.assertEqual(p3["zustand"], "geheilt")
        p4 = B.projiziere_alert(self.conn, "cot", "stagnation", "rot", "2026-08-01T12:03:00")
        self.assertTrue(p4["push"])                                           # Re-Push

    def test_monitor_heartbeat_round_trip(self):
        B.setze_monitor_heartbeat(self.conn, "aufsicht", "2026-08-01T12:00:00")
        B.setze_monitor_heartbeat(self.conn, "aufsicht", "2026-08-01T12:00:30")   # upsert
        hb = B.lies_monitor_heartbeats(self.conn)
        self.assertEqual(hb["aufsicht"], "2026-08-01T12:00:30")


class TestProjektionsLeser(unittest.TestCase):
    """Die Leser, die Modul 17 (GUI) ruft — F119: in ihrer Heimat betrieb_aufsicht, keine Query-Insel."""
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        B.schema_anlegen(self.conn)

    def test_lies_gesundheit_aktuell_nimmt_juengstes(self):
        # zwei Ereignisse derselben (komponente,metrik) -> nur das jüngste (MAX id) zählt
        B.schreibe_gesundheit(self.conn, "groq", "ressource", "quota", "gruen", "2026-08-01T12:00:00", beleg="ok")
        B.schreibe_gesundheit(self.conn, "groq", "ressource", "quota", "rot", "2026-08-01T12:05:00", beleg="HTTP 429")
        r = B.lies_gesundheit_aktuell(self.conn)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["status"], "rot")
        self.assertEqual(r[0]["beleg"], "HTTP 429")           # JSON-Beleg dekodiert

    def test_gesamt_ampel_worst_of(self):
        B.schreibe_gesundheit(self.conn, "a", "technik", "liveness", "gruen", "t")
        B.schreibe_gesundheit(self.conn, "b", "technik", "stagnation", "gelb", "t")
        self.assertEqual(B.gesamt_ampel(self.conn), "gelb")
        B.schreibe_gesundheit(self.conn, "c", "ressource", "quota", "rot", "t")
        self.assertEqual(B.gesamt_ampel(self.conn), "rot")    # rot schlägt gelb schlägt grün

    def test_gesamt_ampel_leer_gelb_nicht_gruen(self):
        # keine Zeile -> unbestimmt -> gelb (Stille != Grün), NIE stumm grün
        self.assertEqual(B.gesamt_ampel(self.conn), "gelb")

    def test_gesamt_ampel_alle_gruen(self):
        B.schreibe_gesundheit(self.conn, "a", "technik", "liveness", "gruen", "t")
        self.assertEqual(B.gesamt_ampel(self.conn), "gruen")

    def test_lies_alert_zustaende(self):
        B.projiziere_alert(self.conn, "groq", "quota", "rot", "2026-08-01T12:00:00")
        az = B.lies_alert_zustaende(self.conn)
        self.assertEqual(len(az), 1)
        self.assertEqual((az[0]["komponente"], az[0]["zustand"]), ("groq", "aktiv"))

    def test_steuer_audit(self):
        # F126: Steuer-Aktionen protokolliert (WER/WANN), jüngste zuerst
        B.schreibe_steuer_audit(self.conn, "jens", "quelle_aus", "epo", None, "2026-08-02T12:00:00")
        B.schreibe_steuer_audit(self.conn, "jens", "kadenz", "preise", "täglich", "2026-08-02T12:01:00")
        a = B.lies_steuer_audit(self.conn)
        self.assertEqual(len(a), 2)
        self.assertEqual(a[0]["aktion"], "kadenz")               # jüngste zuerst
        self.assertEqual(a[1]["ziel"], "epo")

    def test_flapping_cooldown_unterdrueckt_re_push(self):
        # QS-M5: geheilt -> rot innerhalb des Cooldowns -> KEIN erneuter Push (oszillierende Quelle)
        B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:00:00", cooldown_sek=600)
        B.projiziere_alert(self.conn, "cot", "stag", "gruen", "2026-08-01T12:01:00", cooldown_sek=600)  # geheilt
        p = B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:02:00", cooldown_sek=600)
        self.assertEqual(p["zustand"], "geheilt")                  # QS-MINOR-3: bleibt geheilt (kein Push-Verlust)
        self.assertFalse(p["push"])                                # < 600s seit Heilung -> kein Spam
        # außerhalb des Cooldowns pusht es wieder
        B.projiziere_alert(self.conn, "cot", "stag", "gruen", "2026-08-01T12:03:00", cooldown_sek=600)
        p2 = B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:20:00", cooldown_sek=600)
        self.assertTrue(p2["push"])

    def test_flapping_dann_dauer_rot_pusht_nach_cooldown(self):
        # QS-MINOR-3: ein Flap innerhalb des Cooldowns bleibt `geheilt` (nicht aktiv) -> bleibt die Quelle
        # rot, pusht der nächste Tick nach Cooldown-Ablauf DOCH (kein permanenter Push-Verlust)
        B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:00:00", cooldown_sek=600)
        B.projiziere_alert(self.conn, "cot", "stag", "gruen", "2026-08-01T12:01:00", cooldown_sek=600)  # geheilt seit 12:01
        p = B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:02:00", cooldown_sek=600)
        self.assertFalse(p["push"])                                # Flap unterdrückt
        self.assertTrue(p["flapping_unterdrueckt"])
        self.assertEqual(p["zustand"], "geheilt")                  # NICHT auf aktiv vorgerückt
        # Quelle bleibt rot; ein Tick > 600s nach der Heilung (12:01) pusht doch
        p2 = B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:15:00", cooldown_sek=600)
        self.assertTrue(p2["push"])
        self.assertEqual(p2["zustand"], "aktiv")

    def test_quittieren_stoppt_push(self):
        # QS-m2: Mensch-Ack -> quittiert; erneutes rot dedupt (kein Push), bis es heilt + neu ausfällt
        B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:00:00")
        B.quittiere_alert(self.conn, "cot", "stag", "2026-08-01T12:01:00")
        p = B.projiziere_alert(self.conn, "cot", "stag", "rot", "2026-08-01T12:02:00")
        self.assertFalse(p["push"])
        self.assertEqual(p["zustand"], "quittiert")


class TestDriveStandFrische(unittest.TestCase):
    """Konzept B §8 (Fable-B11): der Frische-Beobachter des scraper.db-Drive-Stands — reine Funktion,
    Zeit injiziert, „Stille ≠ Grün" (fehlend/unlesbar/Zukunft -> nie grün)."""

    def test_frischer_stand_gruen(self):
        r = B.drive_stand_frische_pruefung("2026-08-06 06:00:00", "2026-08-06 12:00:00")
        self.assertEqual(r["status"], "gruen")
        self.assertEqual(r["drift_art"], "keine")
        self.assertAlmostEqual(r["alter_tage"], 0.25)

    def test_aelter_als_gelb_schwelle(self):
        r = B.drive_stand_frische_pruefung("2026-08-03 12:00:00", "2026-08-06 12:00:00",
                                           gelb_tage=2.0, rot_tage=7.0)
        self.assertEqual(r["status"], "gelb")                            # 3 Tage > 2
        self.assertEqual(r["drift_art"], "technisch")

    def test_aelter_als_rot_schwelle(self):
        r = B.drive_stand_frische_pruefung("2026-07-20 12:00:00", "2026-08-06 12:00:00",
                                           gelb_tage=2.0, rot_tage=7.0)
        self.assertEqual(r["status"], "rot")                             # 17 Tage > 7 (Heim-Sync tot?)
        self.assertEqual(r["empfehlung"], "reparatur")

    def test_fehlender_stand_ist_rot(self):
        # Stille != Grün: Sync nie gelaufen / kein Manifest -> rot, nie stumm gesund
        r = B.drive_stand_frische_pruefung(None, "2026-08-06 12:00:00")
        self.assertEqual(r["status"], "rot")
        self.assertIsNone(r["alter_tage"])
        self.assertIn("nie gelaufen", r["beleg"])

    def test_unlesbarer_zeitstempel_ist_rot(self):
        r = B.drive_stand_frische_pruefung("kein-datum", "2026-08-06 12:00:00")
        self.assertEqual(r["status"], "rot")

    def test_zukunfts_zeitstempel_ist_rot(self):
        # Clock-Skew jenseits der Toleranz: Frische nicht verifizierbar -> fail-closed rot
        r = B.drive_stand_frische_pruefung("2026-08-06 13:00:00", "2026-08-06 12:00:00")
        self.assertEqual(r["status"], "rot")
        self.assertIn("Zukunfts", r["beleg"])

    def test_kleiner_skew_bleibt_gruen(self):
        # 60s Zukunft < _SKEW_TOLERANZ_SEK (120s) = normaler Uhr-Jitter, kein Alarm
        r = B.drive_stand_frische_pruefung("2026-08-06 12:01:00", "2026-08-06 12:00:00")
        self.assertEqual(r["status"], "gruen")


class TestCacheFrische(unittest.TestCase):
    """Datenpflege W5: der Fundamentals-Cache-Frische-Beobachter fuer den cache_only-Rechenpfad —
    reine Funktion, Zeit injiziert, „Stille ≠ Grün" (fehlend/unlesbar/Zukunft -> nie grün)."""

    def test_frischer_stand_gruen(self):
        r = B.cache_frische_pruefung("2026-07-01", "2026-08-06")             # ~36 Tage: normales Quartal
        self.assertEqual(r["status"], "gruen")
        self.assertEqual(r["drift_art"], "keine")
        self.assertAlmostEqual(r["alter_tage"], 36.0)

    def test_ueber_quartal_gelb(self):
        r = B.cache_frische_pruefung("2026-03-01", "2026-08-06")             # ~158 Tage > 120
        self.assertEqual(r["status"], "gelb")
        self.assertEqual(r["drift_art"], "technisch")

    def test_zwei_zyklen_rot(self):
        r = B.cache_frische_pruefung("2025-10-01", "2026-08-06")             # ~309 Tage > 240
        self.assertEqual(r["status"], "rot")
        self.assertEqual(r["empfehlung"], "reparatur")                       # Auffrischer tot?

    def test_fehlender_stand_ist_rot(self):
        # Stille != Grün: cache_only-Lauf ohne ein einziges nutzbares Symbol -> leer ist NICHT frisch
        r = B.cache_frische_pruefung(None, "2026-08-06")
        self.assertEqual(r["status"], "rot")
        self.assertIsNone(r["alter_tage"])

    def test_unlesbarer_stand_ist_rot(self):
        self.assertEqual(B.cache_frische_pruefung("0000-00-00", "2026-08-06")["status"], "rot")

    def test_zukunfts_stand_ist_rot(self):
        r = B.cache_frische_pruefung("2026-08-10", "2026-08-06")
        self.assertEqual(r["status"], "rot")
        self.assertIn("Zukunfts", r["beleg"])

    def test_schwellen_konfigurierbar(self):
        r = B.cache_frische_pruefung("2026-08-01", "2026-08-06", gelb_tage=2.0, rot_tage=4.0)
        self.assertEqual(r["status"], "rot")                                 # 5 Tage > 4


class TestControlPlane(unittest.TestCase):
    """F128/F131/F133: Prozess-Status (Fortschritt/ETA/Liveness) + Control-Plane + Lauf-Diagnose gegen die
    echte ops-DB (realdaten-nah: das echte Schema, nicht hand-injizierte Zwischenwerte)."""
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        B.schema_anlegen(self.c)

    def test_melde_status_und_eta(self):
        B.melde_status(self.c, "datenpflege", "2026-08-06T10:00:00", phase="fund", aktuell=0, gesamt=100)
        B.melde_status(self.c, "datenpflege", "2026-08-06T10:00:20", phase="fund", aktuell=40, gesamt=100)
        rows = {r["prozess"]: r for r in B.lies_prozess_status(
            self.c, "2026-08-06T10:00:25", tot_ab_sek=300, erwartete={"datenpflege"})}
        d = rows["datenpflege"]
        self.assertEqual(d["angezeigt"], "läuft")
        self.assertEqual(d["ampel"], "gruen")
        self.assertAlmostEqual(d["anteil"], 0.4)
        self.assertAlmostEqual(d["eta_sek"], 30.0)                           # 40 Einheiten in 20s -> 60 in 30s

    def test_erwarteter_ohne_zeile_ist_tot_nicht_unsichtbar(self):
        # Fable-B2: ein erwarteter Prozess ohne melde_status ist ROT (nie gemeldet), nicht unsichtbar.
        rows = {r["prozess"]: r for r in B.lies_prozess_status(
            self.c, "2026-08-06T10:00:00", tot_ab_sek=300, erwartete={"supervisor"})}
        self.assertEqual(rows["supervisor"]["angezeigt"], "tot")
        self.assertEqual(rows["supervisor"]["ampel"], "rot")

    def test_veralteter_herzschlag_ist_tot(self):
        B.melde_status(self.c, "scraper", "2026-08-06T09:00:00", aktuell=1, gesamt=2)
        rows = {r["prozess"]: r for r in B.lies_prozess_status(
            self.c, "2026-08-06T10:00:00", tot_ab_sek=300, erwartete={"scraper"})}
        self.assertEqual(rows["scraper"]["angezeigt"], "tot")               # 1h alt > 300s -> tot trotz "läuft"

    def test_terminaler_zustand_nicht_falsch_rot(self):
        # Fable-m2: ein legitim fertiger Prozess wird NICHT tot-markiert (sein Herzschlag endet legitim).
        B.melde_status(self.c, "bewertung", "2026-08-06T09:00:00", aktuell=100, gesamt=100, zustand="fertig")
        rows = {r["prozess"]: r for r in B.lies_prozess_status(
            self.c, "2026-08-06T10:00:00", tot_ab_sek=300, erwartete={"bewertung"})}
        self.assertEqual(rows["bewertung"]["angezeigt"], "fertig")
        self.assertEqual(rows["bewertung"]["ampel"], "gruen")

    def test_eta_none_bei_null_zeit(self):
        # Fable-M5: aktualisiert == gestartet -> keine Division durch 0, ETA None.
        B.melde_status(self.c, "x", "2026-08-06T10:00:00", aktuell=5, gesamt=100)
        rows = {r["prozess"]: r for r in B.lies_prozess_status(
            self.c, "2026-08-06T10:00:00", tot_ab_sek=300, erwartete={"x"})}
        self.assertIsNone(rows["x"]["eta_sek"])

    def test_steuerung_round_trip_und_audit(self):
        self.assertEqual(B.lies_steuerung(self.c, "scraper"), "run")        # Default: laufen
        B.setze_steuerung(self.c, "scraper", "pause", "jens", "2026-08-06T10:00:00")
        self.assertEqual(B.lies_steuerung(self.c, "scraper"), "pause")
        self.assertEqual(B.lies_alle_steuerung(self.c)["scraper"], "pause")
        audit = B.lies_steuer_audit(self.c, 5)
        self.assertEqual(audit[0]["aktion"], "steuern")
        self.assertEqual(audit[0]["wert"], "pause")

    def test_steuerung_fail_closed(self):
        with self.assertRaises(ValueError):
            B.setze_steuerung(self.c, "x", "kill_-9", "jens", "2026-08-06T10:00:00")

    def test_melde_status_fail_closed(self):
        with self.assertRaises(ValueError):
            B.melde_status(self.c, "x", "2026-08-06T10:00:00", zustand="zombie")

    def test_lauf_diagnose_append_und_juengste(self):
        B.schreibe_lauf_diagnose(self.c, "2026-08-06T10:00:00", {"n_bewertung": 5})
        B.schreibe_lauf_diagnose(self.c, "2026-08-06T11:00:00", {"n_bewertung": 12, "n_konjunktion": 2})
        t, kennz = B.lies_lauf_diagnose(self.c)
        self.assertEqual(t, "2026-08-06T11:00:00")                           # jüngste zählt
        self.assertEqual(kennz["n_bewertung"], 12)


class TestDatenluecken(unittest.TestCase):
    """Datenlücken-Überwachung (Jens 07.08.): der cache-only-Rechenpfad braucht ein vollständig gefülltes
    Universum — fehlende Symbole = verzerrte/fehlende Folds. Evaluator + ops-DB-Verankerung."""
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        B.schema_anlegen(self.c)

    def tearDown(self):
        self.c.close()

    def test_gruen_wenn_luecke_klein(self):
        r = B.datenluecken_pruefung({"n": 100, "n_bereit": 98, "n_leer": 1, "n_delistet_kurz": 0, "n_fehlend": 1})
        self.assertEqual(r["status"], "gruen")                 # 1% < gelb 5%

    def test_gelb_und_rot_nach_anteil(self):
        self.assertEqual(B.datenluecken_pruefung({"n": 100, "n_fehlend": 8})["status"], "gelb")   # 8%
        self.assertEqual(B.datenluecken_pruefung({"n": 100, "n_fehlend": 30})["status"], "rot")   # 30%

    def test_no_data_und_delistet_sind_keine_luecke(self):
        # n_leer/n_delistet_kurz sind AUFGELÖST (nicht nachladbar) -> zählen NICHT als Lücke.
        r = B.datenluecken_pruefung({"n": 100, "n_bereit": 40, "n_leer": 55, "n_delistet_kurz": 5, "n_fehlend": 0})
        self.assertEqual(r["status"], "gruen")
        self.assertEqual(r["anteil"], 0.0)

    def test_fail_closed_leer(self):
        self.assertEqual(B.datenluecken_pruefung({"n": 0})["status"], "rot")        # leer ist NICHT grün
        self.assertEqual(B.datenluecken_pruefung(None)["status"], "rot")

    def test_fail_closed_unlesbar_kein_fail_open(self):
        # MINOR-1: fehlender n_fehlend-Key oder implausibler Zähler = unlesbar -> rot (NICHT still 0/grün).
        self.assertEqual(B.datenluecken_pruefung({"n": 500})["status"], "rot")             # kein n_fehlend
        self.assertEqual(B.datenluecken_pruefung({"n": 500, "n_fehlend": -3})["status"], "rot")
        self.assertEqual(B.datenluecken_pruefung({"n": 500, "n_fehlend": 999})["status"], "rot")  # >n

    def test_writer_crasht_nicht_auf_unlesbar_report(self):
        # MAJOR-2 (fail-closed-Inversion): der Writer darf auf genau dem „unlesbar->rot"-Report NICHT werfen —
        # sonst erreicht das rot-Urteil die DB nie. Die Zeile MUSS mit status=rot + bereitschaft=None landen.
        u = B.schreibe_datenluecken(self.c, {"n": "kaputt", "n_fehlend": "x"}, "2026-08-07T10:00:00")
        self.assertEqual(u["status"], "rot")
        row = {r["metrik"]: r for r in B.lies_gesundheit_aktuell(self.c)}["datenluecken"]
        self.assertEqual(row["status"], "rot")                 # persistiert, nicht verschluckt
        self.assertIsNone(row["beleg"]["bereitschaft"])        # kein erfundener Breakdown

    def test_schreibe_verankert_in_ops_db_mit_breakdown(self):
        b = {"n": 200, "n_bereit": 150, "n_leer": 10, "n_delistet_kurz": 5, "n_fehlend": 35, "prozent": 82.5}
        u = B.schreibe_datenluecken(self.c, b, "2026-08-07T10:00:00")
        self.assertEqual(u["status"], "gelb")                  # 35/200 = 17.5% liegt zwischen gelb(5%) und rot(20%)
        aktuell = {r["metrik"]: r for r in B.lies_gesundheit_aktuell(self.c)}
        self.assertIn("datenluecken", aktuell)
        beleg = aktuell["datenluecken"]["beleg"]
        self.assertEqual(beleg["bereitschaft"]["n_fehlend"], 35)   # Breakdown für die Modul-17-Kachel da


if __name__ == "__main__":
    unittest.main(verbosity=2)
