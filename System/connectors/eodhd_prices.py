"""
eodhd_prices.py — Konnektor: EODHD EOD-Kurse -> Modul-9 (preis_roh) + Modul-8 (Outcome-Return).

Die echte Kurs-Datenklasse (ETFs/Aktien, US+EU). `fetch_eod` ist LIVE lauffähig (eodhd.com ist über den
Agent-Proxy erreichbar). Der Key liefert die **vollen Daten**: tiefe Historie, delistete Namen,
survivorship-freies Universum — keine Tier-Einschränkung.

Survivorship-freier Retro: `fetch_symbol_list(exchange, delisted=True)` zieht die INAKTIVEN (delisteten)
Namen (`delisted=1`), `symbol_universum(aktiv, delisted)` mischt beide zum survivorship-freien Universum
(delisted-Flag), und `fetch_eod` holt die volle Kurshistorie AUCH für delistete Symbole
(`{Code}.{Exchange}`). Das ist die survivorship-freie Datenbasis für die Kohorten-/Outcome-Konstruktion der Analyse-Schicht.

Abbildung: EOD-Zeile {date, close, adjusted_close, ...} -> preis_roh. Preise werden NICHT
revidiert -> t_event = t_disclosed = Handelstag (vintage ≈ ref_date, 3.6); t_ingest = Abrufzeit.
`adjusted_close` (split-/dividendenbereinigt) ist der Default. Nur Standardbibliothek.

Key-Reihenfolge: arg -> $EODHD_API_KEY -> ~/.config/mtf-qs/eodhd.key.
F101: EODHD = die survivorship-freie Retro-Quelle (Modul 8); `eod_returns_universum` baut die Kohorten-
Returns. F102 (entschieden a): `verdichte_kategorie` = gleichgewichtetes Mittel je Kategorie (sicherste
Vergleichsbasis, delistete Namen inklusive).
"""
import json
import os
import subprocess
from pathlib import Path

API_ROOT = "https://eodhd.com/api"
BASE = f"{API_ROOT}/eod"
KEYFILE = os.path.expanduser("~/.config/mtf-qs/eodhd.key")


def _token(arg_token=None):
    if arg_token:
        raw = arg_token
    elif os.environ.get("EODHD_API_KEY"):
        raw = os.environ["EODHD_API_KEY"]
    elif os.path.exists(KEYFILE):
        raw = Path(KEYFILE).read_text()
    else:
        raise RuntimeError(f"Kein EODHD-Key ($EODHD_API_KEY / {KEYFILE} / arg).")
    # `.strip("<>")` toleriert ein Paste-Artefakt <…key…> (Secret-Injektion mit Winkelklammern):
    # roh so gesetzt liefert sonst 401. EODHD-Keys sind alnum + '.', enthalten nie < oder >,
    # daher ist das Entfernen führender/abschließender Klammern verlustfrei.
    tok = raw.strip().strip("<>").strip()
    if not tok:
        raise RuntimeError("EODHD-Key ist leer (nur Whitespace/<>?).")
    return tok


def zu_preis_roh(eod_rows, instrument_id, art, t_ingest, feld="adjusted_close"):
    """EOD-Zeilen -> Modul-9 preis_roh. `feld`: adjusted_close (Default, bereinigt) oder close.
    Preise nicht revidiert -> t_event = t_disclosed = Handelstag (vintage ≈ ref_date)."""
    out = []
    for r in eod_rows:
        wert = r.get(feld)
        if wert is None:
            wert = r.get("close")
        if wert is None or not r.get("date"):
            continue
        d = r["date"][:10]
        out.append({
            "instrument_id": instrument_id, "art": art,
            "wert_numerisch": float(wert),
            "t_event": d, "t_disclosed": d, "t_ingest": t_ingest,
        })
    return out


def zu_return(eod_rows, feld="adjusted_close"):
    """Gesamt-Return über die (chronologische) Reihe = letzter/erster − 1 (für Modul-8-Outcome).
    None bei < 2 gültigen Punkten oder Startwert 0."""
    werte = []
    for r in sorted(eod_rows, key=lambda x: x.get("date", "")):
        w = r.get(feld)
        if w is None:
            w = r.get("close")
        if w is not None:
            werte.append(float(w))
    if len(werte) < 2 or werte[0] == 0:
        return None
    return werte[-1] / werte[0] - 1.0


def _curl_json(url, timeout):
    """GET url per curl -> geparstes JSON (list|dict). Fehlerklar (fail-loud)."""
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                         capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"EODHD nicht erreichbar: rc={out.returncode} {out.stderr[:200]}")
    body = out.stdout.strip()
    if body in ("Forbidden", "Unauthorized") or body.startswith("Forbidden"):
        raise RuntimeError(f"EODHD-Zugriff verweigert (Key?): {body[:120]}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"EODHD: keine JSON-Antwort: {body[:200]}")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"EODHD-Fehler: {data['error']}")
    return data


def fetch_eod(symbol, from_date=None, to_date=None, api_token=None, period="d", timeout=30):
    """LIVE: EODHD EOD-Historie für ein Symbol (z. B. 'AAPL.US', 'IWDA.LSE'). -> Liste EOD-Zeilen.
    Funktioniert AUCH für delistete Symbole (volle Historie). `period`: d/w/m."""
    url = f"{BASE}/{symbol}?api_token={_token(api_token)}&fmt=json&period={period}"
    if from_date:
        url += f"&from={from_date}"
    if to_date:
        url += f"&to={to_date}"
    data = _curl_json(url, timeout)
    return data if isinstance(data, list) else []


def _eod_slice(rows, from_date=None, to_date=None):
    """REIN (offline testbar): aus einer EOD-Voll-History die Zeilen im Fenster [from_date, to_date] (inklusive,
    ISO-Strings 'YYYY-MM-DD'; None = offen). Datum-String-Vergleich (ISO ist lexikografisch = chronologisch).
    Zeilen ohne `date` fallen raus. Reihenfolge bleibt erhalten."""
    out = []
    for r in rows:
        d = (r.get("date") or "")[:10] if isinstance(r, dict) else ""
        if not d:
            continue
        if from_date and d < from_date[:10]:
            continue
        if to_date and d > to_date[:10]:
            continue
        out.append(r)
    return out


def _fetch_eod_full(symbol, api_token=None, timeout=30):
    """LIVE: die VOLLE tägliche EOD-History eines Symbols (kein from/to) = 1 API-Einheit, für ALLE Fenster
    wiederverwendbar. Delistet-fest. **STRIKT (QS-Gemini-B3):** nur eine echte JSON-**Liste** (auch leer =
    genuines No-Data) wird zurückgegeben und darf gecacht werden. `_curl_json` wirft bereits bei rc≠0/Forbidden/
    error-Objekt/Nicht-JSON; eine unerwartete **Nicht-Listen**-Antwort (z. B. ein `{"message":"rate limit"}`-Dict
    ohne `error`-Key) wirft hier → wird NICHT als falscher `[]`-No-Data-Marker gecacht (Retry nächster Lauf)."""
    url = f"{BASE}/{symbol}?api_token={_token(api_token)}&fmt=json&period=d"
    data = _curl_json(url, timeout)                      # fail-loud auf rc/Forbidden/error-Objekt/Nicht-JSON
    if isinstance(data, list):
        return data                                      # inkl. [] = genuines No-Data (200 + leere Liste)
    raise RuntimeError(f"EODHD-EOD: unerwartete Nicht-Listen-Antwort für {symbol}: {str(data)[:80]}")


def fetch_eod_cached(symbol, from_date=None, to_date=None, api_token=None, timeout=30,
                     max_alter_tage=None, voll_wenn_unvollstaendig=False, cache_only=False):
    """Cache-first (Jens 29.07.): die VOLLE tägliche EOD-History wird EINMAL gezogen + im `eod_cache` (→ Drive-DB)
    persistiert; jedes Fenster wird lokal geschnitten (`_eod_slice`). Ein Symbol kostet je Lauf höchstens EINEN
    Call, alle Stichtage/Fenster teilen denselben Cache → die Outcome-Seite der Retro/Ablation ist reclaim-fest
    und bei Wiederholung quota-frei. Nur `period='d'` (die Outcome-Seite ist täglich). `max_alter_tage`: TTL für
    den Live-Frischebedarf (Retro: None = historische Bars ändern sich nie).
    **`voll_wenn_unvollstaendig` (QS-Gemini-B2):** gegen stille Truncation — reicht ein `to_date` über den
    letzten gecachten Bar hinaus, ist der Cache für dieses Fenster evtl. unvollständig (ein noch AKTIVES Symbol
    hat neuere Bars). True erzwingt dann EINEN frischen Voll-Abruf + Re-Cache, bevor geschnitten wird. Default
    False = für die HISTORISCHE Ablation korrekt (Fenster-Ende liegt vor dem Abrufdatum; delistete Namen sind
    ohnehin vollständig — so kostet ein delisteter Name nicht jeden Lauf erneut Quota). -> Liste EOD-Zeilen."""
    import eod_cache

    def _voll_fetch(s):
        return _fetch_eod_full(s, api_token=api_token, timeout=timeout)

    # **cache_only (Governing Guardrail 6, Jens 07.08.):** der RECHENPFAD (Retro/Ablation/Outcome) macht NIE
    # einen Live-Abruf — ein nicht gecachtes Symbol liefert [] (fehlt), wird NICHT ad-hoc heruntergeladen. Das
    # Laden ist ausschließlich Sache des dedizierten asynchronen Load-Jobs (`prefetch_eod_parallel`/datenpflege)
    # in seinem eigenen Zeitfenster. So kann eine Rechnung nie 12k Live-Fetches auslösen (der „der-run-steht"-Fall).
    if cache_only:
        voll = eod_cache.lade(symbol, cache_dir=eod_cache._CACHE_DIR, max_alter_tage=max_alter_tage)
        return _eod_slice(voll if isinstance(voll, list) else [], from_date, to_date)
    voll = eod_cache.hole(symbol, _voll_fetch, cache_dir=eod_cache._CACHE_DIR,
                          max_alter_tage=max_alter_tage)          # Attr zur Call-Zeit
    voll = voll if isinstance(voll, list) else []
    if voll_wenn_unvollstaendig and to_date and voll:
        letzter = max((r.get("date") or "")[:10] for r in voll if isinstance(r, dict) and r.get("date"))
        if letzter and letzter < to_date[:10]:                   # Cache endet vor dem geforderten Fensterende
            frisch = _voll_fetch(symbol)                         # EIN frischer Voll-Abruf (fail-loud → nicht inert)
            if isinstance(frisch, list):
                eod_cache.speichere(symbol, frisch, cache_dir=eod_cache._CACHE_DIR)
                voll = frisch
    return _eod_slice(voll, from_date, to_date)


def prefetch_eod_parallel(symbols, from_date=None, max_workers=8, batch=200,
                          fehler_quote_abbruch=0.8, sync_callback=None, sync_alle_batches=20,
                          fetch_fn=None, ist_gecacht_fn=None, log_fn=None):
    """Zieht die EOD-Voll-History vieler Symbole NEBENLÄUFIG in den `eod_cache` (cache-first: schon gecachte
    werden übersprungen → resumierbar). Für den Voll-Ablations-Lauf (~28k Symbole, Jens 31.07.): der
    sequenzielle Einzel-Fetch (~50/min) ist der Flaschenhals; moderate Nebenläufigkeit (curl je Thread — GIL
    ist im subprocess frei) hebt das ~8-10×. Schreibt je Symbol eine eigene Cache-Datei (thread-sicher).

    **Fail-closed (QS-Gemini-B1-Linie, gegen stille Truncation):** eine hohe Fehlerquote in einem Batch
    (`> fehler_quote_abbruch`, Quota-Limit/systemisch) bricht den Prefetch KONTROLLIERT ab (RuntimeError)
    statt still eine Teilmenge zu füllen; isolierte Einzelausfälle (< Quote) werden toleriert (das Symbol
    bleibt ungecacht, der Ablations-Assembler überspringt es sauber). Genuines No-Data kommt als 200+`[]`
    (gecacht, ZÄHLT ALS ERFOLG); nur echte Exceptions (Quota/Netz/Zugriff) erhöhen den Fehlerzähler. Die
    Schwelle 0.8 (QS-Gemini-B1) ist bewusst hoch: ein Quota-Limit lässt ~100 % eines Batches scheitern,
    seltene Einzel-404 (das Universum sind reale Symbole, Delistete haben EOD) bleiben weit darunter →
    kein False-Positive-Abbruch. `batch=200` (QS-B3): schnelleres Fail-fast bei Quota (weniger verpuffte Calls).

    **Reclaim-fest:** optionaler `sync_callback` (Drive-Zwischen-Sync) alle `sync_alle_batches` Batches +
    final → bei Container-Reclaim geht der bis dahin gezogene EOD-Stand nicht verloren. **Sync sparsam
    halten (Jens 31.07.):** `sync_hoch` lädt je Aufruf ALLE seither geänderten Buckets hoch; da neue Symbole
    immer wieder dieselben ~360 EOD-Buckets treffen, wächst die Upload-Menge — zu häufiger Sync serialisiert
    (O(n²)-Upload) und frisst die Parallelisierung. Default 20 Batches (~4000 Symbole/Sync); bei sehr großen
    Läufen höher (~40 = ~8000). Deps (`fetch_fn`/`ist_gecacht_fn`) injizierbar (offline testbar).
    -> dict {gezogen, uebersprungen, fehler, gesamt}."""
    from concurrent.futures import ThreadPoolExecutor
    if fetch_fn is None:
        def fetch_fn(s):
            return fetch_eod_cached(s, from_date=from_date)      # cache-first; wirft bei Quota/Netz (fail-loud)
    if ist_gecacht_fn is None:
        import eod_cache
        ist_gecacht_fn = eod_cache.ist_gecacht
    todo = [s for s in symbols if not ist_gecacht_fn(s)]
    uebersprungen = len(symbols) - len(todo)
    gezogen = fehler = 0

    def _one(sym):
        try:
            fetch_fn(sym)                                        # [] = No-Data wird gecacht (kein Fehler)
            return True
        except Exception:                                       # noqa: BLE001 — echter Abruf-Fehler (Quota/Netz/Zugriff)
            return False
    n_batches = (len(todo) + batch - 1) // batch
    for bi in range(n_batches):
        chunk = todo[bi * batch:(bi + 1) * batch]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            ergebnisse = list(ex.map(_one, chunk))
        b_ok = sum(ergebnisse)
        b_fehler = len(chunk) - b_ok
        gezogen += b_ok
        fehler += b_fehler
        if chunk and b_fehler / len(chunk) > fehler_quote_abbruch:
            raise RuntimeError(
                f"EOD-Prefetch FAIL-CLOSED: {b_fehler}/{len(chunk)} in einem Batch fehlgeschlagen "
                f"(Quota-Limit/systemisch?). Bis hier gezogen: {gezogen}. Cache persistiert → resumierbar.")
        if sync_callback and (bi + 1) % sync_alle_batches == 0:
            try:
                sync_callback()                                 # Drive-Zwischen-Sync (reclaim-fest)
            except Exception:                                   # noqa: BLE001 — Sync-Fehler kippt den Prefetch nie
                pass
        if log_fn:
            log_fn(f"    EOD-Prefetch: {min((bi + 1) * batch, len(todo))}/{len(todo)} "
                   f"(ok {gezogen}, Fehler {fehler}, übersprungen {uebersprungen})")
    if sync_callback:                                           # finaler Sync (letzter Teil-Batch)
        try:
            sync_callback()
        except Exception:                                       # noqa: BLE001
            pass
    return {"gezogen": gezogen, "uebersprungen": uebersprungen, "fehler": fehler, "gesamt": len(symbols)}


_FUND_BASE = "https://eodhd.com/api/fundamentals"


def _fetch_fundamentals_full(symbol, api_token=None, timeout=30):
    """LIVE: der VOLLE (ungefilterte) EODHD-Fundamentals-Dump. Ein Symbol ohne `General`-Block (No-Data/
    Not-Found, häufig delistet) → `{}` (sauberer No-Data-Marker → Cache hakt es ab, nie wieder abrufen).
    Harte Fehler (rc/Forbidden/error-Objekt/non-JSON) wirft `_curl_json` (fail-loud, NICHT cachen)."""
    url = f"{_FUND_BASE}/{symbol}?api_token={_token(api_token)}&fmt=json"        # UNGEFILTERT = Voll-Dump
    data = _curl_json(url, timeout)
    if not isinstance(data, dict) or not isinstance(data.get("General"), dict):
        return {}
    return data


def fetch_fundamentals(symbol, api_token=None, timeout=30, cache=True):
    """EODHD-Fundamentals eines Symbols (Fundamentals-Eingang: Cash_Flow quarterly + SharesOutstanding).
    **Cache-first (Jens 26.07.):** liest den geteilten Voll-Dump-Cache (`fundamentals_cache`) — ein bereits von
    der Klassifikation gezogenes Symbol kostet KEINEN zweiten 10-Einheiten-Call. Bei Miss wird der VOLLE Dump
    einmal gezogen, gespeichert (auch für die Klassifikation) und zurückgegeben. Die verschachtelungs-toleranten
    Parser (`zu_cashflow_quarterly`/`shares_aus_fundamentals`) lesen die Felder aus dem Voll-Dump. Funktioniert
    auch für delistete Symbole. `cache=False` erzwingt einen Live-Abruf ohne Cache. -> dict (roh)."""
    if not cache:
        return _fetch_fundamentals_full(symbol, api_token, timeout)
    import fundamentals_cache
    return fundamentals_cache.hole(symbol, lambda s: _fetch_fundamentals_full(s, api_token, timeout))


def parse_earnings_kalender(data):
    """REIN (offline testbar): die echte `/calendar/earnings`-Antwort -> Liste `{symbol, report_date}`.

    Echtes Schema (Live-Smoke 2026-08-06): ein dict `{type, description, from, to, earnings:[{code,
    report_date, date, before_after_market, currency, ...Analysten-Schaetz-/Ist-Felder...}]}`.
    **Analysten-Firewall (Datenpflege G8, mechanisch erzwungen via Quell-Scan):** es werden AUSSCHLIESSLICH
    `code` (Symbol) und `report_date` gelesen — die Analysten-Schaetz-/Ist-/Differenz-Felder der Antwort
    werden NIE angefasst (die verworfene Earnings-Surprise-Kruecke laege einen Feldzugriff entfernt).
    Zeilen ohne Symbol oder ohne sauber parsebares `report_date` (`_parse_iso`, positive Validierung)
    fallen raus. -> Liste `{symbol, report_date}` (ISO), Reihenfolge der Antwort."""
    zeilen = data.get("earnings") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    out = []
    for r in zeilen or []:
        if not isinstance(r, dict):
            continue
        sym = r.get("code")
        rd = _parse_iso(r.get("report_date"))
        if sym and rd:
            out.append({"symbol": sym, "report_date": rd.isoformat()})
    return out


def fetch_earnings_kalender(von, bis, api_token=None, timeout=60):
    """LIVE: EODHD-Bulk-Earnings-Kalender `/calendar/earnings?from=&to=` — EIN Call -> viele Symbole mit
    `report_date` (die Melde-TIMING-Quelle des Datenpflege-Auffrischers; die WAHRHEIT bleibt das
    `filing_date` im Fundamentals-Dump, W4). Verfuegbarkeit am Key live bestaetigt (2026-08-06: 200,
    ~2400 Zeilen fuer ein 2-Tage-Fenster). **G8-Firewall:** liefert NUR `{symbol, report_date}` (siehe
    `parse_earnings_kalender`). Fehlt Endpoint/Key/Netz -> leere Liste + lauter Hinweis (der quota-freie
    Kadenz-Fallback des Auffrischers traegt dann allein); ein leerer Kalender ist NIE ein Fehler-Abbruch
    des Aufrufers. [Feinkonzept Datenpflege §8: einzige Connector-Ergaenzung]"""
    try:
        url = (f"{API_ROOT}/calendar/earnings?from={von}&to={bis}"
               f"&api_token={_token(api_token)}&fmt=json")
        data = _curl_json(url, timeout)
    except RuntimeError as e:
        print(f"  ⚠ Earnings-Kalender nicht verfuegbar ({e}) — Kadenz-Fallback traegt.")
        return []
    return parse_earnings_kalender(data)


def zu_cashflow_quarterly(fundamentals):
    """EODHD-Fundamentals -> Modul-9-`cashflow_quarterly`-Schema
    {date: {date, freeCashFlow, filing_date[, capitalExpenditures]}}.
    Liest `Financials::Cash_Flow::quarterly` (gefiltert ODER verschachtelt). NUR Quartale mit echtem
    freeCashFlow UND filing_date (PIT-Pflicht — Modul 9 braucht das Offenlegungsdatum). Pure/offline-testbar.
    `capitalExpenditures` wird MITGEFÜHRT, wenn vorhanden (optional, rückwärtskompatibel: Modul 9 liest
    weiter nur freeCashFlow) — es ist das Finanz-Frühsignal (Kapital-Bindung, Modul 11b S′/S″-Eingang)."""
    cf = fundamentals.get("Financials::Cash_Flow::quarterly")
    if cf is None:
        cf = ((fundamentals.get("Financials") or {}).get("Cash_Flow") or {}).get("quarterly")
    out = {}
    for datum, zeile in (cf.items() if isinstance(cf, dict) else []):
        if not isinstance(zeile, dict):
            continue
        fcf, filing = zeile.get("freeCashFlow"), zeile.get("filing_date")
        if fcf in (None, "") or not filing:
            continue                                     # fehlender Wert/Offenlegung -> raus (kein Fake-0)
        eintrag = {"date": zeile.get("date", datum), "freeCashFlow": fcf, "filing_date": filing}
        capex = zeile.get("capitalExpenditures")
        if capex not in (None, ""):                      # optional — nur wenn EODHD es liefert
            eintrag["capitalExpenditures"] = capex
        out[datum] = eintrag
    return out


def shares_aus_fundamentals(fundamentals):
    """SharesOutstanding (Skalar) aus den Fundamentals. None, wenn fehlt/≤0. Pure/offline-testbar."""
    val = fundamentals.get("SharesStats::SharesOutstanding")
    if val is None:
        val = (fundamentals.get("SharesStats") or {}).get("SharesOutstanding")
    try:
        s = float(val)
        return s if s > 0 else None
    except (TypeError, ValueError):
        return None


def _quarterly(fundamentals, block):
    """Ein `Financials::<block>::quarterly`-Dict lesen (gefiltert ODER verschachtelt). Pure/offline."""
    q = fundamentals.get(f"Financials::{block}::quarterly")
    if q is None:
        q = ((fundamentals.get("Financials") or {}).get(block) or {}).get("quarterly")
    return q if isinstance(q, dict) else {}


def _parse_iso(s):
    """Sauber-parsebares ISO-Datum -> datetime.date, sonst None. POSITIVE Validierung (QS-B1): degenerierte
    EODHD-Platzhalter ('0000-00-00', jahr-only '2020') werden NICHT als gültig durchgelassen (Look-Ahead-Riegel)."""
    import datetime
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _pit_fenster(quarterly, stichtag, k=4):
    """Die k jüngsten KONTIGUIERLICHEN Quartale mit sauber parsebarem filing_date <= stichtag (PIT).
    -> Liste [(fiskal_date, zeile)] (genau k) oder None (fail-closed: <k sichtbar / unparsebar / Lücke).
    QS-B1 (positive Datums-Validierung) + QS-B2 (Kontiguität: jeder Quartalsabstand ~1 Quartal, sonst Fake-TTM)."""
    st = _parse_iso(stichtag)
    if st is None:
        return None
    sichtbar = []
    for datum, z in (quarterly.items() if isinstance(quarterly, dict) else []):
        if not isinstance(z, dict):
            continue
        fil, fisk = _parse_iso(z.get("filing_date")), _parse_iso(z.get("date", datum))
        if fil is None or fisk is None or fil > st:       # nur sauber parsebar UND wissbar
            continue
        sichtbar.append((fisk, z))
    if len(sichtbar) < k:
        return None
    sichtbar.sort(key=lambda x: x[0])
    fenster = sichtbar[-k:]
    for (d0, _), (d1, _) in zip(fenster, fenster[1:]):    # Kontiguität: kein übersprungenes Quartal
        if not (60 <= (d1 - d0).days <= 130):
            return None
    return fenster


def _summe(fenster, felder):
    """Feld-Summen über ein Quartals-Fenster; None bei fehlenden Werten (fail-closed, kein Fake-0)."""
    summe = {}
    for f in felder:
        werte = [z.get(f) for _d, z in fenster]
        if any(w in (None, "", "None") for w in werte):
            return None
        try:
            summe[f] = sum(float(w) for w in werte)
        except (TypeError, ValueError):
            return None
    return summe


# Hinweis: Die Bewertungsnaht (Ableitung von Kennzahlen aus Kursen + Fundamentals) ist Teil der
# proprietären Analyse-Schicht und NICHT Bestandteil dieser offenen Dateninfrastruktur. Dieser Konnektor
# liefert ausschliesslich die Rohdaten (Kurse, Fundamentals, Shares) PIT-sauber; die Ableitung daraus
# lebt in der nicht-öffentlichen Analyse-Schicht.


def preis_am_stichtag(eod_rows, stichtag, feld="adjusted_close", max_stale_tage=None):
    """PIT: letzter Kurs mit date <= stichtag. None, wenn keiner. Pure/offline-testbar.
    eod_rows: fetch_eod-Liste ({date, adjusted_close, ...}).

    `max_stale_tage` (Aktivitäts-Filter, survivorship-Naht): ist gesetzt, gilt ein Kurs nur als am
    Stichtag gültig, wenn sein Handelstag HÖCHSTENS so viele Kalendertage vor dem Stichtag liegt. Ein
    vor Monaten/Jahren delisteter Wert (letzter Print weit vor t) liefert dann `None` statt seines
    veralteten letzten Kurses — er war am Stichtag NICHT aktiv gehandelt und gehört nicht in den
    Signal-Querschnitt (spiegelt die Outcome-Seite `fenster_return`, die vor t Gestorbene ebenfalls
    ausschließt). Rein rückwärtsschauend (nur date <= stichtag) → kein Look-Ahead. None = kein Filter
    (rückwärtskompatibel)."""
    kandidat = None
    for r in eod_rows:
        d = r.get("date")
        if d and d <= stichtag and r.get(feld) is not None and (kandidat is None or d > kandidat[0]):
            kandidat = (d, r[feld])
    if kandidat is None:
        return None
    if max_stale_tage is not None:
        import datetime
        try:
            d_kurs = datetime.date.fromisoformat(kandidat[0][:10])
            d_t = datetime.date.fromisoformat(stichtag[:10])
        except (TypeError, ValueError):
            return None                                  # unparsebares Datum -> fail-closed (nicht aktiv)
        if (d_t - d_kurs).days > int(max_stale_tage):
            return None                                  # letzter Print zu alt -> am Stichtag nicht aktiv
    try:
        return float(kandidat[1])
    except (TypeError, ValueError):
        return None


def umschlag_am_stichtag(eod_rows, shares, stichtag, fenster_tage=90):
    """PIT-Umschlagshäufigkeit (Turnover) = Ø Tagesvolumen im Fenster [stichtag-N, stichtag] / shares.
    Proxy für den GRENZKÄUFER-HORIZONT (V3/F71): hoher Turnover = kurze Haltedauer (≈ 1/Turnover) =
    kurzer Halter-Horizont (die lange Konvergenzkette ist un-gehalten). None, wenn kein Volumen/keine
    shares. Analysten-frei, pure/offline-testbar. `eod_rows`: fetch_eod-Liste ({date, volume, ...}).
    QS-Caveat: das Volumen-Fenster ist PIT (≤ stichtag), der NENNER `shares` ist aber der aktuelle
    Skalar (system-weiter Shares-Vintage) — für ein Ranking 2. Ordnung unkritisch, aber nicht voll
    PIT-rein. QS-Confound: Turnover korreliert mit Liquidität/Reife (nicht nur Halter-Horizont)."""
    if not shares or shares <= 0:
        return None
    import datetime
    try:
        bis = datetime.date.fromisoformat(stichtag)
        von = (bis - datetime.timedelta(days=fenster_tage)).isoformat()
    except (TypeError, ValueError):
        return None
    vols = [r.get("volume") for r in eod_rows
            if r.get("date") and von <= r["date"] <= stichtag and r.get("volume") is not None]
    vols = [float(v) for v in vols if _istzahl(v)]
    if not vols:
        return None
    return (sum(vols) / len(vols)) / float(shares)


def _istzahl(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def fetch_symbol_list(exchange="US", delisted=False, typ=None, api_token=None, timeout=60):
    """LIVE: Symbolliste einer Börse. GET /exchange-symbol-list/{exchange}. `delisted=True` -> die
    INAKTIVEN (delisteten) Namen (`delisted=1`) — der survivorship-freie Teil. `typ`
    filtert z. B. 'common_stock','etf','fund'. -> Liste {Code, Name, Country, Exchange, Currency, Type, Isin}."""
    url = f"{API_ROOT}/exchange-symbol-list/{exchange}?api_token={_token(api_token)}&fmt=json"
    if delisted:
        url += "&delisted=1"
    if typ:
        url += f"&type={typ}"
    data = _curl_json(url, timeout)
    return data if isinstance(data, list) else []


def fetch_exchanges(api_token=None, timeout=30):
    """LIVE: Börsenkatalog (Name, Code, Land, Währung) — für den §-Quellen-Audit (US+EU-Abdeckung)."""
    data = _curl_json(f"{API_ROOT}/exchanges-list/?api_token={_token(api_token)}&fmt=json", timeout)
    return data if isinstance(data, list) else []


def symbol_id(row):
    """EODHD-Symbol-ID '{Code}.{Exchange}' aus einer Symbollisten-Zeile (für /eod). '' wenn unvollständig."""
    code, ex = row.get("Code"), row.get("Exchange")
    return f"{code}.{ex}" if code and ex else ""


def symbol_universum(aktiv_rows, delisted_rows, typ_filter=None):
    """Survivorship-frei: aktive + delistete Symbole zu EINEM Universum mischen, delisted-Flag gesetzt.
    Aktive gewinnen bei ID-Kollision (delisted nur, wenn nicht schon aktiv). `typ_filter` (z. B.
    'Common Stock') optional. -> Liste {symbol_id, code, exchange, name, typ, isin, delisted}.

    DAS ist der Punkt, an dem Survivorship-Bias verschwindet: wer nur `aktiv_rows` nimmt, sieht nur die
    Überlebenden. Die survivorship-freie Kohorten-Konstruktion braucht das volle Universum inkl. delistet."""
    def _norm(row, is_delisted):
        return {
            "symbol_id": symbol_id(row), "code": row.get("Code"), "exchange": row.get("Exchange"),
            "name": row.get("Name"), "typ": row.get("Type"), "isin": row.get("Isin"),
            "delisted": is_delisted,
        }
    def _passt(row):
        return (typ_filter is None or row.get("Type") == typ_filter) and symbol_id(row)
    aus = {}
    for r in aktiv_rows:
        if _passt(r):
            aus[symbol_id(r)] = _norm(r, False)
    for r in delisted_rows:
        if _passt(r) and symbol_id(r) not in aus:
            aus[symbol_id(r)] = _norm(r, True)
    return sorted(aus.values(), key=lambda x: x["symbol_id"])


def fetch_universum(exchange="US", typ=None, api_token=None):
    """Bequem (live, 2 Calls): aktive + delistete Symbolliste einer Börse -> survivorship-freies Universum."""
    aktiv = fetch_symbol_list(exchange, delisted=False, api_token=api_token)
    tot = fetch_symbol_list(exchange, delisted=True, api_token=api_token)
    return symbol_universum(aktiv, tot, typ_filter=typ)


def lade_preise(symbol, instrument_id=None, art="sektor_etf", t_ingest=None,
                from_date=None, to_date=None, api_token=None):
    """Bequem: fetch_eod + zu_preis_roh in einem Schritt (live)."""
    import datetime
    eod = fetch_eod(symbol, from_date, to_date, api_token)
    return zu_preis_roh(eod, instrument_id or symbol, art,
                        t_ingest or datetime.date.today().isoformat())


def eod_returns_universum(universum, kat_map=None, fetch=None, from_date=None, to_date=None,
                          feld="adjusted_close", period="d", api_token=None):
    """Brücke Modul 8 (F101 §3.2): survivorship-freie Returns-Tabelle aus einem Symbol-Universum.

    Genau die Eingabe, die die Kohorten-/Outcome-Konstruktion der Analyse-Schicht erwartet
    ({kat_id, return, delisted}) — der Punkt, an dem die delistete Historie in die Kohorten-Konstruktion fließt.

    - `universum`: Zeilen aus `symbol_universum` ({symbol_id, delisted, ...}).
    - `kat_map`:   symbol_id -> kat_id. None => kat_id = symbol_id (Symbol als eigener Knoten-Proxy).
                   Firmen sind Blätter; die Verdichtung Blatt->Kategorie ist eine offene Design-Frage
                   (F102, Fragen-Batch) — hier mechanisch 1:1, damit der Survivorship-Beleg
                   *falsifizierbar* bleibt und nicht an einer ungetroffenen Mapping-Entscheidung hängt.
    - `fetch`:     fetch_eod-kompatibel (symbol, from_date, to_date, api_token, period) -> EOD-Zeilen.
                   Injizierbar (Default `fetch_eod`) -> offline testbar ohne Netz/Key.
    - Rückgabe:    Liste {kat_id, return, delisted}. Symbole mit < 2 Kurspunkten (zu_return None)
                   werden ausgelassen (fail-quiet). **Delistete Namen bleiben erhalten** -> genau das
                   verhindert den Survivorship-Bias, gegen den Modul 8 gebaut ist.
    """
    if fetch is None:
        fetch = fetch_eod
    out = []
    for row in universum:
        sid = row.get("symbol_id")
        if not sid:
            continue
        kat = (kat_map or {}).get(sid, sid)
        eod = fetch(sid, from_date=from_date, to_date=to_date, api_token=api_token, period=period)
        r = zu_return(eod, feld=feld)
        if r is None:
            continue
        out.append({"kat_id": kat, "return": r, "delisted": bool(row.get("delisted"))})
    return out


def verdichte_kategorie(returns_rows):
    """F102 (Variante a, entschieden 22.07.): verdichtet die Symbol-/Blatt-Returns
    (`eod_returns_universum`) zu EINEM Return je Kategorie — **gleichgewichtetes Mittel** der
    Konstituenten, **delistete Namen mit ihrem echten (bis −100 %) Return eingeschlossen**.

    Gewählt, weil es die **sicherste Vergleichsbasis** liefert: dieselbe parameterfreie Regel für JEDE
    Kategorie, allein aus den vorhandenen Return-Reihen des survivorship-freien Universums — KEINE
    Abhängigkeit von PIT-Kapitalgewichten (Variante b) oder heterogenen ETF-Produkten (Variante c), die
    Kategorien untereinander unvergleichbar machen. `delisted` der Kategorie = True, sobald IRGENDEIN
    Konstituent delistet ist (erhält die Survivorship-Metrik §7 von Modul 8).

    -> Liste `{kat_id, return, delisted, n_konstituenten}`, stabil nach kat_id sortiert; direkt für
    die Kohorten-/Outcome-Konstruktion. Leere Eingabe -> []."""
    grp = {}
    for r in returns_rows:
        k = r["kat_id"]
        g = grp.setdefault(k, {"summe": 0.0, "n": 0, "delisted": False})
        g["summe"] += float(r["return"])
        g["n"] += 1
        g["delisted"] = g["delisted"] or bool(r.get("delisted"))
    aus = [{"kat_id": k, "return": g["summe"] / g["n"], "delisted": g["delisted"],
            "n_konstituenten": g["n"]} for k, g in grp.items() if g["n"] > 0]
    return sorted(aus, key=lambda x: x["kat_id"])
