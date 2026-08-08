"""
markt_db_aufbau.py — der Aufbau der strukturierten Markt-DB (`markt_db`) aus den rohen gzip-Caches (Jens 07.08.).

Die Architektur sieht `markt_db` als die quantitative DB vor (eod_preis/fundamentals/entity_meta,
PIT-Querschnitt) — aber ihr Produzent war NIE verdrahtet. Die Roh-EODHD-Dumps liegen in den gzip-Caches
(fundamentals_cache/eod_cache), die Analyse-Schicht liest sie direkt, und `markt_db` blieb leer → das
Cockpit-Datenstand-Panel zeigte „—".

Dieser Runner schließt die Naht (KEINE INSEL — er ruft `markt_db.aufbau_aus_caches`, das die schon gebauten
`migriere_*_aus_cache`-Primitiven nutzt). Nach dem Cache-Restore von Drive projiziert er die Caches in die
strukturierte DB:
  eod_cache/fundamentals_cache (Roh)  →  markt_db.aufbau_aus_caches  →  markt.db (eod_preis/fundamentals/entity_meta)
Danach zeigt Modul 17 (`frische`/`coverage`) echte Marktdaten, und der Rechenpfad kann die indizierte DB lesen.

**Governing Guardrail (Transparenz+Kontrolle):** der Fortschritt (Symbol i/n je Phase) geht über
`betrieb_aufsicht.status_haken` als `prozess_status` ins Modul-17-Prozess-Board (Balken + ETA), und der
Pause/Stop-Button wirkt verlustfrei zwischen den Symbolen (die Migration ist idempotent INSERT-OR-REPLACE).

**Vintage-Drift-Riegel:** der Roh-Cache bleibt die gespeicherte Quelle; markt_db ist ein PIT-deterministisches,
jederzeit reproduzierbares Derivat — kein mutables Zweit-Original.

Home-gated: braucht die restaurierten Caches (Drive-Restore zuerst). Der reine Orchestrier-Kern ist offline
getestet (injizierte Caches). Nur Standardbibliothek.
"""
import argparse
import datetime as _dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = os.path.dirname(_HERE)
for _p in (_HERE, _SYS, os.path.join(_SYS, "connectors")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PROZESS_NAME = "markt_db_aufbau"
_GIC_MAP_DEFAULT = os.path.join(_HERE, "retro_kat_map_gic_breit.json")


def lade_symbol_kategorie_map(pfad=None):
    """Die ROHE {symbol -> kategorie}-Klassifikation aus `retro_kat_map_gic_breit.json` (Feld `map`) — die
    volle Map OHNE die survivorship-/aktiv-Filter von `lade_breit_kat_map` (die formen Kategorie→Symbol-Folds
    für den Retro; hier ist die Firmen-Kategorie je Symbol gefragt, für `entity_meta`). Fehlt die Datei -> {}."""
    pfad = pfad or _GIC_MAP_DEFAULT
    try:
        with open(pfad, encoding="utf-8") as f:
            d = json.load(f)
        m = d.get("map") if isinstance(d, dict) else None       # TRIVIAL-QS: NUR das `map`-Feld (kein
        m = m if isinstance(m, dict) else {}                    # Fallback aufs Top-Level-dict -> keine Pseudo-Einträge)
        out = {str(s): k for s, k in m.items() if k}
    except (OSError, ValueError) as e:                           # Gemini-B1: kein stiller Fehlschlag (Stille≠Grün)
        print(f"  ⚠ GIC-Kategorie-Map nicht lesbar ({pfad}): {e} → entity_meta bleibt leer", file=sys.stderr)
        return {}
    if not out:                                                 # existiert, aber leer/kein `map`-Feld -> laut sagen
        print(f"  ⚠ GIC-Kategorie-Map leer ({pfad}) → keine Kategorie-Zuordnungen in markt_db", file=sys.stderr)
    return out


def lauf(markt_db_pfad, gic_map_pfad=None, ops_db=None, control_fn=None, heute=None,
         aufbau_fn=None, kat_map_fn=None):
    """Baut/aktualisiert markt_db aus den lokalen Caches. Fortschritt/Steuerung über die Control-Plane
    (Modul-17-Prozess-Board). `aufbau_fn`/`kat_map_fn` injizierbar (Offline-Test). -> Kennzahl-dict."""
    import betrieb_aufsicht as _B
    try:
        import fundamentals_cache as _fc                          # für den Korrupt-Zähler (Transparenz)
    except Exception:                                             # noqa: BLE001
        _fc = None
    heute = heute or _dt.date.today().isoformat()
    melde, wunsch = _B.status_haken(ops_db, PROZESS_NAME, control_fn=control_fn, reset_wunsch=True)
    melde(phase="start", aktuell=0, gesamt=0, zustand="läuft", beleg="Kategorie-Map laden")
    kat_map = (kat_map_fn or lade_symbol_kategorie_map)(gic_map_pfad)
    korrupt_vor = len(_fc.korrupt_pfade()) if _fc else 0

    def _n_korrupt():
        return (len(_fc.korrupt_pfade()) - korrupt_vor) if _fc else 0

    def melde_fn(phase, i, g):
        nk = _n_korrupt()
        extra = f" · ⚠ {nk} korrupt übersprungen" if nk else ""   # „Stille≠Grün": Skips auf der Karte sichtbar
        melde(phase=phase, aktuell=i, gesamt=g, zustand="läuft", beleg=f"{i}/{g} {phase} → markt_db{extra}")

    def abbrechen_fn():
        return wunsch() != "run"

    if aufbau_fn is None:
        import markt_db as _M
        aufbau_fn = _M.aufbau_aus_caches
    r = aufbau_fn(markt_db_pfad, kat_map=kat_map, t_ingest=heute,
                  melde_fn=melde_fn, abbrechen_fn=abbrechen_fn)
    nk = _n_korrupt()
    r["n_korrupt"] = nk
    korrupt_txt = f" · ⚠ {nk} korrupte Cache-Dateien übersprungen (Re-Fetch/Re-Restore)" if nk else ""
    if r.get("abgebrochen"):
        melde(phase="abgebrochen", zustand="pausiert",
              beleg=f"per Button pausiert/gestoppt (migrierte Symbole persistiert){korrupt_txt}")
        return {"status": "abgebrochen", **r}
    melde(phase="fertig", aktuell=r.get("bestand_eod"), gesamt=r.get("bestand_eod"), zustand="fertig",
          beleg=f"{r.get('bestand_eod')} EOD-Symbole · {r.get('n_fundamentals')} Fundamentals · "
                f"{r.get('n_meta')} Kategorie-Zuordnungen in markt_db{korrupt_txt}")
    return {"status": "fertig", **r}


def main(argv=None):
    ap = argparse.ArgumentParser(description="markt_db aus den lokalen Caches aufbauen (Cache→strukturierte "
                                             "Markt-DB; nach dem Drive-Restore auszuführen).")
    ap.add_argument("--markt-db", default=os.environ.get("MTF_MARKT_DB", "markt.db"),
                    help="Ziel-Markt-DB (Default: $MTF_MARKT_DB oder markt.db — die das Cockpit liest)")
    ap.add_argument("--gic-map", default=None, help="GIC-Klassifikation (Default: retro_kat_map_gic_breit.json)")
    ap.add_argument("--ops-db", default=os.environ.get("MTF_OPS_DB", "aufsicht.db"),
                    help="ops-DB (Control-Plane): Fortschritt/ETA ins Cockpit + Pause/Stop. Default: "
                         "$MTF_OPS_DB oder aufsicht.db")
    a = ap.parse_args(argv)
    print(f"=== markt_db-Aufbau === Ziel {a.markt_db}")
    print(f"    Fortschritt/Steuerung im Cockpit (Prozess-Board): ops-DB {a.ops_db} · Prozess '{PROZESS_NAME}'")
    r = lauf(a.markt_db, gic_map_pfad=a.gic_map, ops_db=a.ops_db)
    print(f"  Status: {r.get('status')}")
    print(f"  markt_db: {r.get('bestand_eod')} EOD-Symbole · {r.get('n_bars')} Bars · "
          f"{r.get('n_fundamentals')} Fundamentals · {r.get('n_meta')} Kategorie-Zuordnungen")
    if r.get("status") != "fertig":
        print("  (abgebrochen — erneut ausführen setzt idempotent fort)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
