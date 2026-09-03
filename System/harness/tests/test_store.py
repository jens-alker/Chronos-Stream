"""
test_store.py — the store is append-only, and that is a temporal guarantee, not a style choice.
If a restatement could overwrite its predecessor, the earlier state would stop being
reconstructible and every as-of query would silently answer with today's knowledge.

Both implementations are held to the same contract by the same tests, so they cannot drift
apart: two stores with different semantics behind one interface is a bug that only shows up
when the deployment target changes.

Run offline, no network, standard library only:
  python3 System/harness/tests/test_store.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from store import MemStore, SqliteStore                                      # noqa: E402


class StoreContract:
    """Shared expectations. Subclasses supply `make_store`; nothing here knows which one it got."""

    def make_store(self):
        raise NotImplementedError

    def test_unknown_table_reads_as_empty_not_as_error(self):
        self.assertEqual(self.make_store().get("nie_geschrieben"), [])

    def test_appended_rows_come_back_in_insertion_order(self):
        # Order is what makes "the state as of t" reconstructible from an append-only log.
        store = self.make_store()
        store.append("obs", [{"n": 1}, {"n": 2}])
        store.append("obs", [{"n": 3}])
        self.assertEqual([r["n"] for r in store.get("obs")], [1, 2, 3])

    def test_a_restatement_is_a_new_row_not_an_overwrite(self):
        # The invariant this class exists for: writing the same key twice keeps both, so the
        # earlier value stays visible to a query about an earlier moment.
        store = self.make_store()
        store.append("obs", [{"id": "a", "wert": 10, "t_ingest": "2026-01-01"}])
        store.append("obs", [{"id": "a", "wert": 11, "t_ingest": "2026-02-01"}])
        rows = store.get("obs")
        self.assertEqual(len(rows), 2, "the restatement replaced its predecessor")
        self.assertEqual([r["wert"] for r in rows], [10, 11])

    def test_store_exposes_no_way_to_update_or_delete(self):
        # Append-only has to be structural. A store that merely *documents* the rule relies on
        # every caller remembering it.
        store = self.make_store()
        for verboten in ("update", "delete", "remove", "set", "overwrite"):
            self.assertFalse(hasattr(store, verboten),
                             f"store exposes {verboten}() — append-only is then only a convention")

    def test_tables_reports_everything_written_and_nothing_else(self):
        store = self.make_store()
        store.append("a", [{"x": 1}])
        store.append("b", [{"y": 2}])
        self.assertEqual(sorted(store.tables()), ["a", "b"])
        self.assertEqual(store.tables()["a"], [{"x": 1}])

    def test_reads_do_not_alias_stored_rows(self):
        # A caller mutating what it read must not reach back into the store; that would be an
        # in-place edit through the back door.
        store = self.make_store()
        store.append("obs", [{"n": 1}])
        gelesen = store.get("obs")
        gelesen.append({"n": 99})
        self.assertEqual(len(store.get("obs")), 1)

    def test_appending_nothing_is_harmless(self):
        store = self.make_store()
        store.append("obs", [])
        self.assertEqual(store.get("obs"), [])

    def test_non_ascii_content_survives_the_round_trip(self):
        # Source texts are not ASCII. A store that mangles them corrupts the record it exists
        # to preserve — and does it silently.
        store = self.make_store()
        store.append("obs", [{"titel": "Übertragungsnetz — 40 % Ausbau", "q": "arXiv"}])
        self.assertEqual(store.get("obs")[0]["titel"], "Übertragungsnetz — 40 % Ausbau")


class TestMemStore(StoreContract, unittest.TestCase):
    def make_store(self):
        return MemStore()


class TestSqliteStoreInMemory(StoreContract, unittest.TestCase):
    def make_store(self):
        return SqliteStore(":memory:")


class TestSqliteStoreOnDisk(StoreContract, unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def make_store(self):
        return SqliteStore(os.path.join(self._dir.name, "chronos_test.db"))

    def test_rows_survive_reopening_the_file(self):
        # Durability is the reason the SQLite variant exists at all.
        pfad = os.path.join(self._dir.name, "persist.db")
        store = SqliteStore(pfad)
        store.append("obs", [{"n": 1}, {"n": 2}])
        store.close()
        self.assertEqual([r["n"] for r in SqliteStore(pfad).get("obs")], [1, 2])


class TestSqliteStoreSpecifics(unittest.TestCase):

    def test_internal_sqlite_tables_are_not_reported_as_data(self):
        # AUTOINCREMENT makes SQLite create `sqlite_sequence`, which has no `data` column.
        # Listing it would make tables() raise on a perfectly healthy store.
        store = SqliteStore(":memory:")
        store.append("obs", [{"n": 1}])
        self.assertEqual(sorted(store.tables()), ["obs"])

    def test_table_name_is_restricted_to_plain_identifiers(self):
        # The table name reaches SQL as an identifier, so it is validated rather than quoted-
        # and-hoped. A verification tool will one day point this store at foreign input.
        store = SqliteStore(":memory:")
        for boese in ('obs"; DROP TABLE obs; --', "obs-1", "obs table", "obs;", ""):
            with self.subTest(name=boese):
                with self.assertRaises(ValueError):
                    store.append(boese, [{"n": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
