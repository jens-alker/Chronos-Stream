"""
markt_db.py — die EINHEITLICHE lokale SQLite-DB der strukturierten Markt-/Fundamentaldaten (DB A, Jens 1.8.).

Jens-Direktive (1.8.): am Ende **zwei** Datenbanken, **lokal führend**. Für die strukturierten EOD-/Markt-
Daten „auf jeden Fall eine einheitliche DB" (nicht mehr die gzip-Per-Entität-Cache-Struktur als das Store).
Dieses Modul ist DB A: EIN SQLite-File, strukturierte + abfragbare Tabellen, getrennt je Datenart/Quelle.

**Keine Insel:** die Speicher-MECHANIK der gzip-Caches (fundamentals_cache/eod_cache/quellen_cache) bleibt der
Transport-/Backup-Layer (Drive-Sync); markt_db ist der KONSOLIDIERTE, abfragbare lokale Führer. `migriere_*`
liest die bestehenden Caches und füllt die DB (idempotent) — keine zweite Fetch-Definition.

**EOD nach (Zeitpunkt × Entity) organisiert (Jens 1.8.):** die Bars sind so indiziert, dass BEIDE Achsen
billig sind — die **Entwicklung je Entity** (fix symbol, variiere datum: `lade_eod`) UND der **Querschnitt
zu einem Zeitpunkt** (fix datum, variiere symbol: `lade_querschnitt`, `datum`-Index), inkl. **Gruppierung
nach Sektor/Kategorie** über `entity_meta` (`querschnitt_nach_kategorie`).

Tabellen:
- `eod_preis`  — strukturierte Tagesbars (symbol, datum, open/high/low/close/adjusted_close/volume). PK
                 (symbol, datum) trägt die Entity-Zeitreihe; Index auf `datum` trägt den Querschnitt.
- `entity_meta` — je Symbol Name/Sektor/Kategorie/Währung (die Achse für Sektor-Querschnitte); gefüllt aus
                 den Klassifikations-Maps/Fundamentals.
- `fundamentals` — ein Voll-Dump je Symbol als JSON-Payload (heterogen/nested; nicht spaltenweise zerlegt),
                 (symbol, t_ingest, payload). Extraktoren (Modul 9/18) lesen die Payload wie aus dem Cache.
- `sensor_serie` — die numerischen Alpha-Sensorik-Serien, generisch je Quelle: (quelle, entity, datum,
                 payload, t_disclosed). Getrennt je Quelle über die `quelle`-Spalte (COT/SI/Wiki/CDS/Marge/
                 Futures/USASpending); der PIT-Schnitt läuft über `t_disclosed`.

PIT bleibt hart: `t_disclosed`/`datum` sind die Wissbarkeits-Wand; Leser filtern `<= stichtag`. Nur stdlib.

**Bewusste Vintage-Grenzen (QS-Gemini-B1/B4, dokumentiert statt fingiert):**
- `fundamentals` ist symbol-keyed = der LETZTE Voll-Dump (spiegelt `fundamentals_cache`). Die PIT-Disziplin
  liegt IN der Payload (Modul 9 `ttm_fcf` nimmt nur Quartale mit `filing_date ≤ T`). Eine spätere REVISION
  eines Altquartals überschreibt die Erst-Vintage — das ist das längst anerkannte `wert_vintage`-Limit (der
  freie EODHD-Abruf liefert keine vintaged Dumps). KEIN t_disclosed-PK hier (anders als bei `sensor_serie`,
  wo Revisionen einzeln erfasst werden — dort ist der First-Print billig zu halten).
- `entity_meta` ist STATISCHE Stammdaten (aktuelle GicSubIndustry-Klassifikation, wie die bestehenden
  kat_maps). Historische GICS-Reklassifizierungen sind nicht abgebildet — für die Horizonte in Scope ein
  bewusst akzeptiertes kleines Limit (kein t_valid_from).
"""
import json
import os
import sqlite3

SCHEMA_DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS eod_preis (
  symbol TEXT, datum TEXT, open REAL, high REAL, low REAL, close REAL,
  adjusted_close REAL, volume REAL, t_ingest TEXT,
  PRIMARY KEY (symbol, datum));
CREATE INDEX IF NOT EXISTS ix_eod_symbol ON eod_preis(symbol);
CREATE INDEX IF NOT EXISTS ix_eod_datum ON eod_preis(datum);   -- Querschnitt: alle Entities zu einem Zeitpunkt
CREATE TABLE IF NOT EXISTS entity_meta (
  symbol TEXT PRIMARY KEY, name TEXT, sektor TEXT, kategorie TEXT, waehrung TEXT, t_ingest TEXT);
CREATE INDEX IF NOT EXISTS ix_meta_kategorie ON entity_meta(kategorie);
CREATE INDEX IF NOT EXISTS ix_meta_sektor ON entity_meta(sektor);
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol TEXT PRIMARY KEY, payload TEXT, t_ingest TEXT);
CREATE TABLE IF NOT EXISTS sensor_serie (
  quelle TEXT, entity TEXT, datum TEXT, payload TEXT, t_disclosed TEXT, t_ingest TEXT,
  -- QS-B3: t_disclosed IM Schlüssel -> Revisions-VINTAGES koexistieren (First-Print + Korrekturen); der
  -- PIT-Leser nimmt die zum Stichtag jüngste bekannte Vintage. datum = Perioden-/Referenzdatum (Achse),
  -- t_disclosed = Offenlegungs-Wand (QS-M1/B2: NICHT gleichsetzen — disclosure-lag!).
  PRIMARY KEY (quelle, entity, datum, t_disclosed));
CREATE INDEX IF NOT EXISTS ix_sensor_q_e ON sensor_serie(quelle, entity);
"""


def _minus_tage(iso_datum, tage):
    """'YYYY-MM-DD' minus `tage` Tage -> 'YYYY-MM-DD' (stdlib datetime, nur zum Fenster-Vergleich)."""
    import datetime
    y, m, d = (int(x) for x in str(iso_datum)[:10].split("-"))
    return (datetime.date(y, m, d) - datetime.timedelta(days=int(tage))).isoformat()


def oeffne(db_pfad):
    """Öffnet/legt die einheitliche Markt-DB an und gibt die Verbindung zurück (WAL, alle Tabellen)."""
    conn = sqlite3.connect(db_pfad)
    conn.executescript(SCHEMA_DDL)
    conn.commit()
    return conn


# ------------------------------------------------------------------ #
# EOD-Preise (strukturiert)
# ------------------------------------------------------------------ #
_EOD_FELDER = ("open", "high", "low", "close", "adjusted_close", "volume")


def schreibe_eod(conn, symbol, bars, t_ingest=None):
    """bars: [{'date'|'datum', 'open','high','low','close','adjusted_close'|'adjClose','volume'}]. INSERT OR
    REPLACE je (symbol, datum) — idempotent. Zeilen ohne Datum fallen fail-closed raus. -> Anzahl geschrieben."""
    n = 0
    for b in (bars or []):
        datum = b.get("date") or b.get("datum")
        if not datum:
            continue
        werte = {
            "open": b.get("open"), "high": b.get("high"), "low": b.get("low"), "close": b.get("close"),
            "adjusted_close": b.get("adjusted_close", b.get("adjClose")), "volume": b.get("volume"),
        }
        conn.execute(
            "INSERT OR REPLACE INTO eod_preis(symbol,datum,open,high,low,close,adjusted_close,volume,t_ingest)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (str(symbol), str(datum)[:10], werte["open"], werte["high"], werte["low"], werte["close"],
             werte["adjusted_close"], werte["volume"], t_ingest))
        n += 1
    conn.commit()
    return n


def lade_eod(conn, symbol, von=None, bis=None):
    """Entity-ZEITREIHE: strukturierte Bars je Symbol, chronologisch, optional Fenster [von, bis] (PIT:
    `bis` = Stichtag). -> [{'datum','open',...,'volume'}]. (Entwicklung nachvollziehen — fix symbol.)"""
    q = "SELECT datum,open,high,low,close,adjusted_close,volume FROM eod_preis WHERE symbol=?"
    args = [str(symbol)]
    if von:
        q += " AND datum>=?"; args.append(str(von)[:10])
    if bis:
        q += " AND datum<=?"; args.append(str(bis)[:10])
    q += " ORDER BY datum"
    keys = ("datum",) + _EOD_FELDER
    return [dict(zip(keys, row)) for row in conn.execute(q, args).fetchall()]


def lade_querschnitt(conn, stichtag, feld="close", max_stale_tage=None):
    """QUERSCHNITT zu einem Zeitpunkt: je Symbol der JÜNGSTE Bar mit `datum <= stichtag` (PIT — nicht jedes
    Symbol handelt jeden Tag), Wert aus `feld`. -> {symbol: (datum, wert)}. `max_stale_tage`: Symbole, deren
    letzter Bar älter als das Fenster ist (delistet/illiquide), werden ausgeschlossen — rückwärtsschauend,
    kein Look-ahead (der Aktivitäts-Filter, Guardrail 31.07.). Nutzt den `datum`-Index."""
    if feld not in _EOD_FELDER:
        raise ValueError(f"unbekanntes Feld {feld!r} (erlaubt: {_EOD_FELDER})")
    T = str(stichtag)[:10]
    # je Symbol der MAX(datum) <= T, dann der Wert dieses Bars
    rows = conn.execute(
        f"SELECT e.symbol, e.datum, e.{feld} FROM eod_preis e "
        "JOIN (SELECT symbol, MAX(datum) md FROM eod_preis WHERE datum<=? GROUP BY symbol) m "
        "ON e.symbol=m.symbol AND e.datum=m.md", (T,)).fetchall()
    unter = _minus_tage(T, max_stale_tage) if max_stale_tage is not None else None
    out = {}
    for sym, datum, wert in rows:
        if unter is not None and str(datum)[:10] < unter:
            continue                                   # zu alt -> inaktiv/delistet, raus
        out[sym] = (datum, wert)
    return out


def querschnitt_nach_kategorie(conn, stichtag, feld="close", ebene="kategorie", max_stale_tage=None):
    """SEKTOR-/KATEGORIE-QUERSCHNITT: der Zeitpunkt-Querschnitt (`lade_querschnitt`), gruppiert über
    `entity_meta.{ebene}` (`kategorie` oder `sektor`). -> {gruppe: {symbol: wert}}. Symbole ohne Meta-
    Zuordnung landen unter `gruppe=None` (transparent, nie still verworfen). Damit sind Sektor-Aggregate
    (Mittel/Median/Rang) direkt baubar."""
    if ebene not in ("kategorie", "sektor"):
        raise ValueError("ebene muss 'kategorie' oder 'sektor' sein")
    quer = lade_querschnitt(conn, stichtag, feld=feld, max_stale_tage=max_stale_tage)
    meta = {r[0]: r[1] for r in conn.execute(f"SELECT symbol, {ebene} FROM entity_meta").fetchall()}
    out = {}
    for sym, (_datum, wert) in quer.items():
        gruppe = meta.get(sym)
        out.setdefault(gruppe, {})[sym] = wert
    return out


def schreibe_entity_meta(conn, symbol, meta):
    """meta: {'name','sektor','kategorie','waehrung'} (alle optional). INSERT OR REPLACE je Symbol — die
    Achse für Sektor-Querschnitte. Speist sich aus den Klassifikations-Maps (kat_map) / Fundamentals."""
    conn.execute(
        "INSERT OR REPLACE INTO entity_meta(symbol,name,sektor,kategorie,waehrung,t_ingest) VALUES(?,?,?,?,?,?)",
        (str(symbol), meta.get("name"), meta.get("sektor"), meta.get("kategorie"),
         meta.get("waehrung"), meta.get("t_ingest")))
    conn.commit()


# ------------------------------------------------------------------ #
# Fundamentals (JSON-Payload je Symbol)
# ------------------------------------------------------------------ #
def schreibe_fundamentals(conn, symbol, payload, t_ingest=None):
    """Voll-Dump je Symbol als JSON ablegen (INSERT OR REPLACE — idempotent)."""
    conn.execute("INSERT OR REPLACE INTO fundamentals(symbol,payload,t_ingest) VALUES(?,?,?)",
                 (str(symbol), json.dumps(payload, ensure_ascii=False, sort_keys=True), t_ingest))
    conn.commit()


def lade_fundamentals(conn, symbol):
    """Den Fundamentals-Voll-Dump je Symbol (dict) oder None."""
    row = conn.execute("SELECT payload FROM fundamentals WHERE symbol=?", (str(symbol),)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


# ------------------------------------------------------------------ #
# Sensor-Serien (generisch je Quelle; PIT über t_disclosed)
# ------------------------------------------------------------------ #
def schreibe_sensor(conn, quelle, entity, reihe, datum_feld, disclosed_feld=None, t_ingest=None):
    """reihe: [{…, datum_feld: 'YYYY-MM-DD…', 't_disclosed': …}] (die Roh-Serie einer Entität). INSERT OR
    REPLACE je (quelle, entity, datum, t_disclosed). `datum_feld` = Perioden-/Referenzdatum (die Achse).
    **QS-M1/B2 (disclosure-lag):** `t_disclosed` = die Offenlegungs-Wand, aus `disclosed_feld` ODER (Default)
    dem `t_disclosed`-Feld der Zeile (das die Konnektoren LIEFERN) — NICHT mit `datum` gleichgesetzt. Fehlt
    beides, konservativer Fallback auf `t_ingest` (spätest bekannt), sonst zuletzt auf `datum` (No-Lag-Annahme
    — die Konnektoren setzen t_disclosed, dieser Zweig ist der Rand). Zeilen ohne Datum fallen fail-closed raus.
    -> Anzahl geschrieben."""
    dfeld = disclosed_feld or "t_disclosed"
    n = 0
    for r in (reihe or []):
        d = r.get(datum_feld)
        if not d:
            continue
        d10 = str(d)[:10]
        t_disc = r.get(dfeld) or r.get("t_disclosed") or t_ingest or d          # QS-M1: Disclosure-Wand, nie < datum
        conn.execute(
            "INSERT OR REPLACE INTO sensor_serie(quelle,entity,datum,payload,t_disclosed,t_ingest)"
            " VALUES(?,?,?,?,?,?)",
            (str(quelle), str(entity), d10, json.dumps(r, ensure_ascii=False, sort_keys=True),
             str(t_disc)[:10], t_ingest))
        n += 1
    conn.commit()
    return n


def lade_sensor(conn, quelle, entity, bis=None, inklusive=True):
    """VINTAGE-KORREKTE PIT-Serie einer Entität: je Perioden-`datum` die zum Stichtag `bis` JÜNGSTE bekannte
    Vintage (max `t_disclosed ≤ bis`) — Revisionen, die erst NACH `bis` veröffentlicht wurden, zählen nicht
    (QS-B3). `bis`=None → die jeweils neueste Vintage je datum. `inklusive`: `t_disclosed ≤ bis` bzw. `< bis`.
    -> [dict] (ein Roh-Payload je datum, chronologisch)."""
    op = "<=" if inklusive else "<"
    args = [str(quelle), str(entity)]
    bis_klausel = ""
    if bis:
        bis_klausel = f" AND t_disclosed {op} ?"
        args.append(str(bis)[:10])
    # je datum die MAX(t_disclosed) ≤ bis, dann der Payload genau dieser Vintage
    q = (f"SELECT s.payload FROM sensor_serie s JOIN "
         f"(SELECT datum, MAX(t_disclosed) mt FROM sensor_serie WHERE quelle=? AND entity=?{bis_klausel} "
         f"GROUP BY datum) v ON s.datum=v.datum AND s.t_disclosed=v.mt "
         f"WHERE s.quelle=? AND s.entity=? ORDER BY s.datum")
    args2 = args + [str(quelle), str(entity)]
    return [json.loads(row[0]) for row in conn.execute(q, args2).fetchall()]


# ------------------------------------------------------------------ #
# Migration aus den bestehenden gzip-Caches (idempotent; keine zweite Fetch-Definition)
# ------------------------------------------------------------------ #
def migriere_eod_aus_cache(conn, symbole, lade_fn, t_ingest=None, melde_fn=None, abbrechen_fn=None,
                           melde_jede=50):
    """`lade_fn(symbol) -> bars|None` (z. B. eod_cache.lade). Zieht die gecachten Bars je Symbol in die DB.
    `melde_fn(aktuell, gesamt)` = optionaler Fortschritts-Callback (gedrosselt alle `melde_jede`); `abbrechen_fn()`
    = kooperativer, VERLUSTFREIER Abbruch (die schon migrierten Symbole sind committet). -> (n_symbole, n_bars).
    Symbole ohne Cache werden übersprungen (kein Fetch hier)."""
    syms = list(symbole)
    ges = len(syms)
    ns, nb = 0, 0
    for i, s in enumerate(syms, 1):
        if abbrechen_fn is not None and abbrechen_fn():
            break
        bars = lade_fn(s)
        if bars:
            nb += schreibe_eod(conn, s, bars, t_ingest=t_ingest)
            ns += 1
        if melde_fn is not None and (i % melde_jede == 0 or i == ges):
            melde_fn(i, ges)
    return ns, nb


def migriere_fundamentals_aus_cache(conn, symbole, lade_fn, t_ingest=None, melde_fn=None, abbrechen_fn=None,
                                    melde_jede=50):
    """`lade_fn(symbol) -> payload|None` (z. B. fundamentals_cache.lade). `melde_fn`/`abbrechen_fn` wie
    `migriere_eod_aus_cache` (gedrosselter Fortschritt + verlustfreier Abbruch). -> n_symbole migriert."""
    syms = list(symbole)
    ges = len(syms)
    n = 0
    for i, s in enumerate(syms, 1):
        if abbrechen_fn is not None and abbrechen_fn():
            break
        p = lade_fn(s)
        if p:
            schreibe_fundamentals(conn, s, p, t_ingest=t_ingest)
            n += 1
        if melde_fn is not None and (i % melde_jede == 0 or i == ges):
            melde_fn(i, ges)
    return n


def migriere_sensor_aus_cache(conn, quelle, entities, lade_fn, datum_feld, t_ingest=None):
    """`lade_fn(entity) -> reihe|None` (z. B. quellen_cache.lade(quelle, entity)). -> (n_entities, n_zeilen)."""
    ne, nz = 0, 0
    for e in entities:
        reihe = lade_fn(e)
        if not reihe:
            continue
        nz += schreibe_sensor(conn, quelle, e, reihe, datum_feld, t_ingest=t_ingest)
        ne += 1
    return ne, nz


def aufbau_aus_caches(db_pfad, kat_map=None, eod_symbole=None, fund_symbole=None,
                      eod_lade=None, fund_lade=None, t_ingest=None, melde_fn=None, abbrechen_fn=None):
    """DER bisher fehlende PRODUZENT von markt_db (Jens 07.08.): zieht die rohen gzip-Cache-Dumps
    (eod_cache/fundamentals_cache) in die STRUKTURIERTE Markt-DB — der Aufrufer der schon gebauten, aber nie
    verdrahteten `migriere_*_aus_cache`-Primitiven (KEINE INSEL). So landen die Marktdaten dort, wo die
    Architektur sie erwartet („quantitativ = markt_db"), statt nur als Roh-Blobs im Cache; das Modul-17-
    Datenstand-Panel (`frische`/`coverage`) zeigt sie dann, und der Rechenpfad kann die indizierte DB lesen.

    `kat_map` = {symbol -> kategorie} (aus der GIC-Klassifikation) → `entity_meta` NUR für Symbole mit einem
    Cache-EINTRAG (auch ein No-Data-Marker `[]`/`{}` zählt — ein bekanntes, aber (noch) datenloses Symbol; es
    taucht mangels Preiszeilen nie im `querschnitt` auf, `coverage` weist es korrekt als unvollständig aus).
    Rein kat_map-seitige Symbole ohne jeden Cache-Eintrag bekommen KEINE Meta (MINOR-4-QS). `eod_symbole`/
    `fund_symbole` + `eod_lade`/`fund_lade` injizierbar
    (Offline-Test; Default = die echten Cache-Enumeratoren/-Loader). `melde_fn(phase, aktuell, gesamt)` +
    `abbrechen_fn()` = Fortschritt + verlustfreier Abbruch (Governing Guardrail; die Migration ist idempotent
    INSERT-OR-REPLACE, ein Abbruch verliert nichts). -> Kennzahl-dict.

    **Roh bleibt die Quelle (Vintage-Drift-Riegel):** markt_db ist ein PIT-deterministisches Derivat des
    Caches — der Roh-Dump bleibt der gespeicherte Ursprung, die DB ist jederzeit daraus reproduzierbar."""
    if eod_lade is None or eod_symbole is None:
        import eod_cache as _ec                                 # noqa: E402
        eod_lade = eod_lade or _ec.lade
        eod_symbole = _ec.symbole() if eod_symbole is None else eod_symbole
    if fund_lade is None or fund_symbole is None:
        import fundamentals_cache as _fc                         # noqa: E402
        fund_lade = fund_lade or _fc.lade
        fund_symbole = _fc.symbole() if fund_symbole is None else fund_symbole
    # EINMAL materialisieren: migriere_* konsumiert die Symbole (list()), das entity_meta-`set(…)` unten
    # ebenfalls — ein GENERATOR-Input liefe sonst leer (leere entity_meta trotz Daten). Listen sind idempotent.
    eod_symbole = list(eod_symbole)
    fund_symbole = list(fund_symbole)
    conn = oeffne(db_pfad)
    try:
        ne, nb = migriere_eod_aus_cache(
            conn, eod_symbole, eod_lade, t_ingest=t_ingest, abbrechen_fn=abbrechen_fn,
            melde_fn=(lambda i, g: melde_fn("EOD-Kurse", i, g)) if melde_fn else None)
        abgebrochen = bool(abbrechen_fn and abbrechen_fn())
        nf = 0
        if not abgebrochen:
            nf = migriere_fundamentals_aus_cache(
                conn, fund_symbole, fund_lade, t_ingest=t_ingest, abbrechen_fn=abbrechen_fn,
                melde_fn=(lambda i, g: melde_fn("Fundamentals", i, g)) if melde_fn else None)
            abgebrochen = bool(abbrechen_fn and abbrechen_fn())
        nm = 0
        if kat_map and not abgebrochen:
            relevante = set(eod_symbole) | set(fund_symbole)   # nur Symbole mit echten Cache-Daten
            for s, k in kat_map.items():
                if s in relevante:
                    schreibe_entity_meta(conn, s, {"kategorie": k, "t_ingest": t_ingest})
                    nm += 1
        n_eod, n_fund, n_sens = bestand(conn)
        return {"n_eod_symbole": ne, "n_bars": nb, "n_fundamentals": nf, "n_meta": nm,
                "abgebrochen": abgebrochen, "bestand_eod": n_eod, "bestand_fundamentals": n_fund}
    finally:
        conn.close()


def bestand(conn):
    """(n_eod_symbole, n_fundamentals, n_sensor_zeilen) — für den Migrations-/Größen-Checkpoint."""
    n_eod = conn.execute("SELECT COUNT(DISTINCT symbol) FROM eod_preis").fetchone()[0]
    n_fund = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
    n_sens = conn.execute("SELECT COUNT(*) FROM sensor_serie").fetchone()[0]
    return n_eod, n_fund, n_sens


def frische(conn):
    """(max_datum, max_t_ingest) der EOD-Kurse — die Datenfrische-Kennzahl für den Leitstand (Claude-F3:
    `bestand` liefert sie NICHT; der Frische-Leser gehört in seine Heimat markt_db, nicht als Roh-SELECT in
    die GUI). Leere Tabelle -> (None, None)."""
    row = conn.execute("SELECT MAX(datum), MAX(t_ingest) FROM eod_preis").fetchone()
    return (row[0], row[1]) if row else (None, None)


def coverage(conn, limit=100000):
    """Vollständigkeits-Karte je Entität (Modul-17-Feinkonzept §3.2): welches Symbol hat Preise
    (letztes EOD-Datum) × Fundamentals × Kategorie. Union der Symbole aus entity_meta/eod_preis/fundamentals.
    -> {'symbole':[{symbol,letztes_datum,hat_fundamentals,kategorie}], 'zusammenfassung':{n,n_vollständig,
    prozent,n_preise,n_fundamentals,n_kategorie}}. Rein lesend, deterministisch (sortiert)."""
    # Claude-#3: `hat_p` an einem NICHT-NULL-Datum, `hat_kat` an einer nicht-leeren Kategorie — sonst zählt die
    # Zusammenfassung „hat Preise/Kategorie", während die Kachel-Punkte grau sind (Zähler-vs-Anzeige-Widerspruch).
    eod = {s: d for s, d in conn.execute("SELECT symbol, MAX(datum) FROM eod_preis GROUP BY symbol").fetchall()
           if d}
    fund = {r[0] for r in conn.execute("SELECT symbol FROM fundamentals").fetchall()}
    kat = {s: k for s, k in conn.execute("SELECT symbol, kategorie FROM entity_meta").fetchall()
           if (k or "").strip()}
    alle = sorted(set(eod) | fund | set(kat))
    n = len(alle)
    zeilen, voll = [], 0
    for s in alle[:limit]:                                       # Claude-#3: voll konsistent über denselben Ausschnitt
        hat_p, hat_f, k = s in eod, s in fund, kat.get(s)
        if hat_p and hat_f and k:
            voll += 1
        zeilen.append({"symbol": s, "letztes_datum": eod.get(s), "hat_fundamentals": hat_f, "kategorie": k})
    n_gewertet = min(n, limit)                                   # Prozent über den GEWERTETEN Ausschnitt (kein Under-Report)
    return {"symbole": zeilen,
            "zusammenfassung": {"n": n, "n_gewertet": n_gewertet, "n_vollstaendig": voll,
                                "prozent": round(100.0 * voll / n_gewertet, 1) if n_gewertet else 0.0,
                                "n_preise": len(eod), "n_fundamentals": len(fund), "n_kategorie": len(kat)}}
