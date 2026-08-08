"""
eod_cache.py — persistenter Voll-History-Cache der EODHD-EOD-Kurse (symmetrisch zum Fundamentals-Cache).

Jens (29.07.): die EOD-Preise sollen — wie die Fundamentals — persistiert und **in dieselbe führende Google-
Drive-DB** (`makro_fundamentals_db`) ausgelagert werden, damit die Outcome-Seite der Retro/Ablation reclaim-fest
und bei Wiederholung quota-frei ist (statt je Lauf das ganze Universum neu zu ziehen).

**Keine Insel / keine zweite Definition:** die Speicher-MECHANIK (gzip je Symbol, gebucketet, atomar, TTL) ist
datensatz-agnostisch und lebt bereits in `fundamentals_cache`. Dieses Modul gibt der EOD-Preis-Ablage nur einen
EIGENEN Ordner (`eod_cache/`) + Namen und **delegiert** an die geprüften `fundamentals_cache`-Funktionen (per
`cache_dir`). Auf der Drive-Seite trennt der `namespace="eod_"`-Präfix in `fundamentals_drive` die EOD-Shards von
den Fundamentals-Shards im selben Ordner (siehe `eod_drive`).

**Was gecacht wird:** die VOLLE EOD-History je Symbol (ein `fetch_eod(sym)`-Voll-Abruf = 1 API-Einheit). Aus
dieser einen Ablage schneidet `eodhd_prices.fetch_eod_cached` lokal jedes benötigte Fenster — ein Symbol wird je
Lauf höchstens EINMAL gezogen, alle Stichtage/Fenster teilen sich denselben Cache. Historische Bars werden nicht
revidiert → für Retro `max_alter_tage=None` (nie ablaufen); Live-Frische über eine TTL (siehe `hole`).

Nur Standardbibliothek.
"""
import os

import fundamentals_cache as _fc

_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_HERE, "eod_cache")


def ist_gecacht(symbol, cache_dir=_CACHE_DIR):
    """True, wenn für das Symbol eine (auch leere/No-Data-)EOD-History gecacht ist → kein Re-Fetch nötig."""
    return _fc.ist_gecacht(symbol, cache_dir)


def lade(symbol, cache_dir=_CACHE_DIR, max_alter_tage=None, _jetzt=None):
    """Gecachte EOD-History (Liste EOD-Zeilen, ggf. leere Liste = No-Data) oder None (nicht gecacht/abgelaufen/
    korrupt → Re-Fetch). `max_alter_tage`: optionale TTL (Datei-mtime) für den Live-Frischebedarf; Default None =
    kein Verfall (korrekt für Retro/PIT: historische Bars ändern sich nie). Delegiert an `fundamentals_cache`."""
    return _fc.lade(symbol, cache_dir, max_alter_tage=max_alter_tage, _jetzt=_jetzt)


def speichere(symbol, data, cache_dir=_CACHE_DIR):
    """EOD-History (Liste) ODER [] (No-Data-Marker) gzip-atomar ablegen. Delegiert an `fundamentals_cache`."""
    _fc.speichere(symbol, data, cache_dir)


def hole(symbol, fetch_fn, cache_dir=_CACHE_DIR, max_alter_tage=None):
    """Cache-first: gecachte EOD-History zurückgeben, sonst `fetch_fn(symbol)` (die VOLLE History; DARF werfen —
    harte Fehler propagieren, werden NICHT gecacht), speichern, zurückgeben. `fetch_fn` liefert eine Liste ODER []
    (No-Data → Marker, nie wieder abgerufen). `max_alter_tage`: TTL für Live-Frische. Delegiert an `fundamentals_cache`."""
    return _fc.hole(symbol, fetch_fn, cache_dir, max_alter_tage=max_alter_tage)


def bestand(cache_dir=_CACHE_DIR):
    """(anzahl_eintraege, bytes_auf_disk) — für den Größen-Checkpoint. Delegiert an `fundamentals_cache`."""
    return _fc.bestand(cache_dir)


def symbole(cache_dir=_CACHE_DIR):
    """Alle gecachten EOD-Symbole (sortiert). Delegiert an `fundamentals_cache.symbole`."""
    return _fc.symbole(cache_dir)
