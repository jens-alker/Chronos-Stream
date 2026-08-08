"""
fundamentals_cache.py — geteilter Voll-Dump-Cache (EODHD Fundamentals + EOD-Kurse) in EINER SQLite-DB.

Jens (07.08., harte Ansage): SCHLUSS mit den gzip-Datei-Caches. ALLE Daten in EINE Datenbank. Kein
`<bucket>/<sym>.json.gz`-Wildwuchs mehr, kein „Daten liegen auf Drive statt lokal"-Chaos. Dieser Cache ist
jetzt eine reine SQLite-Tabelle: EIN DB-File, `(namespace, symbol) -> gzip(JSON)`. `namespace` = der bisherige
`cache_dir`-Basename (`fundamentals_cache` bzw. `eod_cache`) — so teilen sich beide Datensorten EINE DB, sauber
getrennt. Die öffentliche Schnittstelle (`hole`/`lade`/`speichere`/`ist_gecacht`/`bestand`/`symbole`) ist
UNVERÄNDERT — alle Aufrufer (Modul 9, Klassifikation, Retro-`_outcome_map` über `eod_cache`) bleiben gleich.

DB-Pfad: `$MTF_CACHE_DB` oder Default `System/connectors/markt_cache.db`. WAL + busy_timeout → der multi-thread-
Scraper UND die Retro-/Ablations-Prozesse greifen gleichzeitig zu, ohne sich zu blockieren.

**Reine Storage-Schicht:** KEINE Fetch-/Fehlerlogik (bleibt bei den Abrufern). `hole(symbol, fetch_fn)` ist
cache-first; harte Fehler wirft `fetch_fn` und werden NICHT gecacht (Retry beim nächsten Lauf). Ein leeres `{}`
= No-Data-Marker (Symbol ohne Datensatz → nie wieder abrufen). `migriere_gzip_zu_db` importiert einen evtl.
vorhandenen Alt-gzip-Cache einmalig. Nur Standardbibliothek.
"""
import gzip
import json
import os
import sqlite3
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# Alt-Konstante bleibt als NAMESPACE-Quelle erhalten (Aufrufer geben cache_dir=…/fundamentals_cache bzw.
# …/eod_cache; der Basename ist der Namespace). Der Pfad selbst wird NICHT mehr als Verzeichnis benutzt.
_CACHE_DIR = os.path.join(_HERE, "fundamentals_cache")
_DB_PFAD = os.environ.get("MTF_CACHE_DB") or os.path.join(_HERE, "markt_cache.db")
_KORRUPT_GEMELDET = set()


def _namespace(cache_dir):
    """cache_dir -> Namespace (der Basename, z. B. 'fundamentals_cache' / 'eod_cache'). Trennt die Datensorten
    in der EINEN DB. None/leer -> 'fundamentals_cache' (Default-Aufrufer)."""
    return os.path.basename(str(cache_dir).rstrip("/\\")) or "fundamentals_cache"


def _conn(db_pfad=None):
    """Verbindung zur Cache-DB (WAL + busy_timeout für gleichzeitige Scraper-/Retro-Zugriffe). Legt die Tabelle
    idempotent an. Eine Verbindung JE Aufruf (multi-prozess-sicher; SQLite-Open ist billig)."""
    c = sqlite3.connect(db_pfad or _DB_PFAD, timeout=30.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("CREATE TABLE IF NOT EXISTS cache_eintrag ("
              "namespace TEXT NOT NULL, symbol TEXT NOT NULL, data BLOB NOT NULL, "
              "t_ingest TEXT NOT NULL DEFAULT (datetime('now')), "
              "PRIMARY KEY(namespace, symbol))")
    return c


def ist_gecacht(symbol, cache_dir=_CACHE_DIR):
    """True, wenn für das Symbol ein (auch leerer/No-Data-)Eintrag existiert → kein Re-Fetch nötig."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM cache_eintrag WHERE namespace=? AND symbol=?",
                         (_namespace(cache_dir), symbol)).fetchone() is not None


def lade(symbol, cache_dir=_CACHE_DIR, max_alter_tage=None, _jetzt=None):
    """Gecachten Voll-Dump (dict/list, ggf. leeres {} = No-Data) oder None (nicht gecacht / korrupt → Re-Fetch).
    **TTL:** `max_alter_tage` gesetzt → ein Eintrag ÄLTER als N Tage (`t_ingest`) gilt als abgelaufen → None.
    Default None = kein Verfall (korrekt für Retro/PIT). `_jetzt` (Epoch-Sekunden) injizierbar (Test)."""
    with _conn() as c:
        row = c.execute("SELECT data, t_ingest FROM cache_eintrag WHERE namespace=? AND symbol=?",
                        (_namespace(cache_dir), symbol)).fetchone()
    if row is None:
        return None
    blob, t_ingest = row
    if max_alter_tage is not None:
        jetzt = time.time() if _jetzt is None else _jetzt
        try:
            eingespielt = time.mktime(time.strptime(t_ingest, "%Y-%m-%d %H:%M:%S"))
            if (jetzt - eingespielt) / 86400.0 > max_alter_tage:
                return None                          # abgelaufen → wie nicht-gecacht (Re-Fetch)
        except (ValueError, TypeError):
            pass                                     # unlesbares Datum → nicht verfallen lassen (fail-open)
    try:
        return json.loads(gzip.decompress(blob).decode("utf-8"))
    except (OSError, ValueError, EOFError) as e:      # korrupter BLOB → wie nicht-gecacht (Re-Fetch), nie Crash
        schluessel = f"{_namespace(cache_dir)}:{symbol}"
        if schluessel not in _KORRUPT_GEMELDET:
            _KORRUPT_GEMELDET.add(schluessel)
            import sys
            print(f"  ⚠ Cache-Eintrag korrupt, übersprungen (Re-Fetch nötig): {schluessel} ({type(e).__name__})",
                  file=sys.stderr)
        return None


def speichere(symbol, data, cache_dir=_CACHE_DIR):
    """Voll-Dump als gzip(JSON)-BLOB in die DB (INSERT OR REPLACE — atomar je Zeile). `data`: der volle dict
    ODER {} (No-Data-Marker)."""
    blob = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO cache_eintrag(namespace,symbol,data,t_ingest) "
                  "VALUES(?,?,?,datetime('now'))", (_namespace(cache_dir), symbol, blob))
        c.commit()


def hole(symbol, fetch_fn, cache_dir=_CACHE_DIR, max_alter_tage=None):
    """Cache-first: gecachten Voll-Dump zurückgeben, sonst `fetch_fn(symbol)` (DARF werfen — harte Fehler
    propagieren und werden NICHT gecacht), das Ergebnis speichern und zurückgeben. `fetch_fn` liefert den
    vollen dict ODER {} (No-Data → wird als Marker gecacht, nie wieder abgerufen)."""
    c = lade(symbol, cache_dir, max_alter_tage=max_alter_tage)
    if c is not None:
        return c
    data = fetch_fn(symbol)
    if isinstance(data, (dict, list)):               # nur wohlgeformte Antworten cachen (inkl. {} = No-Data)
        speichere(symbol, data, cache_dir)
    return data


def frische_tage(symbol, cache_dir=_CACHE_DIR):
    """Alter des Cache-Eintrags in Tagen (aus `t_ingest`) oder None (nicht gecacht) — für Frische-Prüfungen
    (ersetzt die alte Datei-mtime, 07.08.). Unlesbares Datum -> 0.0 (frisch)."""
    with _conn() as c:
        row = c.execute("SELECT t_ingest FROM cache_eintrag WHERE namespace=? AND symbol=?",
                        (_namespace(cache_dir), symbol)).fetchone()
    if row is None:
        return None
    try:
        return (time.time() - time.mktime(time.strptime(row[0], "%Y-%m-%d %H:%M:%S"))) / 86400.0
    except (ValueError, TypeError):
        return 0.0


def bestand(cache_dir=_CACHE_DIR):
    """(anzahl_eintraege, bytes_in_der_db) für den Namespace — für den Größen-Checkpoint."""
    with _conn() as c:
        row = c.execute("SELECT count(*), COALESCE(SUM(LENGTH(data)),0) FROM cache_eintrag WHERE namespace=?",
                        (_namespace(cache_dir),)).fetchone()
    return int(row[0]), int(row[1])


def korrupt_pfade():
    """Die als korrupt erkannten Cache-Schlüssel dieses Prozesses (für die Transparenz-Schicht, „Stille≠Grün").
    -> frozenset."""
    return frozenset(_KORRUPT_GEMELDET)


def symbole(cache_dir=_CACHE_DIR):
    """Alle gecachten Symbole des Namespace (sortiert) — der Enumerator für die markt_db-Aggregation. -> [sym, …]."""
    with _conn() as c:
        return sorted(r[0] for r in c.execute("SELECT symbol FROM cache_eintrag WHERE namespace=?",
                                              (_namespace(cache_dir),)).fetchall())


def migriere_gzip_zu_db(alt_cache_dir, cache_dir=None, db_pfad=None):
    """Einmalig: einen ALT-gzip-Cache (`<bucket>/<sym>.json.gz` unter `alt_cache_dir`) in die DB importieren.
    `cache_dir` bestimmt den Ziel-Namespace (Default = `alt_cache_dir`). Idempotent (INSERT OR REPLACE).
    -> Anzahl migrierter Einträge. Fehlt das Alt-Verzeichnis -> 0 (nichts zu tun)."""
    ns = _namespace(cache_dir or alt_cache_dir)
    if not os.path.isdir(alt_cache_dir):
        return 0
    n = 0
    with _conn(db_pfad) as c:
        for wurzel, _dirs, dateien in os.walk(alt_cache_dir):
            for d in dateien:
                if not d.endswith(".json.gz"):
                    continue
                p = os.path.join(wurzel, d)
                try:
                    with gzip.open(p, "rt", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError, EOFError):
                    continue                          # korrupte Alt-Datei überspringen
                blob = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                c.execute("INSERT OR REPLACE INTO cache_eintrag(namespace,symbol,data,t_ingest) "
                          "VALUES(?,?,?,datetime('now'))", (ns, d[:-len(".json.gz")], blob))
                n += 1
        c.commit()
    return n
