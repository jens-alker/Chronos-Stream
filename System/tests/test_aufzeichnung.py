"""
test_aufzeichnung.py — lossless recording, and the conservation law that keeps it honest.

Two invariants are pinned here. First, rejected documents are stored too: the denominator of
any later evaluation is "everything we saw", not "everything we kept" — a store that silently
drops what it discarded can never be audited for selection bias. Second, `pruefe_lauf` is a
conservation law between what a run reported and what it actually wrote; a counter that only
checks `n > 0` would report a run that lost half its documents as healthy.

Run offline, no network, standard library only:
  python3 System/tests/test_aufzeichnung.py
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from aufzeichnung import (                                                   # noqa: E402
    AufzeichnungFehler, pruefe_lauf, schema_anlegen, schreibe_dokument_roh,
)


def _dok(titel="Grid expansion", relevance=0.9, published="2026-01-03"):
    return {"source_type": "arxiv", "title": titel, "text": "…", "url": "https://example.org/1",
            "relevance": relevance, "trust": 0.8, "published_at": published}


class TestConservationLaw(unittest.TestCase):
    """`pruefe_lauf` compares what a run CLAIMED against what it WROTE — from two sources."""

    def test_matching_counts_pass(self):
        zeile = {"run_id": "r1", "n_gefunden": 10, "n_neu": 4}
        self.assertTrue(pruefe_lauf(zeile, n_relevanz_entscheid=10, n_dokument_roh=4))

    def test_a_lost_document_is_caught_not_absorbed(self):
        # The failure this exists for: the run says it found ten, nine decisions were written.
        # Nothing else in the system would notice.
        zeile = {"run_id": "r1", "n_gefunden": 10, "n_neu": 4}
        with self.assertRaises(AufzeichnungFehler):
            pruefe_lauf(zeile, n_relevanz_entscheid=9, n_dokument_roh=4)

    def test_a_mismatch_in_new_documents_is_caught(self):
        zeile = {"run_id": "r1", "n_gefunden": 10, "n_neu": 4}
        with self.assertRaises(AufzeichnungFehler):
            pruefe_lauf(zeile, n_relevanz_entscheid=10, n_dokument_roh=3)

    def test_the_error_names_the_run_and_both_sides(self):
        # A conservation breach that does not say which run and by how much cannot be chased.
        zeile = {"run_id": "lauf-42", "n_gefunden": 10, "n_neu": 4}
        with self.assertRaises(AufzeichnungFehler) as ctx:
            pruefe_lauf(zeile, n_relevanz_entscheid=9, n_dokument_roh=4)
        text = str(ctx.exception)
        self.assertIn("lauf-42", text)
        self.assertIn("10", text)
        self.assertIn("9", text)

    def test_both_breaches_are_reported_together(self):
        # Reporting only the first would send the reader back for a second run to find the rest.
        zeile = {"run_id": "r1", "n_gefunden": 10, "n_neu": 4}
        with self.assertRaises(AufzeichnungFehler) as ctx:
            pruefe_lauf(zeile, n_relevanz_entscheid=9, n_dokument_roh=3)
        self.assertIn("n_gefunden", str(ctx.exception))
        self.assertIn("n_neu", str(ctx.exception))

    def test_a_missing_counter_is_a_breach_not_a_pass(self):
        # An absent count must never satisfy the law by comparing None to None-ish input.
        with self.assertRaises(AufzeichnungFehler):
            pruefe_lauf({"run_id": "r1"}, n_relevanz_entscheid=5, n_dokument_roh=2)

    def test_a_run_that_found_nothing_is_consistent(self):
        # Zero is a result. The law must not fire on an honest empty run.
        self.assertTrue(pruefe_lauf({"run_id": "r0", "n_gefunden": 0, "n_neu": 0},
                                    n_relevanz_entscheid=0, n_dokument_roh=0))


class TestLosslessRecording(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        schema_anlegen(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_creates_the_recording_tables(self):
        namen = {n for (n,) in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("ingest_log", "fund", "dokument_roh", "relevanz_entscheid",
                  "attribut", "extraktor_version"):
            self.assertIn(t, namen)

    def test_the_raw_document_table_carries_its_temporal_roles(self):
        spalten = {r[1] for r in self.conn.execute("PRAGMA table_info(dokument_roh)")}
        for rolle in ("t_event", "t_disclosed", "t_ingest"):
            self.assertIn(rolle, spalten)

    def test_a_rejected_document_is_stored_too(self):
        # THE invariant of this module. Keeping only accepted documents destroys the
        # denominator, and with it any later statement about what was filtered out.
        schreibe_dokument_roh(self.conn, "d1", _dok(relevance=0.01), t_ingest="2026-01-05")
        zeilen = list(self.conn.execute(
            "SELECT doc_id, angenommen FROM dokument_roh"))
        self.assertEqual(zeilen, [("d1", 0)])

    def test_acceptance_follows_the_threshold(self):
        schreibe_dokument_roh(self.conn, "ja", _dok(relevance=0.9), t_ingest="2026-01-05")
        schreibe_dokument_roh(self.conn, "nein", _dok(relevance=0.1), t_ingest="2026-01-05")
        urteil = dict(self.conn.execute("SELECT doc_id, angenommen FROM dokument_roh"))
        self.assertEqual(urteil, {"ja": 1, "nein": 0})

    def test_the_threshold_boundary_counts_as_accepted(self):
        # Exactly at the threshold is a case someone will hit; leaving it implicit invites a
        # silent off-by-one when the threshold moves.
        schreibe_dokument_roh(self.conn, "rand", _dok(relevance=0.5), t_ingest="2026-01-05")
        self.assertEqual(
            self.conn.execute("SELECT angenommen FROM dokument_roh").fetchone()[0], 1)

    def test_disclosure_and_ingestion_time_are_kept_apart(self):
        # Collapsing them is precisely the look-ahead this project exists to prevent: the
        # publication date is not the date we could have known it.
        schreibe_dokument_roh(self.conn, "d1", _dok(published="2026-01-03"),
                              t_ingest="2026-01-05")
        t_disclosed, t_ingest = self.conn.execute(
            "SELECT t_disclosed, t_ingest FROM dokument_roh").fetchone()
        self.assertEqual(t_disclosed, "2026-01-03")
        self.assertEqual(t_ingest, "2026-01-05")
        self.assertNotEqual(t_disclosed, t_ingest)

    def test_the_full_payload_is_preserved(self):
        # "Lossless" means the original is reconstructible, not that a summary was kept.
        import json
        dok = _dok(titel="Übertragungsnetz — 40 %")
        schreibe_dokument_roh(self.conn, "d1", dok, t_ingest="2026-01-05")
        gespeichert = json.loads(
            self.conn.execute("SELECT payload_voll FROM dokument_roh").fetchone()[0])
        self.assertEqual(gespeichert, dok)

    def test_rewriting_the_same_document_stays_idempotent(self):
        for _ in range(3):
            schreibe_dokument_roh(self.conn, "d1", _dok(), t_ingest="2026-01-05", run_id="r1")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM dokument_roh").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fund").fetchone()[0], 1)

    def test_the_finding_link_records_which_run_saw_the_document(self):
        # The M:N link is the denominator per run — without it, `pruefe_lauf` has nothing to
        # count against.
        schreibe_dokument_roh(self.conn, "d1", _dok(), t_ingest="2026-01-05", run_id="r1")
        schreibe_dokument_roh(self.conn, "d1", _dok(), t_ingest="2026-01-06", run_id="r2")
        self.assertEqual(
            sorted(r for (r,) in self.conn.execute("SELECT run_id FROM fund")), ["r1", "r2"])

    def test_content_hash_tracks_the_content_not_the_identifier(self):
        schreibe_dokument_roh(self.conn, "d1", _dok(titel="A"), t_ingest="2026-01-05")
        schreibe_dokument_roh(self.conn, "d2", _dok(titel="B"), t_ingest="2026-01-05")
        hashes = [h for (h,) in self.conn.execute("SELECT content_hash FROM dokument_roh")]
        self.assertEqual(len(set(hashes)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
