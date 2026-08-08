"""
test_db_drive.py — Offline-Tests des GETEILTEN generischen SQLite→Drive-Sync-Kerns (Konzept B §8).

Fake-gdrive (in-memory, signatur-kompatibel zu `gdrive.py`) injiziert — geprüft: byte-Round-Trip
(sync_db → restore_db identisch), Read-back-Riegel (korrupter Upload → neue Datei gelöscht, Alt-Stände
bleiben, wirft laut), Retention-Rotation, Versionsnummer aus dem Maximum, pre_check-/monotonie-/schutz-
Hooks fail-loud. Der ECHTE Drive-Round-Trip bleibt home/creds-gated (Schein-Test-Riegel; `gdrive.py`
selbst ist live verifiziert). Ausführen:  python3 System/connectors/tests/test_db_drive.py
"""
import gzip
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.dirname(_HERE)
if _CONNECTORS not in sys.path:
    sys.path.insert(0, _CONNECTORS)

import db_drive                                                           # noqa: E402


class FakeDriveFehler(RuntimeError):
    pass


class FakeGdrive:
    """In-memory-Drive, signatur-kompatibel zu `gdrive.py` (preflight/access_token/CRUD/liste_ordner).
    `korrumpiere(name, bytes) -> bytes` simuliert einen truncated/korrupten Upload (Read-back-Riegel)."""
    DriveFehler = FakeDriveFehler

    def __init__(self):
        self.ordner = {}          # name -> folder_id
        self.dateien = {}         # file_id -> (folder_id, name, bytes)
        self._next = 0
        self.korrumpiere = None

    def preflight(self):
        return {"ok": True}

    def access_token(self):
        return "fake-token"

    def ordner_finden_oder_anlegen(self, at, name="makro_fundamentals_db"):
        if name not in self.ordner:
            self._next += 1
            self.ordner[name] = f"ordner{self._next}"
        return self.ordner[name]

    def datei_anlegen(self, at, name, inhalt_bytes, parent_id, mime="application/gzip"):
        self._next += 1
        fid = f"f{self._next}"
        if self.korrumpiere is not None:
            inhalt_bytes = self.korrumpiere(name, inhalt_bytes)
        self.dateien[fid] = (parent_id, name, bytes(inhalt_bytes))
        return fid

    def datei_lesen(self, at, file_id, versuche=4):
        if file_id not in self.dateien:
            raise FakeDriveFehler(f"404: {file_id}")
        return self.dateien[file_id][2]

    def datei_loeschen(self, at, file_id):
        return self.dateien.pop(file_id, None) is not None

    def liste_ordner(self, at, parent_id, name_praefix=None):
        return {name: fid for fid, (p, name, _b) in self.dateien.items()
                if p == parent_id and (name_praefix is None or name.startswith(name_praefix))}

    # Test-Bequemlichkeit
    def namen(self, ordner_name):
        oid = self.ordner.get(ordner_name)
        return sorted(name for (p, name, _b) in self.dateien.values() if p == oid)

    def inhalt(self, ordner_name, name):
        oid = self.ordner.get(ordner_name)
        for (p, nm, b) in self.dateien.values():
            if p == oid and nm == name:
                return b
        return None


class TestSyncRestoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="db_drive_test_")
        self.g = FakeGdrive()
        self.pfad = os.path.join(self.dir, "quelle.db")
        with open(self.pfad, "wb") as f:
            f.write(b"SQLite-artiger Inhalt \x00\x01\x02 " * 50)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_byte_round_trip(self):
        name = db_drive.sync_db(self.pfad, "ordnerX", "db_", _gdrive=self.g)
        self.assertEqual(name, "db_0001.db.gz")
        # der Drive-Inhalt ist das exakte gz der lokalen Bytes
        with open(self.pfad, "rb") as f:
            roh = f.read()
        self.assertEqual(gzip.decompress(self.g.inhalt("ordnerX", name)), roh)
        # Restore in eine frische Datei -> byte-identisch
        ziel = os.path.join(self.dir, "restore.db")
        self.assertTrue(db_drive.restore_db(ziel, "ordnerX", "db_", _gdrive=self.g))
        with open(ziel, "rb") as f:
            self.assertEqual(f.read(), roh)

    def test_restore_ohne_bestand_false(self):
        ziel = os.path.join(self.dir, "restore.db")
        self.assertFalse(db_drive.restore_db(ziel, "leer", "db_", _gdrive=self.g))
        self.assertFalse(os.path.exists(ziel))                            # nichts geschrieben

    def test_versionsnummer_aus_maximum(self):
        oid = self.g.ordner_finden_oder_anlegen("tok", "ordnerX")
        self.g.datei_anlegen("tok", "db_0007.db.gz", gzip.compress(b"alt"), oid)
        name = db_drive.sync_db(self.pfad, "ordnerX", "db_", _gdrive=self.g)
        self.assertEqual(name, "db_0008.db.gz")                           # max+1, nicht len+1

    def test_retention_rotation(self):
        for _ in range(3):
            db_drive.sync_db(self.pfad, "ordnerX", "db_", retention=2, _gdrive=self.g)
        self.assertEqual(self.g.namen("ordnerX"), ["db_0002.db.gz", "db_0003.db.gz"])

    def test_readback_riegel_korrupter_upload(self):
        # 1. gesunder Stand
        db_drive.sync_db(self.pfad, "ordnerX", "db_", _gdrive=self.g)
        # 2. der nächste Upload kommt truncated an -> wirft, Alt bleibt, die korrupte Neue ist weg
        self.g.korrumpiere = lambda name, b: b[: len(b) // 2]
        with self.assertRaises(FakeDriveFehler):
            db_drive.sync_db(self.pfad, "ordnerX", "db_", _gdrive=self.g)
        self.assertEqual(self.g.namen("ordnerX"), ["db_0001.db.gz"])      # NICHTS gelöscht, Fragment weg

    def test_pre_check_hook_fail_loud_vor_upload(self):
        def pre_check(pfad):
            raise RuntimeError("lokal korrupt")
        with self.assertRaises(RuntimeError):
            db_drive.sync_db(self.pfad, "ordnerX", "db_", pre_check=pre_check, _gdrive=self.g)
        self.assertEqual(self.g.namen("ordnerX"), [])                     # kein Upload passiert

    def test_monotonie_hook_fail_loud_vor_upload(self):
        db_drive.sync_db(self.pfad, "ordnerX", "db_", _gdrive=self.g)
        gesehen = {}
        def monotonie(bestand):
            gesehen.update(bestand)
            raise RuntimeError("Drive jünger")
        with self.assertRaises(RuntimeError):
            db_drive.sync_db(self.pfad, "ordnerX", "db_", monotonie=monotonie, _gdrive=self.g)
        self.assertIn("db_0001.db.gz", gesehen)                           # Hook sah den Bestand
        self.assertEqual(self.g.namen("ordnerX"), ["db_0001.db.gz"])      # kein zweiter Upload

    def test_schutz_hook_verweigert_restore(self):
        db_drive.sync_db(self.pfad, "ordnerX", "db_", _gdrive=self.g)
        ziel = os.path.join(self.dir, "lokal.db")
        with open(ziel, "wb") as f:
            f.write(b"lokale Wahrheit")
        def schutz(lokal, drive_name):
            raise RuntimeError(f"verweigert: {drive_name}")
        with self.assertRaises(RuntimeError):
            db_drive.restore_db(ziel, "ordnerX", "db_", schutz=schutz, _gdrive=self.g)
        with open(ziel, "rb") as f:
            self.assertEqual(f.read(), b"lokale Wahrheit")                # unangetastet


if __name__ == "__main__":
    unittest.main(verbosity=2)
