"""
eod_drive.py — Sync des EOD-Preis-Caches in dieselbe führende Google-Drive-DB wie die Fundamentals (Jens 29.07.).

Jens (07.08.): alle Daten in EINER SQLite-DB (`markt_cache.db`, Fundamentals + EOD per Namespace getrennt IN
der DB). Damit ist der EOD-Sync KEIN eigener Datensatz mehr — `fundamentals_drive.sync_hoch`/`sync_restore`
synchronisieren die GANZE DB (beide Namespaces) als EINE Datei. Dieses Modul bleibt als dünner, namens-
kompatibler Delegat erhalten (Aufrufer, die historisch `eod_drive.sync_*` riefen), ruft aber denselben
DB-Sync — `cache_dir`/`namespace` sind bedeutungslos (die ganze DB ist eine Datei). Nur Standardbibliothek.
"""
import fundamentals_drive as _fd

NAMESPACE = "eod_"


def sync_restore(at, drive=None):
    """Drive-DB → lokale Cache-DB (die jüngste `markt_cache__<n>.db`, enthält EOD + Fundamentals).
    -> 1 (restauriert) oder 0. Identisch zu `fundamentals_drive.sync_restore` (eine DB, eine Datei)."""
    return _fd.sync_restore(at, drive=drive)


def sync_hoch(at, drive=None):
    """Lokale Cache-DB → Drive-DB (die ganze DB als neue Version). -> 1 (hochgeladen) oder 0.
    Identisch zu `fundamentals_drive.sync_hoch` (eine DB, eine Datei)."""
    return _fd.sync_hoch(at, drive=drive)
