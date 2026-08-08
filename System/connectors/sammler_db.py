"""
sammler_db.py — Konnektor: Sammler-DB (scraper.db) -> v0-`facts`-Eingang von Modul 2.

Zwei Nähte, entsprechend der Architektur-Arbeitsteilung (Konzept §Stufe 2):
  - `lade_fakten` 🔑 — liest die **`facts`-Tabelle** (die 1c-Extraktion des Scrapers:
    Subjekt-Beziehung-Objekt + modus/signalart). DAS ist die saubere Naht: Modul 2 bekommt
    die EXTRAHIERTE Aussage und **kategorisiert** sie (Modul 2 = Ontologie/Kategorisierung,
    nicht Extraktion). 3.12-konform: die Dezimal-Urteile des Scrapers
    (reife_score/erwartungstempo/konfidenz) werden NICHT übernommen — Reifegrad/Tempo/Konfidenz
    entstehen ordinal downstream (Modul 5 / 2c / 2d). `modus`/`signalart`/`latenz` (kategorial/
    ordinal) bleiben erhalten.
  - `lade_dokumente` — liest die **`documents`-Tabelle** (Rohdokumente, subjekt=Volltext, ohne SBO).
    Das UMGEHT 1c und lässt Modul 2 den ganzen Text grob klassifizieren — nur ein Fallback für
    Bestände OHNE extrahierte Fakten (oder als Doku-Ebene). Für den echten Pfad `lade_fakten`.

`source_type` -> Quellentyp (paper/patent/funding/news). Bitemporal:
`published_at` -> t_event/t_disclosed (Offenlegung), `ingested_at` -> t_ingest (Wissenszeit).
PIT (Retro): `stichtag` -> nur am Stichtag sichtbare Zeilen (ingested_at < stichtag).

Read-only (rührt die Sammler-DB nie an). Läuft, sobald eine scraper.db vorliegt (lokaler Deploy
oder hochgeladene Stichprobe). Nur Standardbibliothek (sqlite3).

Entity-Resolution (leichtgewichtig, hier verortet — die Naht, an der die Roh-SBO in Modul 2
eintreten): `kanonisiere_entitaet` lässt Firmen-Varianten (TSMC Inc. / TSMC, Inc / TSMC) auf EINEN
Knoten fallen (Rechtsform-Suffix-Strip + Whitespace/Casing), statt den Graphen zu zersplittern.
Optionale Alias-Map löst bekannte Synonyme. Echtes KB-Linking (Wikidata/Ticker/LEI) ist eine
terminierte Erweiterung (braucht Daten). Semantik-Dedup des Sammlers (documents.dup_of) wird — falls
vorhanden — respektiert: Fakten aus Near-Dup-Dokumenten fließen NICHT in die Kalibrierung.
"""
import json
import os
import re
import sqlite3

# source_type des Sammlers -> Quellentyp der Reifegradleiter (Modul 5). Default news (konservativ).
_QUELLENTYP = {
    "arxiv": "paper", "openalex": "paper", "paper": "paper", "wissenschaft": "paper",
    "epo": "patent", "patent": "patent",
    "edgar": "funding", "formd": "funding", "form_d": "funding", "form-d": "funding",
    "funding": "funding", "seed": "funding",
    "rss": "news", "eqs": "news", "adhoc": "news", "adhoc_mitteilung": "news", "news": "news",
}
_MAX_TEXT = 2000      # Zeichen des Dokumenttexts an die Extraktion (LLM-Kontext-Budget)

# Für Tests/Demos: die exakten Sammler-Schemata (Auszug aus scraper.py).
SCHEMA_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, source_id INTEGER, source_type TEXT,
  title TEXT, text TEXT, url TEXT,
  relevance REAL, trust REAL, published_at TEXT NOT NULL,
  ingested_at TEXT DEFAULT (datetime('now')),
  dup_of INTEGER,                    -- Semantik-Dedup: kanonisches Dokument (nicht-destruktiv)
  UNIQUE(title, published_at));
"""

# Die 1c-Extraktions-Tabelle: Subjekt-Beziehung-Objekt + Urteile (scraper.py §Modul 1c).
SCHEMA_FACTS = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY, doc_id INTEGER, source_type TEXT,
  subjekt TEXT, beziehung TEXT, objekt TEXT,
  modus TEXT, signalart TEXT,
  reife TEXT, reife_score REAL, latenz TEXT,
  erwartungstempo REAL, konfidenz REAL,
  published_at TEXT, ingested_at TEXT DEFAULT (datetime('now')),
  UNIQUE(doc_id, subjekt, beziehung, objekt));
"""

# Fakten-Ausschluss (Modul-17-Mensch-Tor, Feinkonzept F123): ein NICHT-DESTRUKTIVER Marker in der HEIMAT der
# Fakten (analog `documents.dup_of`) — nie Löschen. Der Vorwärts-Signalpfad `lade_fakten` filtert ihn PIT-sicher
# (aktiv ∧ t_disclosed ≤ Stichtag). `fakt_id` = `facts.id`. Reversibel (aktiv=0).
SCHEMA_FAKT_AUSSCHLUSS = """
CREATE TABLE IF NOT EXISTS fakt_ausschluss (
  fakt_id INTEGER PRIMARY KEY, grund TEXT NOT NULL, akteur TEXT,
  t_disclosed TEXT, t_ingest TEXT DEFAULT (datetime('now')), aktiv INTEGER DEFAULT 1);
"""

# Persistierte STRUKTURIERTE Kapital-Ereignisse (Modul-11-`kapital_roh`, funding-in-Modul-11, 07.08.). Form D
# ist strukturiert (Firma/Betrag/Branche), keine SBO-Prosa → gehört NICHT in die 1c-Textextraktion, sondern
# als Kapital-Ereignis in Modul 11. Der Sammler/Backfill (async-Schicht, Live-Fetch erlaubt) schreibt die
# strukturierten Felder HIERHER; der Rechenpfad (`betrieb_lauf`, cache-only, Guardrail 6) liest sie PIT-sicher.
# Analog `documents`: bitemporal (`t_disclosed`=filingDate=Wissbarkeits-Wand, `t_ingest`=Abrufzeit). `kat_id`
# aus der SIC/CIK→GIC-Map; ein Record ohne Kategorie-Treffer wird gar nicht erst geschrieben (kein Default).
# UNIQUE (Fable-QS Major-4): `cik`+`entity` NICHT-NULL-normalisiert (schreibe_kapital speichert '' statt NULL —
# SQLite behandelt NULLs als distinkt, sonst dedupliziert ein Heim-Doc mit cik=NULL nie). `entity` im Schlüssel,
# weil viele Form-D-Filings keinen CIK tragen (h_edgar url=None) — Firma+Kategorie+Tag ist der stabile Dedup-Key.
SCHEMA_KAPITAL_ROH = """
CREATE TABLE IF NOT EXISTS kapital_roh (
  id INTEGER PRIMARY KEY, kat_id TEXT NOT NULL, version INTEGER NOT NULL,
  art TEXT, richtung TEXT, commitment_stufe TEXT,
  betrag_numerisch REAL, betrag_klasse_ordinal TEXT, kapital_intransparent INTEGER DEFAULT 0,
  cik TEXT DEFAULT '', sic TEXT, entity TEXT DEFAULT '', quelle TEXT,
  t_event TEXT, t_disclosed TEXT NOT NULL, t_ingest TEXT DEFAULT (datetime('now')),
  UNIQUE(kat_id, version, cik, entity, t_disclosed));
"""

# modus/signalart-Vokabular des Scrapers (kategorial, 3.12-konform — wird durchgereicht).
_MODUS = {"ist", "wird"}
_SIGNALART = {"technologie", "ereignis"}
_LATENZ = {"kurz", "mittel", "lang"}


def quellentyp(source_type):
    """source_type des Sammlers -> Quellentyp (paper/patent/funding/news)."""
    return _QUELLENTYP.get((source_type or "").strip().lower(), "news")


# ==================================================================
#  WRITE-Pfad: kompatibler Cloud-Sammellauf -> scraper.db-Schema
#  (Jens 30.07.: die Cloud-Sammlung MUSS mit der Heim-scraper.db mergebar sein).
# ==================================================================
# Geschrieben wird EXAKT SCHEMA_DOCUMENTS/SCHEMA_FACTS (Auszug aus scraper.py) mit denselben
# UNIQUE-Schlüsseln. Backward-kompatibel: die Kategorie-Klassifikation (fact_kategorie/kategorie_version,
# die wir zuletzt geändert haben) ist PIPELINE-/aufzeichnung.db-intern und wird NICHT in scraper.db
# gemerged — scraper.db bleibt beim ursprünglichen documents/facts-Schema.
#
# ⚠ MERGE-ANLEITUNG (Claude-QS MAJOR-1 — NUR documents mergen, NIE facts per SELECT):
#   ATTACH 'sammel_cloud.db' AS cloud;
#   INSERT OR IGNORE INTO documents(source_type,title,text,url,relevance,trust,published_at,ingested_at)
#     SELECT source_type,title,text,url,relevance,trust,published_at,ingested_at FROM cloud.documents;
#   -- KEIN facts-SELECT-Merge! facts.doc_id UND documents.dup_of sind ID-RAUM-RELATIV; ein blinder
#   -- `INSERT … facts SELECT doc_id …` verlinkt die Fakten nach dem id-Remap auf FALSCHE/tote Dokumente.
#   -- Richtig: das HEIM-1c extrahiert die Fakten aus den neu gemergten Dokumenten (facts_done trackt sie).
#   -- Erst wenn die Cloud je selbst Fakten produziert, braucht der Merge eine alt→neu-id-Map über
#   -- (title, published_at). Auch die id/source_id-Spalten werden bewusst NICHT mitkopiert (Heim vergibt neu).

def normalisiere_source_type(conn):
    """Idempotente Migration (Jens 30.07., EINE Dedup-Definition): richtet eine Alt-Sammel-DB auf die
    geteilte Definition aus. Zwei Schritte:
    (1) legacy-`documents.source_type` in der QUELL-Konvention (arxiv/edgar/epo) auf die HEIM-Reifegrad-
        Konvention (paper/patent/funding/news) heben — die geteilte Dedup BLOCKT nach source_type und der
        Merge erwartet das Heim-Schema. Nur Rohlabels ≠ Ziel; normalisierte Zeilen bleiben unberührt.
    (2) Cross-Sprosse-`dup_of` aus der ALTEN Cloud-Dedup (ohne source_type-Block) zurücknehmen: zeigt ein
        dup_of auf ein Dokument ANDERER Reifegrad-Sprosse, verletzt das die geteilte Definition (ein Patent
        als Dup des Papers = Ketten-Kollaps) → dup_of=NULL, das Dokument kehrt in die Pipeline zurück
        (nicht-destruktiv — es war nie gelöscht). Reihenfolge zählt: erst relabeln, dann prüfen.
    (Deckt die vor dieser Änderung von Drive restaurierten Alt-Stände ab.)"""
    if not _dedup_spalte_vorhanden(conn):
        return
    for roh, ziel in _QUELLENTYP.items():
        if roh != ziel:
            conn.execute("UPDATE documents SET source_type=? WHERE source_type=?", (ziel, roh))
    conn.execute(
        "UPDATE documents SET dup_of=NULL WHERE dup_of IS NOT NULL AND EXISTS ("
        " SELECT 1 FROM documents k WHERE k.id=documents.dup_of "
        " AND k.source_type IS NOT documents.source_type)")
    conn.commit()


def oeffne_sammel_db(db_pfad):
    """Öffnet/erstellt eine SEPARATE Sammel-DB im scraper.db-Schema (documents + facts).
    -> sqlite3.Connection. Idempotent (CREATE IF NOT EXISTS). **Kein WAL-Flip** (Claude-QS MINOR-3:
    PRAGMA journal_mode=WAL wäre eine PERSISTENTE Modusänderung — zeigte `db_pfad` je auf die echte
    scraper.db [die einzige Kopie], würde sie umgestellt). `db_pfad` MUSS eine eigene Datei sein, NICHT
    die Heim-scraper.db."""
    conn = sqlite3.connect(db_pfad)
    conn.executescript(SCHEMA_DOCUMENTS + SCHEMA_FACTS)
    conn.commit()
    normalisiere_source_type(conn)                    # Alt-Stände auf die Heim-Konvention heben (idempotent)
    return conn


def schreibe_dokument(conn, doc):
    """Ein Roh-Dokument (source_type/title/text/url/published_at [+ optional relevance/trust/source_id])
    -> `documents` (scraper-Schema), INSERT OR IGNORE über UNIQUE(title, published_at). `published_at`
    ist NOT NULL -> ohne Datum wird übersprungen (fail-closed, wie der Heim-Reader es erwartet).
    relevance/trust default NULL (die Cloud hat die scraper-Filterkaskade nicht; NULL = ehrliche
    Herkunft, schema-kompatibel). ⚠ Claude-QS MINOR-1: ein Heim-Konsument, der `relevance >= x` filtert,
    schließt NULL-relevance-Docs STILL aus (`NULL >= x` ist NULL/falsch). Nach dem Merge müssen die
    Cloud-Docs also entweder als „unbewertet" behandelt oder ihre relevance vom Heim-1c nachgetragen
    werden — NICHT eine relevance erfinden (das wäre ein Schein-Wert). -> documents.id (neu/bestehend)."""
    titel = (doc.get("title") or "").strip()
    pub = (doc.get("published_at") or "").strip()
    if not titel or not pub:
        return None
    # source_type in der HEIM-Reifegrad-Konvention speichern (paper/patent/funding/news) — Jens 30.07.:
    # EINE Dedup-Definition + merge-kompatibel. Die geteilte Dedup blockt nach source_type; würde die
    # Cloud roh 'arxiv' speichern, verglichen die Heim-Dedup die eingemergten Docs NICHT gegen die
    # Heim-'paper'-Docs (stiller Cross-DB-Fehlschlag). `quellentyp` ist die EINE Abbildung.
    roh_st = (doc.get("source_type") or "").strip().lower()
    st = quellentyp(doc.get("source_type"))
    if roh_st and roh_st not in _QUELLENTYP:            # QS-#2 (Claude): nicht STILL auf 'news' (Konsens-
        print(f"  ⚠ unbekannter source_type {doc.get('source_type')!r} -> '{st}' "  # Sprosse = schlechteste
              f"(nicht in _QUELLENTYP; ggf. Mapping ergänzen)")                      # Fehlklasse für Alpha, §4)
    conn.execute(
        "INSERT OR IGNORE INTO documents(source_id,source_type,title,text,url,relevance,trust,"
        "published_at,ingested_at,dup_of) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (doc.get("source_id"), st, titel, doc.get("text"), doc.get("url"),
         doc.get("relevance"), doc.get("trust"), pub, doc.get("ingested_at"), doc.get("dup_of")))
    row = conn.execute("SELECT id FROM documents WHERE title=? AND published_at=?",
                       (titel, pub)).fetchone()
    return row[0] if row else None


def markiere_near_dups(conn, embed_fn, schwelle=0.93, fenster_tage=21, batch=32):
    """Semantisches LIKE-Dedup (Jens 30.07.): Near-Duplikate über Embedding-Kosinus markieren — NON-
    DESTRUKTIV via `documents.dup_of` (das kanonische = ÄLTERE Dokument, kleinere id). Nutzt die GETEILTE
    Definition `dedup_kern.beste_uebereinstimmung` (Heim = Cloud, keine Insel); `embed_fn(texte)->[[float]]`
    injizierbar (Cloud: `hf_embedding.embed_fn`, Test: Fake). `schwelle` konservativ 0.93 (Claude-QS M2:
    MiniLM-Domänen-Text hat hohe Grund-Ähnlichkeit → 0.90 = Über-Dedup-Risiko; unkalibriert, pro Modell).

    Die Embeddings liegen in einer CLOUD-INTERNEN `doc_embedding`-Tabelle (NICHT scraper-gemerged — der
    Merge betrifft nur documents/facts). **Blocking (geteilte Politik):** verglichen wird nur gegen frühere
    Docs DERSELBEN `source_type`-Reifegrad-Sprosse (paper/patent/funding/news — konzept-kritisch) im
    published_at-Zeitfenster (±`fenster_tage`, Default `dedup_kern.FENSTER_TAGE`=21) — sonst O(n²). Der
    Kanon ist der ähnlichste (Best-Match, nicht First-Match).

    Claude-QS M3 (bounded storage, für Jens' „Masse"-Ziel): die Vektor-Tabelle würde sonst unbegrenzt
    wachsen und bei jedem Drive-Sync komplett neu hochgeladen. Zwei Mechanismen: (1) ein PERMANENTER
    `doc_embedding_done`-Marker (nur die id) verhindert Re-Embedding schon verarbeiteter Docs auch nach dem
    Prune; (2) nach dem Lauf werden Vektoren außerhalb des Blocking-Fensters (>2·`fenster_tage` älter als
    das jüngste published_at) AUSGERÄUMT — sie könnten nie mehr Kandidat für ein neues Doc werden. Der
    Commit läuft PRO BATCH (idempotent-resumierbar; ein Mid-Run-Fehler wirft die bezahlten HF-Embeddings der
    vorherigen Batches nicht weg). -> Anzahl neu als dup markierte Docs."""
    import json as _j
    from dedup_kern import beste_uebereinstimmung          # die EINE Dedup-Entscheidung (Heim = Cloud)
    conn.execute("CREATE TABLE IF NOT EXISTS doc_embedding (doc_id INTEGER PRIMARY KEY, vec TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS doc_embedding_done (doc_id INTEGER PRIMARY KEY)")
    rows = conn.execute(
        "SELECT d.id, d.source_type, d.title, d.text, d.published_at FROM documents d "
        "WHERE d.id NOT IN (SELECT doc_id FROM doc_embedding_done) ORDER BY d.id").fetchall()
    markiert = 0
    for i in range(0, len(rows), batch):
        teil = rows[i:i + batch]
        vecs = embed_fn([f"{(r[2] or '').strip()}. {(r[3] or '').strip()}" for r in teil])
        for (did, st, _titel, _text, pub), vec in zip(teil, vecs):
            conn.execute("INSERT OR REPLACE INTO doc_embedding(doc_id, vec) VALUES(?,?)",
                         (did, _j.dumps(vec)))
            conn.execute("INSERT OR IGNORE INTO doc_embedding_done(doc_id) VALUES(?)", (did,))
            # Blocking (geteilte Definition): frühere, nicht-dup Docs DERSELBEN source_type-Sprosse im
            # Zeitfenster. Das source_type=? ist KONZEPT-KRITISCH (nie ein Patent als Dup des Papers →
            # Kette Paper→Patent→Funding→News nicht kollabieren); newest-first für stabilen Best-Match-Tiebreak.
            kand = conn.execute(
                "SELECT e.doc_id, e.vec FROM doc_embedding e JOIN documents d2 ON d2.id=e.doc_id "
                "WHERE e.doc_id < ? AND d2.dup_of IS NULL AND d2.source_type = ? "
                "AND ABS(julianday(d2.published_at) - julianday(?)) <= ? ORDER BY e.doc_id DESC",
                (did, st, pub, fenster_tage)).fetchall()
            kanon = beste_uebereinstimmung(vec, [(kid, _j.loads(kvec)) for kid, kvec in kand], schwelle)
            if kanon is not None:
                conn.execute("UPDATE documents SET dup_of=? WHERE id=?", (kanon, did))
                markiert += 1
        conn.commit()                               # M3: pro Batch committen (resumierbar; keine Quota-Verschwendung)
    # M3: Vektoren außerhalb des Blocking-Fensters ausräumen (bounded size; der done-Marker bleibt).
    conn.execute(
        "DELETE FROM doc_embedding WHERE doc_id IN ("
        " SELECT e.doc_id FROM doc_embedding e JOIN documents d ON d.id=e.doc_id "
        " WHERE (SELECT julianday(MAX(published_at)) FROM documents) - julianday(d.published_at) > ?)",
        (2 * fenster_tage,))
    conn.commit()
    return markiert


def schreibe_fakt(conn, fakt, doc_id=None):
    """Ein 1c-SBO-Fakt (subjekt/beziehung/objekt [+ modus/signalart/reife/latenz/…]) -> `facts`
    (scraper-Schema), INSERT OR IGNORE über UNIQUE(doc_id, subjekt, beziehung, objekt). Nur mit
    nicht-leerem Subjekt (wie der Heim-Reader filtert). -> True, wenn geschrieben/vorhanden."""
    subj = (fakt.get("subjekt") or "").strip()
    if not subj:
        return False
    did = doc_id if doc_id is not None else fakt.get("doc_id")
    conn.execute(
        "INSERT OR IGNORE INTO facts(doc_id,source_type,subjekt,beziehung,objekt,modus,signalart,"
        "reife,reife_score,latenz,erwartungstempo,konfidenz,published_at,ingested_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, fakt.get("source_type"), subj, (fakt.get("beziehung") or "").strip(),
         (fakt.get("objekt") or "").strip(), fakt.get("modus"), fakt.get("signalart"),
         fakt.get("reife"), fakt.get("reife_score"), fakt.get("latenz"),
         fakt.get("erwartungstempo"), fakt.get("konfidenz"), fakt.get("published_at"),
         fakt.get("ingested_at")))
    return True


# Rechtsform-/Sammelbegriff-Suffixe (nur am ENDE, Token-basiert) — konservativ,
# damit kein bedeutungstragendes Wort wegfällt.
_SUFFIX_WORDS = {"inc", "incorporated", "corp", "corporation", "company", "co", "ltd",
                 "limited", "llc", "plc", "gmbh", "ag", "se", "sa", "nv", "spa",
                 "holdings", "holding", "group"}


def _strip_suffix(name):
    """Rechtsform-Suffixe am ENDE strippen ('… Holdings Inc.'), Token-basiert.
    QS-B3: bleibt nach dem Strippen NUR ein generisches Stopwort übrig
    ('Group Inc' -> 'Group'), wird der ORIGINALNAME behalten — sonst kollabierten
    alle '…Group'-Firmen auf denselben synthetischen Knoten."""
    roh = re.sub(r"\s+", " ", (name or "").strip())
    toks = roh.split()
    out = list(toks)
    while len(out) > 1 and out[-1].lower().strip(".,") in _SUFFIX_WORDS:
        out.pop()
    cleaned = " ".join(out).strip(" ,.")
    # nur noch ein Stopwort (oder leer) übrig -> Original behalten (keine Falsch-Fusion)
    if not cleaned or (len(out) == 1 and out[0].lower().strip(".,") in _SUFFIX_WORDS):
        return roh
    return cleaned


def kanonisiere_entitaet(name, alias_map=None):
    """Entity-Resolution (leichtgewichtig): Rechtsform-Suffixe strippen, Whitespace/
    Casing normalisieren -> Firmen-Varianten fallen auf EINEN Knoten zusammen.
    Optionale alias_map {surface_lower: kanonisch} löst bekannte Synonyme
    (z. B. {'tsmc': 'Taiwan Semiconductor'}). Reine Textfunktion, read-only.
    Gibt die kanonische Anzeigeform zurück (leerer Eingang -> '').

    QS-Grenze (bekannt, akzeptiert): trägt eine Marke die Rechtsform INTRINSISCH
    ('News Corp' -> 'News'), wird sie mit-gestrippt. Nicht-destruktiv (nur Knoten-
    Fusion, kein Datenverlust); für solche Fälle die alias_map setzen
    (z. B. {'news corp': 'News Corp'})."""
    roh = re.sub(r"\s+", " ", (name or "").strip())
    if not roh:
        return ""
    if alias_map:
        treffer = alias_map.get(roh.lower())
        if treffer:
            return treffer
    kern = _strip_suffix(roh)
    if alias_map:
        treffer = alias_map.get(kern.lower())
        if treffer:
            return treffer
    return kern


def _dedup_spalte_vorhanden(conn):
    """True, wenn die DB die Semantik-Dedup-Spalte documents.dup_of trägt (neue
    Sammler-DB). Alte DBs / reine facts-Test-DBs -> False (Filter wird ausgelassen)."""
    try:
        hat_tabelle = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if not hat_tabelle:
            return False
        cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
        return "dup_of" in cols
    except sqlite3.Error:
        return False


def lade_fakten(db_pfad, limit=None, seit=None, stichtag=None, alias_map=None, pit_published=None):
    """🔑 facts (1c-Extraktion des Scrapers) -> v0-`facts`-Eingang von Modul 2 (je 1c-Fakt eine Zeile).

    DIE saubere Naht: Modul 2 bekommt die EXTRAHIERTE Subjekt-Beziehung-Objekt-Aussage und
    kategorisiert sie — nicht rohen Text (das ist `lade_dokumente`, ein Bypass). `modus`/`signalart`/
    `latenz` (kategorial/ordinal) bleiben erhalten; die Dezimal-Urteile des Scrapers
    (reife_score/erwartungstempo/konfidenz) werden BEWUSST verworfen (3.12: keine LLM-Dezimalen —
    Reifegrad aus der Quellentyp-Leiter/Modul 5, Tempo aus 2c, Konfidenz aus dem 2d-Ensemble).

    - `seit`:     nur published_at >= seit (ISO).
    - `stichtag`: OPERATIVES PIT (Vorwärts-Uhr) — nur am Stichtag von UNS SCHON GESAMMELTE Fakten
      (ingested_at < stichtag), kein Look-ahead auf noch nicht ingestierte Docs.
    - `pit_published`: INFORMATIONS-PIT (Retro über den Bestands-Korpus) — nur Fakten, deren Dokument am
      Stichtag PUBLIC verfügbar WAR (published_at <= pit_published). Für den Retro-Test „hätten wir damals
      gesammelt": die Doc-Existenz ist ein hartes Faktum ≤ T; die spätere Ingestion (ingested_at ≈ heute)
      darf den Retro NICHT leeren (sonst kennt man an jedem alten T nichts). Die semantische Extraktion nutzt
      trotzdem das heutige Modell — leck-frei auf der Signal-ZEIT, vintage-behaftet auf der Neuheits-Achse
      (die der Retro-Lauf weglässt). NIE zusammen mit `stichtag` setzen (zwei widersprüchliche PIT-Begriffe).
    Read-only. -> Liste v0-`facts`-Zeilen (fact_id/subjekt/beziehung/objekt/quellentyp/modus/
    signalart/latenz/rolle/t_event/t_disclosed/t_ingest)."""
    conn = sqlite3.connect(db_pfad)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT f.id, f.doc_id, f.source_type, f.subjekt, f.beziehung, f.objekt, f.modus, "
               "f.signalart, f.latenz, f.published_at, f.ingested_at FROM facts f "
               "WHERE f.subjekt IS NOT NULL AND TRIM(f.subjekt) != ''")
        args = []
        if seit:
            sql += " AND f.published_at >= ?"; args.append(seit)
        if stichtag:
            sql += " AND f.ingested_at < ?"; args.append(stichtag)     # operatives PIT: am Stichtag gesammelt
        if pit_published:
            sql += " AND f.published_at <= ?"; args.append(pit_published)   # Informations-PIT: damals public
        _as_of = stichtag or pit_published                             # der wirksame Retro-Stichtag (Ausschluss)
        if _dedup_spalte_vorhanden(conn):
            # Semantik-Dedup respektieren: Fakten aus Near-Dup-Dokumenten raus.
            # QS-Minor: doc_id IS NULL explizit BEHALTEN (sonst schluckt NULL NOT IN
            # den Fakt lautlos — Verhalten würde je nach Schema-Version abweichen).
            # Gemini-QS B1: NOT EXISTS statt NOT IN (NULL-robust; f.doc_id IS NULL bleibt erhalten)
            sql += (" AND NOT EXISTS (SELECT 1 FROM documents dd "
                    "WHERE dd.id=f.doc_id AND dd.dup_of IS NOT NULL)")
        if _ausschluss_tabelle_vorhanden(conn):
            # Fakten-Ausschluss (F123) PIT-sicher fail-closed: im Vorwärts-Lauf (stichtag=None) wirken alle
            # aktiven Marker; im Retro (stichtag gesetzt) NUR ein Marker mit bekanntem t_disclosed ≤ Stichtag
            # (ein datumsloser Marker wirkt retro NICHT zurück — kein Look-Ahead).
            sql += (" AND NOT EXISTS (SELECT 1 FROM fakt_ausschluss fa WHERE fa.fakt_id=f.id AND fa.aktiv=1 "
                    "AND (? IS NULL OR (fa.t_disclosed IS NOT NULL AND fa.t_disclosed <= ?)))")
            args += [_as_of, _as_of]
        sql += " ORDER BY f.published_at"
        if limit:
            sql += " LIMIT ?"; args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        pub = r["published_at"]
        modus = r["modus"] if r["modus"] in _MODUS else None
        signalart = r["signalart"] if r["signalart"] in _SIGNALART else None
        latenz = r["latenz"] if r["latenz"] in _LATENZ else None
        out.append({
            "fact_id": f"fakt{r['id']}",
            "doc_id": r["doc_id"],                   # QS-G1: für die Unabhängigkeits-Zählung (Modul 3 §3.2)
            "subjekt": kanonisiere_entitaet(r["subjekt"], alias_map),
            "beziehung": (r["beziehung"] or "").strip(),
            "objekt": kanonisiere_entitaet(r["objekt"], alias_map),
            "quellentyp": quellentyp(r["source_type"]),
            "modus": modus, "signalart": signalart, "latenz": latenz,   # kategorial/ordinal (3.12-OK)
            "rolle": None,                          # v0-Passthrough (kein LLM-Dezimal-Urteil)
            "t_event": pub, "t_disclosed": pub,     # Offenlegung
            "t_ingest": r["ingested_at"] or pub,    # Wissenszeit
        })
    return out


# ------------------------------------------------------------------ #
# Kapital-Ereignisse (Modul-11-`kapital_roh`) — persistiert vom Sammler/Backfill, gelesen vom Rechenpfad
# ------------------------------------------------------------------ #
def schreibe_kapital(conn, roh):
    """Ein strukturiertes Kapital-Ereignis (Form D → `zu_kapital_roh`-Format) idempotent in `kapital_roh`.
    Erwartet: kat_id/version/commitment_stufe/t_disclosed (Pflicht) + art/richtung/betrag_numerisch/cik/sic/
    entity/t_event/quelle (optional). Ohne kat_id/version/t_disclosed wird NICHT geschrieben (kein Default-
    Filling — genau wie `zu_kapital_roh` einen Record ohne Taxonomie-Treffer überspringt). -> 1 geschrieben, 0 sonst."""
    conn.execute(SCHEMA_KAPITAL_ROH)
    if not (roh.get("kat_id") and roh.get("version") is not None and roh.get("t_disclosed")):
        return 0
    # t_ingest: der HISTORISCHE Ingest-Zeitpunkt, falls mitgegeben (Backfill reicht documents.ingested_at durch,
    # Gemini-B2) — sonst der Schema-Default datetime('now'). NIE in die Zukunft geraten.
    hat_ti = bool(roh.get("t_ingest"))
    spalten = ("kat_id,version,art,richtung,commitment_stufe,betrag_numerisch,betrag_klasse_ordinal,"
               "kapital_intransparent,cik,sic,entity,quelle,t_event,t_disclosed" + (",t_ingest" if hat_ti else ""))
    platz = "?,?,?,?,?,?,?,?,?,?,?,?,?,?" + (",?" if hat_ti else "")
    werte = [roh["kat_id"], int(roh["version"]), roh.get("art", "funding"), roh.get("richtung", "zufluss"),
             roh.get("commitment_stufe", "committed"),
             float(roh["betrag_numerisch"]) if roh.get("betrag_numerisch") is not None else None,
             roh.get("betrag_klasse_ordinal"), 1 if roh.get("kapital_intransparent") else 0,
             (roh.get("cik") or ""), roh.get("sic"), (roh.get("entity") or roh.get("text") or ""),   # Major-4: '' statt NULL
             roh.get("quelle", "edgar_form_d"), roh.get("t_event"), roh["t_disclosed"]]
    if hat_ti:
        werte.append(roh["t_ingest"])
    conn.execute(f"INSERT OR IGNORE INTO kapital_roh({spalten}) VALUES({platz})", werte)
    return 1


def lade_kapital(db_pfad, stichtag=None, pit_published=None):
    """🔑 `kapital_roh` -> Modul-11-`make_kapital_fluss`-Eingang (funding-in-Modul-11). PIT wie `lade_fakten`:
    - `pit_published` (Informations-PIT, Retro): nur Ereignisse mit `t_disclosed <= pit_published` (die Publik-
      Wand — filingDate ist ein hartes Datum, kein Modell-Vintage → 100 % leckfrei, anders als die Semantik).
    - `stichtag` (operatives PIT, Vorwärts-Uhr): nur am Stichtag schon abgerufene Ereignisse (`t_ingest < stichtag`).
    Read-only, fail-safe (fehlt die Tabelle → leer). -> Liste kapital_roh-Zeilen für die Pipeline."""
    try:
        conn = sqlite3.connect("file:" + os.path.abspath(db_pfad).replace("\\", "/") + "?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kapital_roh'").fetchone():
            return []
        sql = ("SELECT kat_id,version,art,richtung,commitment_stufe,betrag_numerisch,betrag_klasse_ordinal,"
               "kapital_intransparent,cik,sic,entity,t_event,t_disclosed,t_ingest FROM kapital_roh WHERE 1=1")
        args = []
        if pit_published:
            sql += " AND t_disclosed <= ?"; args.append(pit_published)
        if stichtag:
            sql += " AND t_ingest < ?"; args.append(stichtag)
        out = []
        for r in conn.execute(sql, args).fetchall():
            out.append({"kat_id": r["kat_id"], "version": r["version"], "art": r["art"],
                        "richtung": r["richtung"], "commitment_stufe": r["commitment_stufe"],
                        "betrag_numerisch": r["betrag_numerisch"], "betrag_klasse_ordinal": r["betrag_klasse_ordinal"],
                        "kapital_intransparent": bool(r["kapital_intransparent"]), "text": r["entity"] or "",
                        "t_event": r["t_event"] or r["t_disclosed"], "t_disclosed": r["t_disclosed"],
                        "t_ingest": r["t_ingest"] or r["t_disclosed"]})
        return out
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# Fakten-Ausschluss (Modul-17-Mensch-Tor, F123) — Marker in der Fakten-Heimat, PIT, reversibel
# ------------------------------------------------------------------ #
def _ausschluss_tabelle_vorhanden(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fakt_ausschluss'").fetchone() \
        is not None


def _fakt_id_int(fakt_id):
    """'fakt123' ODER 123 -> 123 (die GUI zeigt 'faktN', die Heimat-Tabelle key't auf facts.id)."""
    if isinstance(fakt_id, int):
        return fakt_id
    s = str(fakt_id).strip()
    return int(s[4:]) if s.lower().startswith("fakt") else int(s)


def schliesse_fakt_aus(db_pfad, fakt_id, grund, akteur="jens", t_disclosed=None, aktiv=1):
    """Einen Fakt (nicht-destruktiv) aus-/wieder-einschließen (F123). **Begründungspflicht** (leerer Grund →
    Fehler, wie das Mensch-Tor override/veto). `t_disclosed` = Ausschluss-Zeitpunkt (PIT); None = Vorwärts-only.
    Reversibel über `aktiv=0`. Idempotent per fakt_id (PRIMARY KEY)."""
    if not (grund or "").strip():
        raise ValueError("Fakten-Ausschluss verlangt eine Begründung (F123, Forking-Path-Disziplin)")
    conn = sqlite3.connect(db_pfad)
    try:
        conn.executescript(SCHEMA_FAKT_AUSSCHLUSS)
        conn.execute(
            "INSERT INTO fakt_ausschluss(fakt_id,grund,akteur,t_disclosed,aktiv) VALUES(?,?,?,?,?) "
            "ON CONFLICT(fakt_id) DO UPDATE SET grund=excluded.grund, akteur=excluded.akteur, "
            "t_disclosed=excluded.t_disclosed, aktiv=excluded.aktiv",
            (_fakt_id_int(fakt_id), grund.strip(), akteur, t_disclosed, int(aktiv)))
        conn.commit()
    finally:
        conn.close()


def lies_fakt_ausschluesse(db_pfad, nur_aktiv=True):
    """-> [{'fakt_id','grund','akteur','t_disclosed','aktiv'}] für die Provenienz-Anzeige."""
    conn = sqlite3.connect(db_pfad)
    try:
        if not _ausschluss_tabelle_vorhanden(conn):
            return []
        sql = "SELECT fakt_id,grund,akteur,t_disclosed,aktiv FROM fakt_ausschluss"
        if nur_aktiv:
            sql += " WHERE aktiv=1"
        sql += " ORDER BY fakt_id"
        return [{"fakt_id": r[0], "grund": r[1], "akteur": r[2], "t_disclosed": r[3], "aktiv": r[4]}
                for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def ausschluss_hash(db_pfad):
    """Stabiler Hash des AKTIVEN Ausschluss-Satzes — in jede Retro-KALIBRIER-Zeile gestempelt (F123/Claude-#6:
    Reproduzierbarkeit; toggeln → Retro neu → anderer Lift wird nachvollziehbar). Leerer Satz -> feste Kennung."""
    import hashlib
    aktive = lies_fakt_ausschluesse(db_pfad, nur_aktiv=True)
    if not aktive:
        return "leer"
    roh = ";".join(f"{a['fakt_id']}:{a['t_disclosed'] or ''}" for a in sorted(aktive, key=lambda a: a["fakt_id"]))
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()[:16]


def lade_dokumente(db_pfad, limit=None, seit=None, min_relevanz=None, neueste_zuerst=False):
    """documents (Sammler) -> Modul-2-`facts`-Eingang (je Dokument eine Zeile). BYPASS von 1c:
    subjekt = Roh-Volltext (kein SBO), Modul 2 klassifiziert grob den ganzen Text. Nur für Bestände
    OHNE extrahierte Fakten oder als Doku-Ebene; der echte Pfad ist `lade_fakten`.
    seit: nur published_at >= seit (ISO). min_relevanz: nur relevance >= Schwelle.
    `neueste_zuerst` (Claude-QS MAJOR-2): DESC statt ASC — sonst greift ein `limit` immer die ÄLTESTEN
    Dokumente (der Forward-Kandidaten-Pass würde dieselben veralteten Docs wiederholen). Read-only."""
    conn = sqlite3.connect(db_pfad)
    conn.row_factory = sqlite3.Row
    try:
        # dup_of IS NULL: semantische Near-Dups (markiere_near_dups) aus dem Bypass-Pfad ausschließen
        # (symmetrisch zu lade_fakten; sonst kategorisierte das Ensemble Near-Duplikate mit).
        sql = ("SELECT id, source_type, title, text, published_at, ingested_at, relevance "
               "FROM documents WHERE published_at IS NOT NULL AND dup_of IS NULL")
        args = []
        if seit:
            sql += " AND published_at >= ?"; args.append(seit)
        if min_relevanz is not None:
            sql += " AND relevance >= ?"; args.append(min_relevanz)
        sql += " ORDER BY published_at DESC" if neueste_zuerst else " ORDER BY published_at"
        if limit:
            sql += " LIMIT ?"; args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        titel = (r["title"] or "").strip()
        text = (r["text"] or "").strip()
        volltext = (f"{titel}. {text}" if titel else text)[:_MAX_TEXT]
        pub = r["published_at"]
        out.append({
            "fact_id": f"doc{r['id']}",
            "subjekt": volltext, "beziehung": "", "objekt": "",
            "quellentyp": quellentyp(r["source_type"]),
            "rolle": "technologie",
            "t_event": pub, "t_disclosed": pub,       # Dokument: Offenlegungsdatum = published_at
            "t_ingest": r["ingested_at"] or pub,       # Wissenszeit
        })
    return out


# ==================================================================
#  TAXONOMIE-BRÜCKE: Modul-1-Ontologie (themes) -> Modul-2-Kategorien
#  (open-set Selbst-Ergänzung + Graduierung in den LoRA-Kern; Jens 28.07.)
# ==================================================================
# Modul 1 LÄSST DIE ONTOLOGIE WACHSEN (Scraper) — die Kategorie-Menge ist ein wachsender Strom, kein
# Enum. Ein wachsender Raum braucht einen OPEN-SET-Kategorisierer: der Embedding-Anker-Weg trägt den
# Rand (neues Thema = neuer Anker, kein Retrain), LoRA schärft nur den gereiften Kern (Graduierung).
# Diese Brücke ist die EINE Quelle des Signal-Vokabulars — kein Insel-Enum in Modul 2.

def lade_themen(db_pfad):
    """Modul-1-Ontologie -> Themen-dicts {name, keywords:[…], created_by, n_belege}. `themes` (nicht
    excluded) ⋈ Beleg-Zählung aus `doc_themes`. Die keywords SIND die fertigen Kategorie-Anker.
    Read-only; home gegen die echte scraper.db, offline gegen eine Test-DB."""
    conn = sqlite3.connect(db_pfad)
    conn.row_factory = sqlite3.Row
    try:
        # Fable-QS M-1: der Beleg-Count MUSS die Semantik-Dedup respektieren — sonst blähen Near-Dups
        # (documents.dup_of NOT NULL) `n_belege` auf und graduieren ein Thema, das faktisch aus einer
        # dedupten Handvoll Dokumente besteht (dieselbe Filter-Invariante wie lade_fakten/lade_dokumente).
        dt_join = "LEFT JOIN doc_themes dt ON dt.theme_id=th.id"
        if _dedup_spalte_vorhanden(conn):
            dt_join += " AND dt.doc_id NOT IN (SELECT id FROM documents WHERE dup_of IS NOT NULL)"
        rows = conn.execute(
            "SELECT th.name, th.keywords, th.created_by, th.created_at, COUNT(dt.doc_id) AS n_belege "
            f"FROM themes th {dt_join} "
            "WHERE th.excluded=0 GROUP BY th.id ORDER BY th.name").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            kws = json.loads(r["keywords"]) if r["keywords"] else []
        except (json.JSONDecodeError, TypeError):
            kws = []
        out.append({"name": r["name"], "keywords": [k for k in kws if (k or "").strip()],
                    "created_by": r["created_by"] or "seed", "n_belege": int(r["n_belege"] or 0),
                    "created_at": (r["created_at"] if "created_at" in r.keys() else None)})   # bitemporal t_valid_von
    return out


def anker_aus_themen(themen):
    """OPEN-SET-Kern: {kat_id: [name, *keywords]} — die Anker DIREKT aus der (wachsenden) Ontologie.
    Ein NEUES Thema in Modul 1 wird beim nächsten Lesen sofort zur Kategorie, ohne Retraining (das ist
    die Selbst-Ergänzung „wo nötig"). kat_id = Themenname. Speist `embedding_llm.baue_anker`-kompatibel."""
    anker = {}
    for t in themen:
        name = (t.get("name") or "").strip()
        if name:
            anker[name] = [name] + [k for k in t.get("keywords", []) if (k or "").strip()]
    return anker


def ist_graduiert(thema, min_belege=5, min_labels=0, label_counts=None):
    """Graduiert vom offenen Rand (nur Embedding-Anker) in den LoRA-geschärften KERN, wenn genug Evidenz
    UND (optional) genug Kategorie-Labels da sind. `label_counts`: {name: n} vom Aufrufer (Labeling-
    Akkumulation); None -> nur Evidenz. Bis dahin trägt das Thema der open-set Kategorisierer."""
    if thema.get("n_belege", 0) < min_belege:
        return False
    if min_labels:                                             # Gemini-QS B3: fail-closed — verlangt der
        lc = label_counts or {}                                # Aufrufer Labels, aber liefert keine, gilt 0
        return lc.get(thema.get("name"), 0) >= min_labels      # (kein stilles Überspringen der Hürde)
    return True


def graduierte_themen(themen, min_belege=5, min_labels=0, label_counts=None):
    """Der reife KERN = LoRA-Trainingskandidaten. Der unreife Rest bleibt open-set (Embedding-Anker)."""
    return [t for t in themen if ist_graduiert(t, min_belege, min_labels, label_counts)]


# B1-Auflösung (Jens 28.07., „voll an den Live-Kontrakt"): die Themen-Reife (Signal-Seite, LoRA-Graduierung)
# wird auf den KATEGORIE_VERSION-Lebenszyklus-Enum abgebildet. Ehrlich: das sind zwei Achsen (LoRA-Reife ≠
# ökonomischer Kategorie-Lebenszyklus) — Jens' Entscheid koppelt sie über diese feste Karte. Die reine
# Graduierungs-Info bleibt separat über die ist_graduiert-Helfer (der reife-Kern-Filter) für die LoRA-Wahl.
_REIFEGRAD_MAP = {"seed": "emerging", "gewachsen": "growing", "graduiert": "established"}
OFFEN_SENTINEL = "9999-12-31"


def themen_zu_kategorie_version(themen, rollup_map=None, min_belege=5, min_labels=0,
                                label_counts=None, version=1, ebene="technologie", stichtag=None):
    """themes -> KONTRAKT-VALIDE `kategorie_version`-Zeilen (Modul-2-Signal-Vokabular, 2a besitzt die
    Tabelle). B1-Auflösung: emittiert JETZT alle Pflichtfelder des `KATEGORIE_VERSION`-Kontrakts +
    `gic_rollup` (Zwei-Ebenen-Hybrid). `reifegrad` wird über `_REIFEGRAD_MAP` auf den Kontrakt-Enum
    (emerging/growing/established) abgebildet. `rollup_map` {name: GIC-Kategorie} bindet an die Outcome-
    Seite (fehlt -> gic_rollup=None, offener Rand, NICHT geraten). `ebene` = feinste Signal-Ebene (Themen
    sind fein → 'technologie'-Default). Bitemporal: `t_valid_von`/`t_ingest` = `created_at` des Themas
    (Wissbarkeits-Wand); fehlt es, greift `stichtag`. Ohne beides wird das Thema fail-closed ÜBERSPRUNGEN
    (kein bitemporales Vokabular ohne echte Zeit — kein erfundenes Datum). -> Liste, direkt an
    `contracts.validate(rows, KATEGORIE_VERSION)`/`embedding_llm.baue_anker` verfütterbar."""
    rollup_map = rollup_map or {}
    out = []
    for t in themen:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        zeit = (t.get("created_at") or stichtag)
        if not zeit:
            continue                                           # fail-closed: keine echte Zeit -> kein Eintrag
        grad = ist_graduiert(t, min_belege, min_labels, label_counts)
        theme_reife = "graduiert" if grad else ("seed" if t.get("created_by") == "seed" else "gewachsen")
        out.append({
            "kat_id": name, "version": version, "ebene": ebene, "name": name,
            "aliase": [k for k in t.get("keywords", []) if (k or "").strip()],
            "vorgaenger": [], "nachfolger": [],                # frisches Thema: keine Merge/Split-Lineage
            "status_vokabular": "aktiv",
            "reifegrad": _REIFEGRAD_MAP.get(theme_reife, "emerging"),   # Gemini-QS B2: Fallback statt KeyError
            "gic_rollup": rollup_map.get(name),
            "t_valid_von": zeit, "t_valid_bis": OFFEN_SENTINEL, "t_ingest": zeit,
        })
    return out
