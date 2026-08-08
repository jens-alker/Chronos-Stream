"""test_fundamentals_drive.py — Single-DB-File-Sync mit der Drive-DB (Jens 07.08.: eine DB, eine Datei).

Prüft: sync_hoch lädt die Cache-DB versioniert hoch + räumt alte Versionen auf; sync_restore holt die jüngste
zurück, überschreibt aber KEINE bereits dicke lokale DB. Gegen einen Fake-Drive (offline)."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONN = os.path.dirname(_HERE)
if _CONN not in sys.path:
    sys.path.insert(0, _CONN)

import fundamentals_drive as FD        # noqa: E402


class FakeDrive:
    """Minimaler Drive-Ersatz: ein Ordner {name -> (id, bytes)}."""
    def __init__(self):
        self.dateien = {}
        self._n = 0

    def ordner_finden_oder_anlegen(self, at, name=None):
        return "ordner1"

    def liste_ordner(self, at, parent_id, name_praefix=None):
        return {name: fid for name, (fid, _b) in self.dateien.items()}

    def datei_anlegen(self, at, name, inhalt_bytes, parent_id, mime=None):
        self._n += 1
        self.dateien[name] = (f"id{self._n}", bytes(inhalt_bytes))
        return f"id{self._n}"

    def datei_lesen(self, at, file_id, versuche=4):
        for _name, (fid, b) in self.dateien.items():
            if fid == file_id:
                return b
        raise KeyError(file_id)

    def datei_loeschen(self, at, file_id):
        for name, (fid, _b) in list(self.dateien.items()):
            if fid == file_id:
                del self.dateien[name]


class TestDBFileSync(unittest.TestCase):
    def _db(self, inhalt=b"x" * 60000):
        p = os.path.join(tempfile.mkdtemp(), "markt_cache.db")
        with open(p, "wb") as f:
            f.write(inhalt)
        return p

    def test_hoch_dann_restore_roundtrip(self):
        drive = FakeDrive()
        quelle = self._db(b"SQLITE-INHALT" * 5000)
        self.assertEqual(FD.sync_hoch(drive=drive, db_pfad=quelle), 1)
        self.assertTrue(any(n.startswith("markt_cache__") and n.endswith(".db") for n in drive.dateien))
        # Restore in eine FEHLENDE lokale DB
        ziel = os.path.join(tempfile.mkdtemp(), "markt_cache.db")
        self.assertEqual(FD.sync_restore(drive=drive, db_pfad=ziel), 1)
        with open(ziel, "rb") as f:
            self.assertEqual(f.read(), open(quelle, "rb").read())      # byte-gleich zurück

    def test_restore_clobbert_dicke_lokale_db_nicht(self):
        drive = FakeDrive()
        FD.sync_hoch(drive=drive, db_pfad=self._db(b"DRIVE" * 20000))
        lokal_dick = self._db(b"LOKAL-NEUER" * 20000)                  # >= 50KB
        self.assertEqual(FD.sync_restore(drive=drive, db_pfad=lokal_dick), 0)   # NICHT überschrieben
        self.assertIn(b"LOKAL-NEUER", open(lokal_dick, "rb").read())

    def test_hoch_raeumt_alte_versionen_auf(self):
        drive = FakeDrive()
        p = self._db()
        FD.sync_hoch(drive=drive, db_pfad=p)                           # markt_cache__1.db
        FD.sync_hoch(drive=drive, db_pfad=p)                           # markt_cache__2.db, __1 gelöscht
        versionen = [n for n in drive.dateien if n.startswith("markt_cache__")]
        self.assertEqual(versionen, ["markt_cache__2.db"])            # nur die neueste bleibt

    def test_restore_ohne_drive_datei_ist_null(self):
        self.assertEqual(FD.sync_restore(drive=FakeDrive(),
                                         db_pfad=os.path.join(tempfile.mkdtemp(), "leer.db")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
