#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""betrieb_aufsicht_lauf.py — der v0-RUNNER der prozess-unabhängigen Aufsicht (Schicht S).

Evaluiert den ECHTEN Betrieb (scraper.db-Kontrakte, Prozess-Liveness, Frische) und schreibt die ops-DB,
die das Cockpit (Modul 17, Leitstand) liest. KEINE INSEL: orchestriert nur `betrieb_aufsicht`s Evaluatoren
(`pruefe_dokument_kontrakte`) + Writer (`schreibe_gesundheit`), definiert KEINE zweite Gesundheits-Logik.
Ein Tick = eine Momentaufnahme; wiederholt aufrufbar (append-only Historie, `lies_gesundheit_aktuell`
zeigt den jüngsten Zustand je Komponente/Metrik). Für Dauerbetrieb schedulebar (analog Scraper-Watchdog).

Aufruf:  python3 betrieb_aufsicht_lauf.py [--ops <pfad>] [--scraper <pfad>]
"""
import argparse, os, sqlite3, sys, datetime, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "connectors"), os.path.join(HERE, "harness"), os.path.join(HERE, "modules"),
           os.path.join(HERE, "integration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import betrieb_aufsicht as S                                                    # noqa: E402


def _http_status(url, timeout=3):
    """(status, beleg): 'gruen' wenn erreichbar (2xx/3xx/4xx = Prozess lebt), 'rot' bei Verbindungsfehler."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return "gruen", f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return "gruen", f"HTTP {e.code} (Prozess antwortet)"
    except Exception as e:
        return "rot", f"nicht erreichbar ({str(e)[:60]})"


def bewerte_datenfrische(alter_h, laeuft):
    """Zwei-Achsen-Datenfrische (Jens 08.08.). `alter_h` = Stunden seit letzter Ingestion, `laeuft` = ist der
    Sammler-Prozess erreichbar. LÄUFT er, ist veraltete Ingestion eine DATEN-/Quellensache (Quellen erschöpft/
    rate-limitiert) -> max. `gelb`, Empfehlung `quellen_pruefen`, NIE `prozess_neustart` (der Prozess ist ja da).
    Läuft er NICHT, greift die schärfere Betriebs-Staffel (gruen<6h / gelb<48h / rot). -> (status, empfehlung)."""
    if laeuft:
        status = "gruen" if alter_h < 24 else "gelb"
        return status, ("ok" if status == "gruen" else "quellen_pruefen")
    status = "gruen" if alter_h < 6 else ("gelb" if alter_h < 48 else "rot")
    return status, ("ok" if status == "gruen" else "prozess_neustart")


def _datenluecken_tick(ops, jetzt, gic_map=None):
    """Prozess-unabhängige EOD-Cache-COVERAGE über das gemappte Universum (Präsenz-Proxy, quota-frei): wie viele
    der gemappten Symbole hat der Cache? Schreibt eine `datenluecken`-Zeile — so bleibt die Kachel auch OHNE
    Rechenlauf aktuell (der Wächter misst selbst; eine leere/gewischte Cache-DB fällt sofort auf). Der genaue,
    horizont-bewusste Report kommt weiterhin vom Rechenlauf (`outcome_eod_bereitschaft`); freshest gewinnt.
    Fail-safe: fehlt Map/Cache -> 0 (kein Tick), kippt den Aufsichts-Lauf nie. -> Anzahl geschriebener Zeilen."""
    try:
        import eod_cache
        # Breite Symbol->Kategorie-Map direkt aus den mitgelieferten Taxonomie-JSONs laden.
        import glob as _glob, json as _json
        _intdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "integration")
        kat_map = {}
        for _p in sorted(_glob.glob(os.path.join(_intdir, "retro_kat_map_gic_*.json"))):
            try:
                _d = _json.load(open(_p, encoding="utf-8"))
                kat_map.update(_d.get("map", _d) if isinstance(_d, dict) else {})
            except Exception:                                      # noqa: BLE001 — defekte Map ignorieren
                pass
        if not kat_map:
            return 0
        universum = set(kat_map)
        n = len(universum)
        gecacht = set(eod_cache.symbole()) & universum          # nur gemappte, im Cache präsente Symbole
        n_bereit = len(gecacht)
        n_fehlend = n - n_bereit
        ber = {"n": n, "n_bereit": n_bereit, "n_leer": 0, "n_delistet_kurz": 0, "n_fehlend": n_fehlend,
               "prozent": round(100.0 * n_bereit / n, 1) if n else 0.0}
        S.schreibe_datenluecken(ops, ber, jetzt)
        return 1
    except Exception:                                           # noqa: BLE001 — Coverage-Tick kippt die Aufsicht nie
        return 0


def lauf(ops_pfad, scraper_pfad, jetzt=None):
    """Ein Aufsichts-Tick: schreibt die aktuellen Gesundheits-Zustände in die ops-DB. -> Anzahl Zeilen."""
    jetzt = jetzt or datetime.datetime.now().isoformat(timespec="seconds")
    ops = sqlite3.connect(ops_pfad, timeout=30)
    S.schema_anlegen(ops)
    n = 0
    # 0) Prozess-Liveness ZUERST (roh, HTTP) — die Frische-Bewertung braucht sie (Jens 08.08.: „Prozess lebt"
    #    und „Daten frisch" sind ZWEI Achsen; ein laufender Sammler, der nur keine NEUEN Dokumente findet, darf
    #    keinen Prozess-Alarm ausloesen).
    liveness = {}
    for komp, url in (("scraper", "http://127.0.0.1:8000/"), ("ollama", "http://127.0.0.1:11434/api/tags")):
        status, beleg = _http_status(url)
        liveness[komp] = status
        S.schreibe_gesundheit(ops, komp, "technik", "prozess_erreichbar", status, jetzt,
                              empfehlung="ok" if status == "gruen" else "prozess_neustart", beleg=beleg)
        n += 1
    # 1) documents/facts-Kontrakte auf der ECHTEN scraper.db (F113, beobachtend)
    if os.path.exists(scraper_pfad):
        sc = sqlite3.connect("file:" + os.path.abspath(scraper_pfad).replace("\\", "/") + "?mode=ro",
                             uri=True, timeout=15)   # Windows-sicheres URI (Backslashes = ungültig)
        try:
            urteil = S.pruefe_dokument_kontrakte(sc)
            S.schreibe_gesundheit(ops, "scraper", urteil.get("kategorie", "technik"), "dokument_kontrakte",
                                  urteil["status"], jetzt, drift_art=urteil.get("drift_art", "keine"),
                                  empfehlung=urteil.get("empfehlung", "ok"), beleg=urteil.get("beleg"))
            n += 1
            # Datenfrische (NICHT Prozess-Alarm): Alter der jüngsten Ingestion. LÄUFT der Sammler (erreichbar),
            #   ist veraltete Ingestion eine DATEN-/Quellen-Sache (Quellen erschöpft/rate-limitiert) -> max. gelb,
            #   Empfehlung „quellen_pruefen", NIE „prozess_neustart". Ist der Sammler NICHT erreichbar, greift die
            #   schärfere Staffel (dann ist es wirklich ein Prozess-/Betriebs-Problem).
            row = sc.execute("SELECT MAX(ingested_at) FROM documents").fetchone()
            letzte = row[0] if row else None
            if letzte:
                try:
                    alter_h = (datetime.datetime.fromisoformat(jetzt)
                               - datetime.datetime.fromisoformat(str(letzte).replace(" ", "T"))).total_seconds() / 3600
                    laeuft = liveness.get("scraper") == "gruen"
                    fstatus, empf = bewerte_datenfrische(alter_h, laeuft)
                    if laeuft:
                        beleg = (f"letzte Ingestion {letzte} (vor {alter_h:.1f} h) — Sammler läuft; "
                                 f"{'keine neuen Dokumente (Quellen erschöpft/rate-limitiert?)' if fstatus=='gelb' else 'frisch'}")
                    else:
                        beleg = f"letzte Ingestion {letzte} (vor {alter_h:.1f} h) — Sammler NICHT erreichbar"
                    S.schreibe_gesundheit(ops, "scraper", "technik", "datenfrische_alter_h", fstatus, jetzt,
                                          wert_numerisch=round(alter_h, 1), schwelle=24 if laeuft else 6,
                                          empfehlung=empf, beleg=beleg)
                    n += 1
                except (ValueError, TypeError):
                    pass
        finally:
            sc.close()
    # 3) DATENLÜCKEN — prozess-UNABHÄNGIGE Coverage (Fable-MAJOR-1): der Wächter re-evaluiert die EOD-Cache-
    #    Abdeckung des gemappten Universums SELBST (quota-frei, reine Präsenz), damit die Datenlücken-Kachel nicht
    #    auf einer alten Selbstauskunft des Rechenlaufs „grün" stehenbleibt, nachdem ein Reclaim den Cache wischte.
    n += _datenluecken_tick(ops, jetzt)
    ops.commit()
    ampel = S.gesamt_ampel(ops)
    ops.close()
    return {"zeilen": n, "gesamt_ampel": ampel, "ops": ops_pfad, "jetzt": jetzt}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Schicht-S-Aufsicht: ein Tick -> ops-DB (Cockpit-Leitstand).")
    ap.add_argument("--ops", default=os.path.join(HERE, "aufsicht.db"), help="ops-DB (Default: System/aufsicht.db)")
    ap.add_argument("--scraper", default=os.path.join(HERE, "scraper.db"), help="scraper.db (Kontrakt-/Frische-Beobachtung)")
    args = ap.parse_args(argv)
    r = lauf(args.ops, args.scraper)
    print(f"Aufsicht-Tick: {r['zeilen']} Zustände -> {r['ops']} · Gesamt-Ampel: {r['gesamt_ampel']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
