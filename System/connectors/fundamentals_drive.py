"""
fundamentals_drive.py — Sync der EINEN Cache-DB (Fundamentals + EOD) mit der führenden Google-Drive-DB.

Jens (07.08.): alle Daten in EINER SQLite-DB (`fundamentals_cache` = `markt_cache.db`, EOD + Fundamentals per
Namespace getrennt IN der DB). Die alte gzip-Shard-/Bucket-/Manifest-Mechanik ist damit obsolet — eine DB synct
man als EINE Datei. Reclaim-Festigkeit (Cloud) + optionale Auslagerung: die DB-Datei wird versioniert nach Drive
hoch- (`sync_hoch`) und bei kaltem/fehlendem lokalem Stand von dort restauriert (`sync_restore`).

`sync_hoch`/`sync_restore` behalten ihre Signatur (die Aufrufer — datenpflege/backfill/retro — bleiben gleich);
`cache_dir`/`namespace` werden ignoriert (die ganze DB ist EINE Datei). `drive` injizierbar (Test). Nur
Standardbibliothek + der `gdrive`-REST-Konnektor.
"""
import os
import re
import sqlite3

import fundamentals_cache

_DATEI_RE = re.compile(r"^markt_cache__(\d+)\.db$")   # versionierte DB-Datei auf Drive: markt_cache__<n>.db
_MIN_LOKAL_BYTES = 50_000   # lokale DB kleiner -> als "dünn/kalt" behandeln (Restore sinnvoll)


def _gdrive():
    import gdrive
    return gdrive


def _db_pfad(db_pfad=None):
    return db_pfad or fundamentals_cache._DB_PFAD


def _checkpoint(p):
    """WAL in die Haupt-DB-Datei falten (TRUNCATE), BEVOR wir die Datei kopieren — sonst lägen die jüngsten
    Commits nur im `-wal`-Sidecar und gingen beim Datei-Sync verloren. Fail-safe (kein DB da / gesperrt -> egal)."""
    if not os.path.exists(p):
        return
    try:
        c = sqlite3.connect(p, timeout=30.0)
        try:
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.commit()
        finally:
            c.close()
    except sqlite3.Error:
        pass


def _sidecars_weg(p):
    """Verwaiste `-wal`/`-shm`-Sidecars nach dem Datei-Restore entfernen — sonst würde SQLite den alten WAL auf
    die frisch restaurierte DB anwenden (die restaurierte Datei ist die Autorität)."""
    for suf in ("-wal", "-shm"):
        try:
            os.remove(p + suf)
        except OSError:
            pass


def _neueste_db(dateien):
    """{name->id} -> (id, n) der jüngsten `markt_cache__<n>.db`, sonst (None, 0)."""
    best = (None, -1)
    for name, fid in dateien.items():
        m = _DATEI_RE.match(name)
        if m:
            n = int(m.group(1))
            if n > best[1]:
                best = (fid, n)
    return best if best[0] else (None, 0)


def sync_restore(at=None, cache_dir=None, drive=None, namespace=None, db_pfad=None):
    """Drive-DB → lokale Cache-DB: die jüngste `markt_cache__<n>.db` herunterladen, NUR wenn die lokale DB fehlt
    oder dünn ist (kein Clobbern eines volleren lokalen Stands). -> 1 (restauriert) oder 0. `drive` injizierbar."""
    p = _db_pfad(db_pfad)
    if os.path.exists(p) and os.path.getsize(p) >= _MIN_LOKAL_BYTES:
        return 0                                          # lokale DB ist schon dick -> nicht überschreiben
    drive = drive or _gdrive()
    fid = drive.ordner_finden_oder_anlegen(at)
    fdid, _n = _neueste_db(drive.liste_ordner(at, fid))
    if not fdid:
        return 0                                          # nichts auf Drive (Erstlauf)
    roh = drive.datei_lesen(at, fdid)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "wb") as f:
        f.write(roh)
    os.replace(tmp, p)                                    # atomar
    _sidecars_weg(p)                                      # alten WAL/SHM verwerfen (restaurierte DB = Autorität)
    return 1


def sync_hoch(at=None, cache_dir=None, drive=None, namespace=None, db_pfad=None):
    """Lokale Cache-DB → Drive-DB: die DB-Datei als NEUE Version `markt_cache__<n+1>.db` hochladen, ältere
    Versionen löschen (REST-Delete). -> 1 (hochgeladen) oder 0 (keine lokale DB). `drive` injizierbar."""
    p = _db_pfad(db_pfad)
    if not os.path.exists(p):
        return 0
    _checkpoint(p)                                        # jüngste Commits aus dem WAL in die Datei falten
    with open(p, "rb") as f:
        inhalt = f.read()
    drive = drive or _gdrive()
    fid = drive.ordner_finden_oder_anlegen(at)
    dateien = drive.liste_ordner(at, fid)
    _fdid, counter = _neueste_db(dateien)
    neu = f"markt_cache__{counter + 1}.db"
    drive.datei_anlegen(at, neu, inhalt, fid, mime="application/x-sqlite3")
    for name, sid in dateien.items():                     # alte Versionen aufräumen (das neue ist frisch)
        if _DATEI_RE.match(name) and name != neu:
            drive.datei_loeschen(at, sid)
    return 1
