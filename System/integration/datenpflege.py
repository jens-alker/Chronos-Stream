"""
datenpflege.py — asynchroner, dynamischer Datenauffrischer (Orchestrator: WANN wird was gezogen).

Feinkonzept `Feinkonzepte/Feinkonzept_Querschnitt_Datenhaltung_v1.md` — massgeblich §8
(FINALER BAU-SPEC v2, K1-K3/W4-W10/G8). Jens: „Datenbeschaffung muss asynchron erfolgen, jeweils wenn
neue Daten da sind … ich sehe nicht ein, 30.000 Abrufe zu machen, wenn es keine neuen Infos gibt."

**KEINE INSEL — alles Fetchen/Cachen delegiert an Bestehendes:**
  - Speicher: `fundamentals_cache` + `eod_cache` (per-Symbol gzip, atomar, TTL via `max_alter_tage`).
  - Abruf: `retro_kat_map_breit._fetch_full_fundamentals` (Voll-Dump, `TageslimitErreicht`-Semantik) +
    `eodhd_prices.fetch_eod_cached` (Voll-Refetch + Re-Cache) / `prefetch_eod_parallel` (Batch, mit dem
    hier gebauten stale-bewussten `ist_gecacht_fn`).
  - Universum: `fundamentals_backfill.lade_universum` (die gemappten Symbole aller Regionen).
  - Kalender: `eodhd_prices.fetch_earnings_kalender` (symbol+report_date-only, G8-Firewall).
Dieses Modul definiert KEINE zweite Cache-/Fetch-/Signal-Logik — nur die Faelligkeits-Orchestrierung.

**Mechanik (§8):**
  - **K1** Fundamentals-Refetch laeuft ueber den `fundamentals_cache.speichere`-Pfad (erzwungen bei
    Faelligkeit; `_erzwinge_refetch` spiegelt exakt den Miss-Zweig von `hole` — nie „cache=False", das
    speichert nicht). Ein realer Dump wird NIE mit einem `{}`-No-Data ueberschrieben (Delisting behaelt
    den letzten echten Stand).
  - **K2** Kurs-Refresh = VOLL-Refetch + Re-Cache (`fetch_eod_cached(to_date=heute, max_alter_tage=…)`);
    KEIN inkrementeller Bar-Merge (Split-Rueckwirkung mischt zwei `adjusted_close`-Basen). Dieselbe
    1 API-Einheit (EODHD berechnet je Call, nicht je Bar).
  - **K3** Faelligkeits-Zustand = persistierter Sidecar-Log `datenpflege_faelligkeit.json` (kleiner
    operativer Cursor, KEIN Bulk-Datencache — die Marktdaten liegen in der EINEN `markt_cache.db`; der
    Log ist inhaltsbasiert und damit restore-fest, unabhaengig von der Cache-`t_ingest`). Je Symbol
    `{letztes_filing, naechste_faelligkeit,
    fehlversuche, ruhe}`. **Erfolg = neues `filing_date` sichtbar** (nicht „Call gemacht"); kein neues
    Quartal -> wachsendes Backoff (+7/+14/+28 Tage), nach `max_fehlversuche` Zyklen Ruhezustand (nur noch
    kalender-getrieben) — schliesst das „ewig-faellig"-Quota-Leck (K3.1). `{}`-No-Data wird im
    Vorwaerts-Pfad per TTL re-gecheckt (K3.2: eine Neu-Notierung bekommt irgendwann Fundamentals).
  - **W4** Kalender liefert TIMING, Filing die WAHRHEIT: Refetch-Faelligkeit = `report_date +
    Filing-Latenz-Puffer` (empirisch je Symbol: `median(filing_date − Quartalsende)` aus den eigenen
    Dumps — dieselbe Datenbasis wie der Kadenz-Fallback).
  - **W6** Budget in EODHD-EINHEITEN (Fundamentals 10, EOD 1), `TageslimitErreicht` wiederverwendet
    (fail-fast, sauberer Abbruch), resumierbar (Zustand liegt im Log/Cache), fail-safe (`tick` kippt NIE
    den Aufrufer).
  - **W7** Kadenz-Schaetzer aus den ROHEN `filing_date`s ALLER Statements (nicht `zu_cashflow_quarterly`,
    das FCF-lose Quartale filtert); min. 3 Filings sonst Fallback ~100 Tage; Anker = max(filing_date)
    ueber `_parse_iso`-saubere Daten; Median robust gegen nicht-monotone Amendments.
  - **G8** Analysten-Firewall: vom Kalender werden AUSSCHLIESSLICH Symbol + Datum konsumiert
    (Quell-Scan-erzwungen, `test_datenpflege.TestAnalystenFirewall`).

**PIT unberuehrt:** der Auffrischer fuellt nur den Cache; Modul 9 filtert weiter `filing_date <= Stichtag`.
**Log-Persistenz (K3, ehrlich):** datenpflege ist HOME-primaer (laeuft neben dem Heim-Scraper) — dort haelt
die Platte den Sidecar-Log, K3.1 ist voll geschlossen. Der Log liegt im Cache-Wurzelverzeichnis und wird vom
bucket-basierten `fundamentals_drive.sync_hoch` NICHT mitgetragen (ein Fork der Sync-Mechanik fuer eine
Einzeldatei waere eine Insel). Auf einem Cloud-Reclaim geht daher NUR der Backoff-/Ruhe-Zustand verloren; jeder
Eintrag wird beim ersten Kontakt aus dem eigenen Dump re-initialisiert (`_init_eintrag`, NULL Extra-Call) —
Worst Case ist EIN Recheck je gestrandetem Symbol, danach greift der Backoff wieder. Das ist eine begrenzte,
selbst-heilende Degradation im Sekundaerpfad, NICHT das unbegrenzte „ewig-faellig"-Quota-Leck.
**Ehrlich offen (Schein-Test-Riegel):** der echte Dauerbetrieb (Live-EODHD, Scheduler-Kadenz) ist home-/quota-
gated; getestet ist die Mechanik (realdaten-nahe Schemata, W10-Naht-Tests). Nur Standardbibliothek.
"""
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = os.path.dirname(_HERE)
for _p in (os.path.join(_SYS, "connectors"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fundamentals_cache                                              # noqa: E402
import eod_cache                                                       # noqa: E402
from eodhd_prices import _parse_iso, _quarterly                        # noqa: E402

# W6: Budget in EODHD-EINHEITEN, nicht Symbolen (Fundamentals-Voll-Dump 10, EOD-Voll-History 1).
EINHEITEN_FUNDAMENTALS = 10
EINHEITEN_EOD = 1

LOG_DATEINAME = "datenpflege_faelligkeit.json"
_STATEMENT_BLOECKE = ("Balance_Sheet", "Cash_Flow", "Income_Statement")
_KADENZ_FALLBACK_TAGE = 100      # W7: < 3 Filings -> Quartal + Puffer
_LATENZ_FALLBACK_TAGE = 14       # W4: keine belastbare eigene Latenz-Historie -> konservativer Default
_LATENZ_CAP_TAGE = 90            # Ausreisser-Dumps deckeln (nie ein Jahr „Latenz")
_MAX_FEHLVERSUCHE = 4            # K3.1: +7/+14/+28, danach Ruhezustand (nur noch kalender-getrieben)
_TTL_NO_DATA_TAGE = 120          # K3.2: {}-No-Data-Recheck-Kadenz (Neu-Notierung bekommt irgendwann Daten)
_KURS_STALE_TTL_TAGE = 7         # K2: Montagsschluss-Frische (Default-Policy, Feinkonzept §7)


# ------------------------------------------------------------------ #
# Kadenz-/Latenz-Schaetzer (W7/W4) — rein, aus dem eigenen Voll-Dump, null Extra-Call
# ------------------------------------------------------------------ #
def rohe_filing_dates(fundamentals):
    """W7: ALLE sauber parsebaren `filing_date`s der drei quarterly-Statement-Bloecke (Balance_Sheet/
    Cash_Flow/Income_Statement — NICHT `zu_cashflow_quarterly`, das FCF-lose Quartale filtert).
    Dedupliziert + sortiert (-> Median-Kadenz robust gegen nicht-monotone Amendments). -> [date, ...]."""
    daten = set()
    for block in _STATEMENT_BLOECKE:
        q = _quarterly(fundamentals if isinstance(fundamentals, dict) else {}, block)
        for _datum, z in (q.items() if isinstance(q, dict) else []):
            if isinstance(z, dict):
                d = _parse_iso(z.get("filing_date"))
                if d is not None:
                    daten.add(d)
    return sorted(daten)


def letztes_filing(fundamentals):
    """W7-Anker: `max(filing_date)` ueber die `_parse_iso`-sauberen Daten aller Statements. ISO oder None."""
    daten = rohe_filing_dates(fundamentals)
    return daten[-1].isoformat() if daten else None


def kadenz_tage(fundamentals, fallback=_KADENZ_FALLBACK_TAGE):
    """W7: firmen-eigene Melderhythmik = Median der positiven Abstaende aufeinanderfolgender (distinkter)
    filing_dates. < 3 Filings ODER keine positiven Abstaende -> `fallback` (~100 Tage = Quartal+Puffer)."""
    daten = rohe_filing_dates(fundamentals)
    if len(daten) < 3:
        return fallback
    gaps = sorted((b - a).days for a, b in zip(daten, daten[1:]) if (b - a).days > 0)
    if not gaps:
        return fallback
    return gaps[len(gaps) // 2]


def filing_latenz_tage(fundamentals, fallback=_LATENZ_FALLBACK_TAGE, cap=_LATENZ_CAP_TAGE):
    """W4: empirischer Filing-Latenz-Puffer je Symbol = `median(filing_date − Quartalsende)` ueber alle
    Statements mit beiden sauber parsebaren Daten (dieselbe Datenbasis wie der Kadenz-Fallback). Der
    Kalender meldet die ANKUENDIGUNG (`report_date`); das 10-Q/K-`filing_date` + EODHD-Ingest kommt
    Tage-Wochen spaeter -> erst `report_date + Latenz` ist ein Refetch sinnvoll faellig.
    < 3 Stichproben -> `fallback`; geklammert auf [1, cap] (degenerierte Dumps deckeln)."""
    diffs = []
    for block in _STATEMENT_BLOECKE:
        q = _quarterly(fundamentals if isinstance(fundamentals, dict) else {}, block)
        for datum, z in (q.items() if isinstance(q, dict) else []):
            if not isinstance(z, dict):
                continue
            fil, qe = _parse_iso(z.get("filing_date")), _parse_iso(z.get("date", datum))
            if fil is not None and qe is not None and fil >= qe:
                diffs.append((fil - qe).days)
    if len(diffs) < 3:
        return fallback
    diffs.sort()
    return max(1, min(cap, diffs[len(diffs) // 2]))


# ------------------------------------------------------------------ #
# K3: der persistierte Faelligkeits-Log (Sidecar, inhaltsbasiert = restore-fest, NICHT Prozess-RAM)
# ------------------------------------------------------------------ #
def _log_pfad(cache_dir=None):
    return os.path.join(cache_dir or fundamentals_cache._CACHE_DIR, LOG_DATEINAME)


def lade_log(cache_dir=None):
    """Den Faelligkeits-Log laden -> {symbol: {letztes_filing, naechste_faelligkeit, fehlversuche, ruhe}}.
    Fehlt/korrupt -> {} (der Log ist aus den Dumps re-initialisierbar — `fundamentals_refresh` baut je
    Symbol beim ersten Kontakt einen Eintrag, OHNE Abruf; nur der Backoff-Zustand ginge verloren)."""
    p = _log_pfad(cache_dir)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    sym = d.get("symbole") if isinstance(d, dict) else None
    return sym if isinstance(sym, dict) else {}


def speichere_log(log, cache_dir=None):
    """Log atomar persistieren (tmp + os.replace — kein halber Log bei Abbruch/Reclaim). K3:
    Datenhaltung auf Platte (Dreifach-Verankerung: nie nur Prozess-RAM), inhaltsbasiert = restore-fest."""
    p = _log_pfad(cache_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "symbole": log}, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)


def ist_faellig(eintrag, jetzt, kalender_datum=None, latenz_tage=_LATENZ_FALLBACK_TAGE):
    """REIN: ist ein Symbol refetch-faellig? `eintrag`: Log-Zeile (oder None), `jetzt`/`kalender_datum`: ISO.
    - **Kalender-Pfad (W4, weckt auch `ruhe`):** `report_date + latenz_tage <= jetzt` UND das report_date
      liegt NACH dem letzten gesehenen Filing (sonst ist die Meldung schon im Dump).
    - **Kadenz-Pfad (K3):** `naechste_faelligkeit <= jetzt` — ausser im Ruhezustand (nur Kalender weckt).
    Kein Eintrag + kein Kalender -> False (der Aufrufer initialisiert erst aus dem Dump)."""
    jetzt_d = _parse_iso(jetzt)
    if jetzt_d is None:
        return False
    e = eintrag or {}
    kd = _parse_iso(kalender_datum) if kalender_datum else None
    if kd is not None:
        lf = _parse_iso(e.get("letztes_filing"))
        if (kd + datetime.timedelta(days=int(latenz_tage))) <= jetzt_d and (lf is None or kd > lf):
            return True
    if not eintrag or e.get("ruhe"):
        return False
    nf = _parse_iso(e.get("naechste_faelligkeit"))
    return nf is not None and nf <= jetzt_d


def _init_eintrag(dump):
    """Log-Eintrag aus einem vorhandenen realen Dump initialisieren (NULL Extra-Call): Anker = letztes
    Filing, naechste Faelligkeit = Anker + firmen-eigene Kadenz. Dump ohne parsebares Filing ->
    kalender-only (naechste_faelligkeit None, kein Kadenz-Anker)."""
    lf = letztes_filing(dump)
    if lf is None:
        return {"letztes_filing": None, "naechste_faelligkeit": None, "fehlversuche": 0, "ruhe": False}
    nf = (_parse_iso(lf) + datetime.timedelta(days=kadenz_tage(dump))).isoformat()
    return {"letztes_filing": lf, "naechste_faelligkeit": nf, "fehlversuche": 0, "ruhe": False}


def _erzwinge_refetch(symbol, fetch_fn, cache_dir=None, alt_leer=False):
    """K1: erzwungener Refetch ueber DENSELBEN Speicher-Pfad wie `fundamentals_cache.hole` bei Miss
    (fetch -> `speichere`; harte Fehler propagieren, werden NICHT gecacht). Zusatz-Riegel: ein REALER
    Dump wird NIE mit einem `{}`-No-Data ueberschrieben (`alt_leer=False` -> {} wird verworfen, der letzte
    echte Stand bleibt; Delisting/Melde-Verzug laeuft ueber den Backoff, nicht ueber Datenverlust)."""
    data = fetch_fn(symbol)
    if isinstance(data, (dict, list)) and (data or alt_leer):
        fundamentals_cache.speichere(symbol, data, cache_dir or fundamentals_cache._CACHE_DIR)
    return data


def _default_fund_fetch():
    """Der geteilte Voll-Dump-Fetcher (KEINE INSEL: `retro_kat_map_breit._fetch_full_fundamentals` mit
    Tageslimit-/Backoff-/No-Data-Semantik — dieselbe Naht wie Klassifikation + Backfill + Modul 9)."""
    from retro_kat_map_breit import _fetch_full_fundamentals
    return _fetch_full_fundamentals


def _tageslimit_klasse():
    """Die EINE Quota-Fail-fast-Exception (W6: wiederverwendet, keine zweite Fehlertaxonomie)."""
    from retro_kat_map_breit import TageslimitErreicht
    return TageslimitErreicht


def _ist_tageslimit_text(exc):
    """EOD-Pfad: `_curl_json` wirft bei der Tageslimit-Textantwort einen generischen RuntimeError —
    an der EODHD-Signatur erkennen (dieselbe Erkennung wie `_fetch_full_fundamentals`)."""
    return "daily api requests limit" in str(exc).lower()


# ------------------------------------------------------------------ #
# Fundamentals-Auffrischer (K1/K3/W4/W7) — dynamisch je Firma, kein Blind-Abruf
# ------------------------------------------------------------------ #
_CHECKPOINT_JEDE = 25            # Fable-M9: Log alle N verarbeiteten Symbole atomar sichern (Kill mitten im Lauf)


def fundamentals_refresh(symbole, einheiten_budget, jetzt=None, log=None, kalender_map=None,
                         fetch_fn=None, cache_dir=None, ttl_no_data_tage=_TTL_NO_DATA_TAGE,
                         max_fehlversuche=_MAX_FEHLVERSUCHE, control_fn=None, melde_fn=None):
    """Die faelligen Fundamentals-Dumps auffrischen. `symbole`: das gemappte Universum (sortiert
    verarbeitet, deterministisch). `einheiten_budget`: EODHD-Einheiten (Fundamentals = 10/Symbol, W6).
    `jetzt`: ISO (injizierbar). `log`: der Faelligkeits-Log (wird IN PLACE aktualisiert; None -> laden).
    `kalender_map`: {symbol: report_date} (aus `fetch_earnings_kalender`; G8: mehr traegt er nicht).
    `fetch_fn(symbol)`: injizierbar (Default = geteilter `_fetch_full_fundamentals`); DARF
    `TageslimitErreicht` werfen -> fail-fast.

    Nicht-gecachte Symbole werden UEBERSPRUNGEN (Erst-Load = `fundamentals_backfill`, keine zweite
    Definition). Erfolg = neues filing_date sichtbar; sonst Backoff/Ruhe (K3). Resumierbar: der Zustand
    liegt im Log + Cache — ein Budget-/Limit-Abbruch macht morgen genau da weiter.
    -> (bericht, log). bericht: Zaehler + einheiten_verbraucht/rest_einheiten + tageslimit_erreicht."""
    jetzt = jetzt or datetime.date.today().isoformat()
    jetzt_d = _parse_iso(jetzt)
    log = lade_log(cache_dir) if log is None else log
    kalender_map = kalender_map or {}
    fetch_fn = fetch_fn or _default_fund_fetch()
    TageslimitErreicht = _tageslimit_klasse()
    cd = cache_dir or fundamentals_cache._CACHE_DIR
    b = {"n_symbole": len(symbole), "n_faellig": 0, "n_refetcht": 0, "n_erfolg": 0, "n_backoff": 0,
         "n_ruhe_neu": 0, "n_no_data_recheck": 0, "n_log_init": 0, "n_uebersprungen_ungecacht": 0,
         "n_fehler": 0, "tageslimit_erreicht": False, "budget_erschoepft": False, "abgebrochen": False,
         "einheiten_verbraucht": 0}
    if jetzt_d is None:
        b["rest_einheiten"] = einheiten_budget
        return b, log

    syms = sorted(symbole)
    n_todo = len(syms)
    for _i, sym in enumerate(syms):
        # F131 verlustfreier Abbruch (Fable-M6): am Checkpoint VOR dem nächsten Symbol den Steuer-Wunsch
        # prüfen; alles != "run" -> Backoff-Log SICHERN (sonst verbrennt ein Kill in der Pause die Fetches)
        # und sauber abbrechen. Der State liegt schon persistent (Cache atomar), das Log jetzt auch.
        if control_fn is not None and control_fn() != "run":
            speichere_log(log, cd)
            b["abgebrochen"] = True
            break
        # Fable-M7: Herzschlag + Fortschritt melden (sonst gilt ein 4h-Grind als „tot", und die ETA bleibt None).
        # Fable-M9: periodischer Log-Checkpoint gegen den Kill MITTEN im Lauf (Schlaf/Shutdown/Reclaim).
        if _i and _i % _CHECKPOINT_JEDE == 0:
            speichere_log(log, cd)
            if melde_fn is not None:
                melde_fn(phase="fundamentals", aktuell=_i, gesamt=n_todo)
        dump = fundamentals_cache.lade(sym, cd)
        if dump is None:                                  # nie gecacht / korrupt -> Backfill-Territorium
            b["n_uebersprungen_ungecacht"] += 1
            continue

        # --- {}-No-Data (K3.2): TTL-Recheck (mtime) ODER Kalender-Weckruf ---------------------------
        if isinstance(dump, dict) and not dump:
            ttl_abgelaufen = fundamentals_cache.lade(sym, cd, max_alter_tage=ttl_no_data_tage) is None
            kal_faellig = ist_faellig({}, jetzt, kalender_map.get(sym))
            if not (ttl_abgelaufen or kal_faellig):
                continue
            b["n_faellig"] += 1
            if einheiten_budget - b["einheiten_verbraucht"] < EINHEITEN_FUNDAMENTALS:
                b["budget_erschoepft"] = True
                break
            b["einheiten_verbraucht"] += EINHEITEN_FUNDAMENTALS
            try:
                neu = _erzwinge_refetch(sym, fetch_fn, cd, alt_leer=True)   # {} darf {} bleiben (TTL neu)
            except TageslimitErreicht:
                b["tageslimit_erreicht"] = True
                break
            except Exception:                             # noqa: BLE001 — transient: bleibt faellig, Retry
                b["n_fehler"] += 1
                continue
            b["n_refetcht"] += 1
            b["n_no_data_recheck"] += 1
            if isinstance(neu, dict) and neu:             # Neu-Notierung hat jetzt Daten -> Log-Anker
                log[sym] = _init_eintrag(neu)
                b["n_erfolg"] += 1
                b["n_log_init"] += 1
            continue

        # --- realer Dump: Log-Eintrag sicherstellen (NULL Extra-Call), dann Faelligkeit ------------
        eintrag = log.get(sym)
        if eintrag is None:
            eintrag = _init_eintrag(dump)
            log[sym] = eintrag
            b["n_log_init"] += 1
        latenz = filing_latenz_tage(dump)
        if not ist_faellig(eintrag, jetzt, kalender_map.get(sym), latenz_tage=latenz):
            continue
        b["n_faellig"] += 1
        if einheiten_budget - b["einheiten_verbraucht"] < EINHEITEN_FUNDAMENTALS:
            b["budget_erschoepft"] = True
            break
        b["einheiten_verbraucht"] += EINHEITEN_FUNDAMENTALS
        try:
            neu = _erzwinge_refetch(sym, fetch_fn, cd, alt_leer=False)      # realer Dump nie -> {}
        except TageslimitErreicht:
            b["tageslimit_erreicht"] = True
            break
        except Exception:                                 # noqa: BLE001 — transient: Log unveraendert (Retry)
            b["n_fehler"] += 1
            continue
        b["n_refetcht"] += 1
        neu_lf = letztes_filing(neu if isinstance(neu, dict) else {})
        alt_lf = eintrag.get("letztes_filing")
        if neu_lf and (alt_lf is None or neu_lf > alt_lf):
            # ERFOLG (K3): neues Filing sichtbar -> Anker + Kadenz neu, Backoff/Ruhe zurueckgesetzt.
            nf = (_parse_iso(neu_lf) + datetime.timedelta(days=kadenz_tage(neu))).isoformat()
            eintrag.update({"letztes_filing": neu_lf, "naechste_faelligkeit": nf,
                            "fehlversuche": 0, "ruhe": False})
            b["n_erfolg"] += 1
        else:
            # KEIN neues Quartal (Melde-Verzug/eingestellt/delistet): wachsendes Backoff, dann Ruhe (K3.1).
            fv = int(eintrag.get("fehlversuche", 0)) + 1
            if fv >= max_fehlversuche:
                eintrag.update({"fehlversuche": fv, "ruhe": True, "naechste_faelligkeit": None})
                b["n_ruhe_neu"] += 1
            else:
                backoff = 7 * (2 ** (fv - 1))             # +7, +14, +28 Tage
                nf = (jetzt_d + datetime.timedelta(days=backoff)).isoformat()
                eintrag.update({"fehlversuche": fv, "naechste_faelligkeit": nf})
                b["n_backoff"] += 1
    b["rest_einheiten"] = einheiten_budget - b["einheiten_verbraucht"]
    return b, log


# ------------------------------------------------------------------ #
# Kurs-Auffrischer (K2) — Voll-Refetch + Re-Cache, stale-bewusst, NIE Bar-Merge
# ------------------------------------------------------------------ #
def letzter_montag(jetzt):
    """Der juengste Montag <= jetzt (ISO -> date; Montagsschluss-Frische-Policy, Feinkonzept §7).
    Unparsebares jetzt -> None (fail-closed: nichts gilt als frisch)."""
    d = _parse_iso(jetzt)
    return d - datetime.timedelta(days=d.weekday()) if d is not None else None


def eod_ist_frisch(symbol, jetzt=None, cache_dir=None, stale_ttl_tage=_KURS_STALE_TTL_TAGE):
    """Stale-bewusster `ist_gecacht`-Ersatz (K2) — direkt in `prefetch_eod_parallel(ist_gecacht_fn=…)`
    injizierbar (True = ueberspringen, False = Voll-Refetch). Frisch, wenn
      (a) der juengste gecachte Bar >= letzter Montag liegt (die Serie deckt die Frische-Policy), ODER
      (b) die Cache-Datei juengst geschrieben wurde (mtime <= `stale_ttl_tage`): ein frisch voll-
          refetchtes, aber INAKTIVES/delistetes Symbol waechst nie ueber seinen letzten Print hinaus —
          ohne (b) wuerde es JEDE Woche erneut gezogen (das K3.1-Quota-Leck auf der Kurs-Seite).
    `[]`-No-Data gilt als frisch (Symbol ohne EOD-Historie; Marker-Politik des `eod_cache`).
    Nicht gecacht -> False (Erst-Load faellig)."""
    cd = cache_dir or eod_cache._CACHE_DIR
    rows = eod_cache.lade(symbol, cd)
    if rows is None:
        return False
    if not rows:                                          # [] = genuines No-Data (Marker) -> nicht anfassen
        return True
    montag = letzter_montag(jetzt or datetime.date.today().isoformat())
    if montag is None:
        return False
    letzter = max(((r.get("date") or "")[:10] for r in rows if isinstance(r, dict) and r.get("date")),
                  default="")
    if letzter and _parse_iso(letzter) is not None and _parse_iso(letzter) >= montag:
        return True
    # Serie endet vor dem letzten Montag: nur frisch, wenn eben erst voll-refetcht (inaktiv/delistet).
    # Frische aus `t_ingest` der Cache-DB (07.08.: DB statt Datei-mtime).
    alter_tage = fundamentals_cache.frische_tage(symbol, cd)
    return alter_tage is not None and alter_tage <= stale_ttl_tage


def kurs_refresh(symbole, einheiten_budget, jetzt=None, fetch_fn=None, cache_dir=None,
                 stale_ttl_tage=_KURS_STALE_TTL_TAGE, control_fn=None, melde_fn=None):
    """K2: die stalen EOD-Serien VOLL neu ziehen + re-cachen (1 Einheit/Symbol; kein Bar-Merge —
    `fetch_eod_cached` mit abgelaufener TTL fetcht die volle History und `speichere`t sie als GANZES,
    die alte Serie wird ERSETZT). `fetch_fn(symbol)` injizierbar; Default =
    `fetch_eod_cached(sym, to_date=jetzt, max_alter_tage=stale_ttl_tage, voll_wenn_unvollstaendig=True)`.
    Fail-fast bei Tageslimit (EODHD-Signatur im RuntimeError), resumierbar (Cache = Zustand).
    -> Bericht-dict."""
    jetzt = jetzt or datetime.date.today().isoformat()
    cd = cache_dir or eod_cache._CACHE_DIR
    if fetch_fn is None:
        from eodhd_prices import fetch_eod_cached

        def fetch_fn(s):
            return fetch_eod_cached(s, to_date=jetzt, max_alter_tage=stale_ttl_tage,
                                    voll_wenn_unvollstaendig=True)
    b = {"n_symbole": len(symbole), "n_stale": 0, "n_refetcht": 0, "n_fehler": 0,
         "tageslimit_erreicht": False, "budget_erschoepft": False, "abgebrochen": False,
         "einheiten_verbraucht": 0}
    todo = [s for s in sorted(symbole) if not eod_ist_frisch(s, jetzt, cd, stale_ttl_tage)]
    b["n_stale"] = len(todo)
    TageslimitErreicht = _tageslimit_klasse()
    for _i, sym in enumerate(todo):
        if control_fn is not None and control_fn() != "run":   # F131: verlustfreier Abbruch (Cache = State)
            b["abgebrochen"] = True
            break
        if _i and _i % _CHECKPOINT_JEDE == 0 and melde_fn is not None:   # Fable-M7: Herzschlag/Fortschritt
            melde_fn(phase="kurse", aktuell=_i, gesamt=len(todo))
        if einheiten_budget - b["einheiten_verbraucht"] < EINHEITEN_EOD:
            b["budget_erschoepft"] = True
            break
        b["einheiten_verbraucht"] += EINHEITEN_EOD
        try:
            fetch_fn(sym)
        except TageslimitErreicht:
            b["tageslimit_erreicht"] = True
            break
        except RuntimeError as e:
            if _ist_tageslimit_text(e):
                b["tageslimit_erreicht"] = True
                break
            b["n_fehler"] += 1
            continue
        except Exception:                                 # noqa: BLE001 — transient: Symbol bleibt stale (Retry)
            b["n_fehler"] += 1
            continue
        b["n_refetcht"] += 1
    b["rest_einheiten"] = einheiten_budget - b["einheiten_verbraucht"]
    return b


# ------------------------------------------------------------------ #
# tick — der kontinuierliche Einstieg (Sibling der Scraper-Schleife; fail-safe)
# ------------------------------------------------------------------ #
def tick(einheiten_budget, jetzt=None, symbole=None, kalender=None,
         fund_fetch_fn=None, eod_fetch_fn=None, cache_dir=None, eod_cache_dir=None,
         ttl_no_data_tage=_TTL_NO_DATA_TAGE, max_fehlversuche=_MAX_FEHLVERSUCHE,
         kurs_stale_ttl_tage=_KURS_STALE_TTL_TAGE, log_speichern=True, control_fn=None, melde_fn=None):
    """EIN Budget-Haeppchen Datenpflege: erst der ereignisgetriebene Fundamentals-Refresh (10 Einheiten/
    Symbol, der knappere Pfad), dann der Kurs-Refresh mit dem Rest-Budget (1 Einheit/Symbol). `jetzt`/
    Fetches injizierbar (offline testbar). `symbole`: Default = das gemappte Universum aller Regionen
    (`fundamentals_backfill.lade_universum`, KEINE zweite Universums-Definition). `kalender`: Zeilen aus
    `fetch_earnings_kalender` ({symbol, report_date}); None = kein Kalender (Kadenz-Fallback traegt).

    **Fail-safe (Feinkonzept §4):** tick kippt NIE den Aufrufer — jeder Fehler (auch ein fataler) wird
    als Bericht zurueckgegeben, nie geworfen. **Fail-fast (W6):** `TageslimitErreicht` beendet den Tick
    sauber (Bericht `tageslimit_erreicht=True`); resumierbar via Log/Cache. -> Bericht-dict."""
    try:
        jetzt = jetzt or datetime.date.today().isoformat()
        if symbole is None:
            from fundamentals_backfill import lade_universum
            symbole = sorted(lade_universum())
        kalender_map = {}
        for z in kalender or []:
            if isinstance(z, dict) and z.get("symbol") and z.get("report_date"):
                alt = kalender_map.get(z["symbol"])
                if alt is None or z["report_date"] > alt:     # juengstes report_date je Symbol
                    kalender_map[z["symbol"]] = z["report_date"]
        log = lade_log(cache_dir)
        fb, log = fundamentals_refresh(symbole, einheiten_budget, jetzt=jetzt, log=log,
                                       kalender_map=kalender_map, fetch_fn=fund_fetch_fn,
                                       cache_dir=cache_dir, ttl_no_data_tage=ttl_no_data_tage,
                                       max_fehlversuche=max_fehlversuche, control_fn=control_fn,
                                       melde_fn=melde_fn)
        if log_speichern:
            speichere_log(log, cache_dir)
        kb = {"n_symbole": 0, "n_stale": 0, "n_refetcht": 0, "n_fehler": 0, "tageslimit_erreicht": False,
              "budget_erschoepft": False, "abgebrochen": False, "einheiten_verbraucht": 0,
              "rest_einheiten": fb["rest_einheiten"]}
        # F131: ein Abbruch im Fundamentals-Teil stoppt auch den Kurs-Teil (Stop gilt fuer den ganzen Tick).
        if not fb["tageslimit_erreicht"] and not fb["abgebrochen"] and fb["rest_einheiten"] >= EINHEITEN_EOD:
            kb = kurs_refresh(symbole, fb["rest_einheiten"], jetzt=jetzt, fetch_fn=eod_fetch_fn,
                              cache_dir=eod_cache_dir, stale_ttl_tage=kurs_stale_ttl_tage,
                              control_fn=control_fn, melde_fn=melde_fn)
        return {"ok": True, "jetzt": jetzt, "einheiten_budget": einheiten_budget,
                "einheiten_verbraucht": fb["einheiten_verbraucht"] + kb["einheiten_verbraucht"],
                "tageslimit_erreicht": fb["tageslimit_erreicht"] or kb["tageslimit_erreicht"],
                "abgebrochen": fb.get("abgebrochen") or kb.get("abgebrochen"),
                "fundamentals": fb, "kurse": kb}
    except Exception as e:                                # noqa: BLE001 — fail-safe: kippt NIE den Aufrufer
        return {"ok": False, "fehler_fatal": f"{type(e).__name__}: {e}",
                "einheiten_budget": einheiten_budget, "einheiten_verbraucht": None,
                "tageslimit_erreicht": False, "fundamentals": {}, "kurse": {}}


def main():
    """Diagnose ohne Abruf (offline): Log-/Cache-Stand + wie viele Symbole heute faellig waeren.
    Der echte Live-Tick laeuft ueber den Scheduler/`tick` (quota-/home-gated, Schein-Test-Riegel)."""
    print("=== Datenpflege (asynchroner Auffrischer) — Diagnose ===")
    try:
        from fundamentals_backfill import lade_universum
        uni = sorted(lade_universum())
        log = lade_log()
        jetzt = datetime.date.today().isoformat()
        n_gecacht = sum(1 for s in uni if fundamentals_cache.ist_gecacht(s))
        n_faellig = sum(1 for s in uni if s in log and ist_faellig(log.get(s), jetzt))
        n_ruhe = sum(1 for e in log.values() if e.get("ruhe"))
        print(f"  Universum: {len(uni)} gemappt | {n_gecacht} gecacht | Log: {len(log)} Eintraege "
              f"({n_ruhe} in Ruhe) | heute kadenz-faellig: {n_faellig}")
        print("  (Live-Tick: datenpflege.tick(einheiten_budget) — quota-gated, faellt nie auf den Aufrufer.)")
    except Exception as e:                                # noqa: BLE001
        print(f"  (Diagnose uebersprungen: {e})")


if __name__ == "__main__":
    main()
