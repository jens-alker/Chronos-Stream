"""
store.py — Tabellenspeicher für den Harness (append-only).

Zwei austauschbare Implementierungen, gleiche Schnittstelle (append / get / tables):
  - MemStore    : In-Memory (schnellste Testiterationen)
  - SqliteStore : SQLite-Datei (Test-DB in der Cloud; beim lokalen Deploy zeigt
                  dieselbe Klasse auf die echte DB — nur der Pfad ändert sich)

Beide sind **append-only** (nur `append`, kein Update/Delete) — konsistent mit der
Schicht-D-Konvention (No-Overwrite; jede Änderung ist eine neue Zeile).
Nur Standardbibliothek.
"""
import json
import sqlite3


class MemStore:
    """Append-only In-Memory-Speicher."""
    def __init__(self):
        self._tables = {}

    def append(self, table, rows):
        self._tables.setdefault(table, []).extend(rows)

    def get(self, table):
        return list(self._tables.get(table, []))

    def tables(self):
        return {k: list(v) for k, v in self._tables.items()}


class SqliteStore:
    """Append-only SQLite-Speicher (Test-DB / später echte DB, nur Pfad ändert sich).

    Generisch: jede Tabelle ist `(seq INTEGER PK, data TEXT)` mit der Zeile als JSON.
    Die *fachliche* Schema-/Kontrakt-Prüfung macht der Kontrakt-Validator (contracts.py)
    auf Dict-Ebene VOR dem append — hier geht es nur um Persistenz.
    """
    def __init__(self, path=":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")

    def _ensure(self, table):
        # Tabellenname auf einfache Bezeichner beschränken (kein SQL-Injection-Risiko).
        if not table.replace("_", "").isalnum():
            raise ValueError(f"unzulässiger Tabellenname: {table!r}")
        self._conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{table}" '
            f'(seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)')

    def append(self, table, rows):
        self._ensure(table)
        self._conn.executemany(
            f'INSERT INTO "{table}"(data) VALUES(?)',
            [(json.dumps(r, ensure_ascii=False),) for r in rows])
        self._conn.commit()

    def get(self, table):
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if cur.fetchone() is None:
            return []
        return [json.loads(d) for (d,) in
                self._conn.execute(f'SELECT data FROM "{table}" ORDER BY seq')]

    def tables(self):
        # `sqlite_%` ausschliessen: SQLite legt bei AUTOINCREMENT automatisch die interne Tabelle
        # `sqlite_sequence` an (ohne `data`-Spalte) — sonst wirft `get()` darauf "no such column: data".
        names = [n for (n,) in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        return {n: self.get(n) for n in names}

    def close(self):
        self._conn.close()
