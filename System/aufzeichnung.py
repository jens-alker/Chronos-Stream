#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aufzeichnung.py — verlustfreie Aufzeichnungsschicht der Ingestion.

Reiner, offline-testbarer Recording-Kern: jedes gesammelte Dokument (AUCH abgelehnte — der
lernbare Nenner) und jedes extrahierte Attribut wird mit gepinnter Extraktor-Version in eine
SEPARATE `aufzeichnung.db` (WAL) geschrieben, ohne die eigentliche Sammler-DB zu beschweren.

Was hier IST (offline-getestet): das DDL der separaten `aufzeichnung.db`, die reinen Schreib-
Funktionen gegen die echte scraper-Zeilenform (documents/facts) und die Pro-Lauf-Nenner-Invariante
(`pruefe_lauf`, fail-loud). Die Live-Verdrahtung des Scrapers (`scraper.py` ruft diese Funktionen beim
Ernten) ist fail-safe: ein Fehler deaktiviert die Aufzeichnung, nie den Scraper.

Dates: ISO `YYYY-MM-DD` (lexikografisch = chronologisch vergleichbar). Nur Standardbibliothek.
"""


class AufzeichnungFehler(RuntimeError):
    """Verletzung einer Aufzeichnungs-Invariante (z. B. inkonsistenter Nenner) — fail-loud."""


def pruefe_lauf(ingest_row, n_relevanz_entscheid, n_dokument_roh):
    """Pro-Lauf-Abgleich: `n_gefunden == #relevanz_entscheid(run_id)` UND `n_neu == #dokument_roh(run_id)`.
    Differenz → `AufzeichnungFehler` (fail-loud). Belegt, dass der Verlust-Stopp lückenlos protokolliert."""
    fehler = []
    if ingest_row.get("n_gefunden") != n_relevanz_entscheid:
        fehler.append(f"n_gefunden={ingest_row.get('n_gefunden')} ≠ relevanz_entscheid={n_relevanz_entscheid}")
    if ingest_row.get("n_neu") != n_dokument_roh:
        fehler.append(f"n_neu={ingest_row.get('n_neu')} ≠ dokument_roh={n_dokument_roh}")
    if fehler:
        raise AufzeichnungFehler(f"Lauf {ingest_row.get('run_id')}: Nenner inkonsistent — " + "; ".join(fehler))
    return True


# --------------------------------------------------------------------------- #
# DDL der separaten aufzeichnung.db (WAL). Wird von der Live-Verdrahtung angelegt;
# hier als kanonische Referenz + für Offline-Tests gegen ein Temp-DB.
# --------------------------------------------------------------------------- #
SCHEMA_DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS ingest_log (
  run_id TEXT PRIMARY KEY, quelle TEXT, query TEXT, t_query TEXT,
  status TEXT, n_gefunden INTEGER, n_neu INTEGER, fehlermodus TEXT);
CREATE TABLE IF NOT EXISTS fund (           -- M:N welcher Lauf/Query holte welches Dokument (der Nenner)
  run_id TEXT, doc_id TEXT, UNIQUE(run_id, doc_id));
CREATE TABLE IF NOT EXISTS dokument_roh (   -- AUCH abgelehnte Dokumente (Voll-Payload)
  doc_id TEXT PRIMARY KEY, content_hash TEXT, payload_voll TEXT,
  t_event TEXT, t_disclosed TEXT, t_ingest TEXT, quelle TEXT, lizenz TEXT, angenommen INTEGER);
CREATE TABLE IF NOT EXISTS relevanz_entscheid (
  doc_id TEXT, angenommen INTEGER, urteil_ordinal TEXT, stimmen TEXT,
  extraktor_id TEXT, entschieden_am TEXT);
CREATE TABLE IF NOT EXISTS attribut (
  id INTEGER PRIMARY KEY, doc_id TEXT, fact_id TEXT, name TEXT, wert TEXT, span TEXT,
  extraktor_id TEXT, berechnet_am TEXT, pit_klasse TEXT);
CREATE TABLE IF NOT EXISTS extraktor_version (
  extraktor_id TEXT PRIMARY KEY, modell TEXT, gewichte_hash TEXT,
  cutoff_datum TEXT, pin_datum TEXT, prompt_hash TEXT, schema_version TEXT,
  temp REAL, seed INTEGER, gold_eval TEXT, overlap_lauf_ref TEXT);
"""


def schema_anlegen(conn):
    """Legt das Aufzeichnungs-Schema in einer sqlite3-Verbindung an (Live-Verdrahtung + Offline-Tests)."""
    conn.executescript(SCHEMA_DDL)
    conn.commit()


_RELEVANZ_SCHWELLE = 0.5


def _content_hash(title, text):
    import hashlib
    return hashlib.sha256(((title or "") + " " + (text or "")).encode("utf-8")).hexdigest()[:16]


def schreibe_dokument_roh(conn, doc_id, doc, t_ingest, run_id=None, relevanz_schwelle=_RELEVANZ_SCHWELLE):
    """Eine scraper-`documents`-Zeile (source_type/title/text/url/relevance/trust/published_at) ->
    `dokument_roh` (Voll-Payload, AUCH abgelehnte = der lernbare Nenner) + optional `fund` (M:N-Nenner).
    `angenommen` = relevance>=Schwelle. t_disclosed=published_at, t_ingest=Wissenszeit. INSERT OR REPLACE
    (idempotent je doc_id). REINE Funktion."""
    import json
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    angenommen = 1 if (doc.get("relevance") or 0) >= relevanz_schwelle else 0
    conn.execute(
        "INSERT OR REPLACE INTO dokument_roh(doc_id,content_hash,payload_voll,t_event,t_disclosed,"
        "t_ingest,quelle,lizenz,angenommen) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(doc_id), _content_hash(doc.get("title"), doc.get("text")), payload, None,
         doc.get("published_at"), t_ingest, doc.get("source_type"), doc.get("lizenz"), angenommen))
    if run_id is not None:
        conn.execute("INSERT OR IGNORE INTO fund(run_id, doc_id) VALUES(?,?)", (str(run_id), str(doc_id)))
    conn.commit()
    return angenommen


def schreibe_fakt_attribut(conn, doc_id, fact, extraktor_id, berechnet_am, fact_id=None):
    """Eine scraper-`facts`-Zeile (subjekt/beziehung/objekt/modus/signalart/reife_score/...) -> `attribut`-Zeilen
    (name/wert je Feld) der verlustfreien Aufzeichnung. REINE Funktion."""
    felder = ("subjekt", "beziehung", "objekt", "modus", "signalart", "reife_score",
              "latenz", "erwartungstempo", "konfidenz")
    for name in felder:
        wert = fact.get(name)
        if wert is None:
            continue
        conn.execute(
            "INSERT INTO attribut(doc_id,fact_id,name,wert,span,extraktor_id,berechnet_am,pit_klasse) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(doc_id), (str(fact_id) if fact_id is not None else None), name, str(wert), None,
             extraktor_id, berechnet_am, None))
    conn.commit()
