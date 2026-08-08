"""
test_scraper_db_drive.py — Offline-Tests des scraper.db→Drive-Syncs (Konzept B §8, Fake-gdrive injiziert).

Realdaten-nah (Schein-Test-Riegel): die Quell-DB ist eine ECHTE temp-SQLite im scraper.db-`documents`-
Schema (`sammler_db.SCHEMA_DOCUMENTS`), der Snapshot ein echtes `VACUUM INTO`. Geprüft: Snapshot +
quick_check + doc-count, Rollen-Gate, Byte-Round-Trip sync→restore, Manifest (Hash-Skip beim Restore),
Zeilenzahl-/Drive-Monotonie-Guards (+force), Schema-Version-Guard, Restore-Schutz (Sicherung, nie blind),
Orphan-Cleanup, gestaffelte Retention (Wochenstand), Snapshot-fail-safe. Der ECHTE Drive-Round-Trip
bleibt home/creds-gated. Ausführen:  python3 System/connectors/tests/test_scraper_db_drive.py
"""
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.dirname(_HERE)
for _p in (_CONNECTORS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scraper_db_drive as SD                                             # noqa: E402
import db_drive                                                           # noqa: E402
from sammler_db import SCHEMA_DOCUMENTS                                   # noqa: E402  (das ECHTE Schema)
from test_db_drive import FakeGdrive, FakeDriveFehler                     # noqa: E402  (geteilter Fake)


def _mach_db(pfad, n_docs=5):
    """Echte scraper.db-artige SQLite (documents-Schema aus sammler_db) mit n Dokumenten."""
    conn = sqlite3.connect(pfad)
    conn.executescript(SCHEMA_DOCUMENTS)
    for i in range(n_docs):
        conn.execute("INSERT INTO documents(source_type, title, text, published_at, ingested_at) "
                     "VALUES ('paper', ?, ?, ?, ?)",
                     (f"Paper {i}", f"abstract {i}", f"2026-07-{i+1:02d}", "2026-07-30 12:00:00"))
    conn.commit()
    conn.close()


class Basis(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="scraper_drive_test_")
        self.g = FakeGdrive()
        self.db = os.path.join(self.dir, "scraper.db")
        _mach_db(self.db, n_docs=5)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _sync(self, **kw):
        kw.setdefault("rolle", "home")
        kw.setdefault("jetzt", "2026-08-06 12:00:00")
        kw.setdefault("_gdrive", self.g)
        return SD.sync_scraper_db(self.db, **kw)


class TestSnapshot(Basis):
    def test_vacuum_into_snapshot_konsistent(self):
        snap = SD.snapshot_erstellen(self.db)
        try:
            self.assertTrue(os.path.exists(snap))
            self.assertNotEqual(snap, self.db)
            n, sv = SD._quick_check_und_count(snap)
            self.assertEqual(n, 5)                                        # alle Zeilen im Snapshot
            self.assertEqual(sv, 0)                                       # SQLite-Default user_version
        finally:
            os.unlink(snap)

    def test_snapshot_fehler_fail_safe(self):
        # ein Verzeichnis als "DB" -> VACUUM INTO scheitert -> None + Status-dict, KEIN Raise (Fable-B12)
        r = SD.sync_scraper_db(self.dir, rolle="home", snapshot_versuche=1, _gdrive=self.g)
        self.assertEqual(r["status"], "snapshot_fehler")
        self.assertEqual(self.g.dateien, {})                              # kein Drive-Zugriff passiert

    def test_quick_check_fail_loud(self):
        # kaputte Datei (kein SQLite) -> _quick_check_und_count wirft (Fable-B10, nie hochladen)
        kaputt = os.path.join(self.dir, "kaputt.db")
        with open(kaputt, "wb") as f:
            f.write(b"das ist keine sqlite-datei" * 100)
        with self.assertRaises(Exception):
            SD._quick_check_und_count(kaputt)

    def test_ohne_documents_tabelle_fail_loud(self):
        leer = os.path.join(self.dir, "leer.db")
        sqlite3.connect(leer).close()                                     # valide SQLite, aber ohne documents
        with self.assertRaises(sqlite3.OperationalError):
            SD._quick_check_und_count(leer)


class TestRollenGate(Basis):
    def test_ohne_home_rolle_inaktiv(self):
        r = SD.sync_scraper_db(self.db, rolle="", _gdrive=self.g)
        self.assertEqual(r["status"], "inaktiv")
        self.assertEqual(self.g.dateien, {})                              # kein Upload (Fable-B9)

    def test_cloud_rolle_inaktiv(self):
        r = SD.sync_scraper_db(self.db, rolle="cloud", _gdrive=self.g)
        self.assertEqual(r["status"], "inaktiv")


class TestSyncUndManifest(Basis):
    def test_sync_happy_path_und_manifest(self):
        r = self._sync()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["datei"], "scraper_db_v0001.db.gz")
        self.assertEqual(r["doc_count"], 5)
        namen = self.g.namen(SD._DRIVE_ORDNER)
        self.assertIn("scraper_db_v0001.db.gz", namen)
        self.assertIn(SD._MANIFEST_NAME, namen)
        self.assertIn("scraper_db_woche_2026W32.db.gz", namen)            # Wochenstand (6.8.2026 = KW32)
        man = json.loads(self.g.inhalt(SD._DRIVE_ORDNER, SD._MANIFEST_NAME).decode("utf-8"))
        self.assertEqual(man["doc_count"], 5)
        self.assertEqual(man["schema_version"], 0)
        self.assertEqual(man["datei"], "scraper_db_v0001.db.gz")
        self.assertEqual(man["timestamp"], "2026-08-06 12:00:00")
        # der Upload-Hash ist der Hash der ENTPACKTEN Snapshot-Bytes
        gz = self.g.inhalt(SD._DRIVE_ORDNER, "scraper_db_v0001.db.gz")
        self.assertEqual(SD._sha256_bytes(gzip.decompress(gz)), man["sha256"])

    def test_zweiter_sync_rotiert_und_erneuert_manifest(self):
        self._sync()
        os.remove(self.db)
        _mach_db(self.db, n_docs=7)                                       # DB gewachsen
        r2 = self._sync(jetzt="2026-08-07 12:00:00")
        self.assertEqual(r2["datei"], "scraper_db_v0002.db.gz")
        man = json.loads(self.g.inhalt(SD._DRIVE_ORDNER, SD._MANIFEST_NAME).decode("utf-8"))
        self.assertEqual(man["doc_count"], 7)
        # genau EIN Manifest (das alte nach dem neuen gelöscht)
        namen = self.g.namen(SD._DRIVE_ORDNER)
        self.assertEqual(namen.count(SD._MANIFEST_NAME), 1)

    def test_zeilenzahl_monotonie_guard(self):
        self._sync()                                                      # Upload mit 5 Docs
        os.remove(self.db)
        _mach_db(self.db, n_docs=1)                                       # lokal "entleert"
        with self.assertRaises(RuntimeError):
            self._sync(jetzt="2026-08-07 12:00:00")
        # force übergeht bewusst
        r = self._sync(jetzt="2026-08-07 13:00:00", force=True)
        self.assertEqual(r["status"], "ok")

    def test_drive_monotonie_guard_juengeres_manifest(self):
        self._sync(jetzt="2026-08-06 12:00:00")
        # eine "ältere" Maschine (rückdatiertes jetzt) darf den jüngeren Drive-Stand nicht wegrotieren
        with self.assertRaises(RuntimeError):
            self._sync(jetzt="2026-08-01 12:00:00")
        r = self._sync(jetzt="2026-08-01 13:00:00", force=True)           # nur force übergeht
        self.assertEqual(r["status"], "ok")

    def test_korruptes_manifest_fail_loud(self):
        oid = self.g.ordner_finden_oder_anlegen("tok", SD._DRIVE_ORDNER)
        self.g.datei_anlegen("tok", SD._MANIFEST_NAME, b"kein json {", oid)
        with self.assertRaises(RuntimeError):
            self._sync()
        r = self._sync(force=True)                                        # force: wie fehlend behandelt
        self.assertEqual(r["status"], "ok")

    def test_readback_riegel_erbt_vom_kern(self):
        self._sync()
        self.g.korrumpiere = lambda name, b: (b[: len(b) // 2] if name.startswith(SD._PRAEFIX) else b)
        with self.assertRaises(FakeDriveFehler):
            self._sync(jetzt="2026-08-07 12:00:00")
        # der alte Stand + sein Manifest sind unangetastet
        self.assertIn("scraper_db_v0001.db.gz", self.g.namen(SD._DRIVE_ORDNER))
        man = json.loads(self.g.inhalt(SD._DRIVE_ORDNER, SD._MANIFEST_NAME).decode("utf-8"))
        self.assertEqual(man["datei"], "scraper_db_v0001.db.gz")


class TestOrphanUndWoche(Basis):
    def test_orphan_cleanup_entfernt_nur_fragmente(self):
        oid = self.g.ordner_finden_oder_anlegen("tok", SD._DRIVE_ORDNER)
        self.g.datei_anlegen("tok", "scraper_db_v0009.db.gz.part", b"fragment", oid)   # Upload-Leiche
        self.g.datei_anlegen("tok", "scraper_db_tmp_upload", b"leiche", oid)           # Fragment
        self.g.datei_anlegen("tok", "fremde_datei.txt", b"fremd", oid)                 # NICHT unser Namensraum
        r = self._sync()
        self.assertEqual(r["orphans_entfernt"], 2)
        namen = self.g.namen(SD._DRIVE_ORDNER)
        self.assertNotIn("scraper_db_v0009.db.gz.part", namen)
        self.assertNotIn("scraper_db_tmp_upload", namen)
        self.assertIn("fremde_datei.txt", namen)                          # fremde Dateien nie angefasst

    def test_wochenstand_einmal_pro_woche_und_rotiert(self):
        self._sync(jetzt="2026-08-06 12:00:00")                           # KW32
        r2 = self._sync(jetzt="2026-08-07 12:00:00")                      # gleiche Woche
        self.assertEqual(r2["wochenstand"], "scraper_db_woche_2026W32.db.gz")
        namen = self.g.namen(SD._DRIVE_ORDNER)
        self.assertEqual(sum(1 for n in namen if n.startswith(SD._WOCHE_PRAEFIX)), 1)
        r3 = self._sync(jetzt="2026-08-13 12:00:00")                      # KW33 -> neue Woche, alte weg
        self.assertEqual(r3["wochenstand"], "scraper_db_woche_2026W33.db.gz")
        namen = self.g.namen(SD._DRIVE_ORDNER)
        self.assertEqual([n for n in namen if n.startswith(SD._WOCHE_PRAEFIX)],
                         ["scraper_db_woche_2026W33.db.gz"])

    def test_taegliche_retention_beruehrt_wochenstand_nicht(self):
        for tag in (6, 7, 8):
            self._sync(jetzt=f"2026-08-{tag:02d} 12:00:00")
        namen = self.g.namen(SD._DRIVE_ORDNER)
        taeglich = [n for n in namen if SD._RE_VERSION.fullmatch(n)]
        self.assertEqual(taeglich, ["scraper_db_v0002.db.gz", "scraper_db_v0003.db.gz"])  # Retention 2
        self.assertIn("scraper_db_woche_2026W32.db.gz", namen)            # Wochenstand bleibt


class TestRestore(Basis):
    def test_round_trip_byte_identisch(self):
        self._sync()
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        r = SD.restore_scraper_db(ziel, _gdrive=self.g)
        self.assertEqual(r["status"], "restauriert")
        self.assertTrue(r["download"])
        # byte-identisch zum SNAPSHOT-Inhalt (VACUUM-kompaktiert): gleiche sha wie im Manifest,
        # und die Daten sind vollständig zurücklesbar
        man = json.loads(self.g.inhalt(SD._DRIVE_ORDNER, SD._MANIFEST_NAME).decode("utf-8"))
        self.assertEqual(SD._sha256_datei(ziel), man["sha256"])
        conn = sqlite3.connect(ziel)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)
        conn.close()

    def test_hash_gleich_kein_download(self):
        self._sync()
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        SD.restore_scraper_db(ziel, _gdrive=self.g)
        r2 = SD.restore_scraper_db(ziel, _gdrive=self.g)                  # Gemini-B2: Manifest-Hash-Skip
        self.assertEqual(r2["status"], "aktuell")
        self.assertFalse(r2["download"])

    def test_schema_version_guard(self):
        self._sync()
        man = json.loads(self.g.inhalt(SD._DRIVE_ORDNER, SD._MANIFEST_NAME).decode("utf-8"))
        man["schema_version"] = SD.VERSTANDENE_SCHEMA_VERSION + 1         # Home migrierte weiter
        oid = self.g.ordner_finden_oder_anlegen("tok", SD._DRIVE_ORDNER)
        for fid, (p, nm, _b) in list(self.g.dateien.items()):
            if nm == SD._MANIFEST_NAME:
                self.g.dateien[fid] = (p, nm, json.dumps(man).encode("utf-8"))
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        with self.assertRaises(RuntimeError):
            SD.restore_scraper_db(ziel, _gdrive=self.g)                   # Gemini-B3: fail-loud
        self.assertFalse(os.path.exists(ziel))

    def test_restore_schutz_groessere_lokale_db(self):
        self._sync()                                                      # Drive-Stand: 5 Docs
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        _mach_db(ziel, n_docs=9)                                          # lokal GRÖSSER (9 > 5)
        with self.assertRaises(RuntimeError):
            SD.restore_scraper_db(ziel, _gdrive=self.g)                   # kein blinder Rückschritt
        conn = sqlite3.connect(ziel)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 9)  # unangetastet
        conn.close()
        # force: erst Sicherung (Rename), dann Ersatz
        r = SD.restore_scraper_db(ziel, force=True, jetzt="2026-08-06 13:00:00", _gdrive=self.g)
        self.assertEqual(r["status"], "restauriert")
        self.assertTrue(r["sicherung"] and os.path.exists(r["sicherung"]))
        sich = sqlite3.connect(r["sicherung"])
        self.assertEqual(sich.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 9)  # gesichert
        sich.close()
        conn = sqlite3.connect(ziel)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)  # Drive-Stand
        conn.close()

    def test_restore_hash_mismatch_fail_loud(self):
        self._sync()
        # die gz-Datei auf Drive nachträglich korrumpieren (Manifest-Hash passt nicht mehr)
        for fid, (p, nm, b) in list(self.g.dateien.items()):
            if nm == "scraper_db_v0001.db.gz":
                self.g.dateien[fid] = (p, nm, gzip.compress(b"anderer inhalt"))
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        with self.assertRaises(RuntimeError):
            SD.restore_scraper_db(ziel, _gdrive=self.g)
        self.assertFalse(os.path.exists(ziel))                            # NICHTS geschrieben

    def test_legacy_restore_ohne_manifest(self):
        # nur eine versionierte gz, KEIN Manifest (Kaltstart/Legacy) -> Fallback über den geteilten Kern
        db_drive.sync_db(self.db, SD._DRIVE_ORDNER, SD._PRAEFIX, _gdrive=self.g)
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        r = SD.restore_scraper_db(ziel, _gdrive=self.g)
        self.assertEqual(r["status"], "restauriert")
        self.assertFalse(r["manifest"])
        # vorhandene lokale DB ohne Manifest-Beleg -> fail-loud ohne force
        with self.assertRaises(RuntimeError):
            SD.restore_scraper_db(ziel, _gdrive=self.g)
        r2 = SD.restore_scraper_db(ziel, force=True, jetzt="2026-08-06 14:00:00", _gdrive=self.g)
        self.assertEqual(r2["status"], "restauriert")                     # force: Sicherung + Restore

    def test_leerer_drive_ohne_alles(self):
        ziel = os.path.join(self.dir, "cloud_kopie.db")
        r = SD.restore_scraper_db(ziel, _gdrive=self.g)
        self.assertEqual(r["status"], "leer")
        self.assertFalse(os.path.exists(ziel))


if __name__ == "__main__":
    unittest.main(verbosity=2)
