# Architektur — Chronos-Stream Dateninfrastruktur

Dieses Dokument beschreibt die Architektur der offenen Dateninfrastruktur in Diagrammen. Alle
Diagramme sind in [Mermaid](https://mermaid.js.org/) geschrieben und rendern direkt auf GitHub.

Der Aufbau folgt vier Grundprinzipien:

1. **Trennung von Beschaffung und Rechnen** — der Rechen-/Analysepfad ruft niemals live ab; er
   arbeitet ausschließlich auf vorgespeicherten Daten (cache-first). Live-Abruf ist allein Sache der
   asynchronen Datenpflege-Schicht.
2. **Point-in-Time (PIT) / bitemporal** — jede Zeile trägt getrennt „wann geschah es", „wann wurde es
   offengelegt" und „wann wussten wir es". Rückblickende Auswertungen können so kein Zukunftswissen
   verwenden (kein Look-Ahead).
3. **Verlustfreiheit & Redundanzfreiheit** — jedes Dokument wird genau einmal gespeichert (Dedup),
   nichts wird destruktiv gelöscht, und einmal abgerufene Daten werden nicht erneut geholt (Cache).
4. **„Stille ≠ Grün"** — der Betrieb wird von einem unabhängigen Prozess aktiv gemessen; Ausbleiben
   von Fehlern gilt nicht als Beweis für Funktion.

---

## 1. Schichtenmodell

```mermaid
flowchart TB
  subgraph L0[Externe Datenquellen]
    src[arXiv · EDGAR · EPO · EODHD · FRED · Treasury]
  end

  subgraph L1[Ingestion-Schicht]
    conn[Konnektoren<br/>fetch + verifizierte Transformation]
    scr[Sammler / Scraper<br/>Qualitätskaskade · multi-threaded]
    dd[Dedup-Kern<br/>semantische Ähnlichkeit · nicht-destruktiv]
    rec[Aufzeichnungsschicht<br/>verlustfrei · gepinnte Extraktor-Version]
  end

  subgraph L2[Datenhaltungs-Schicht]
    doc[(scraper.db<br/>Dokumente + Fakten)]
    mkt[(markt.db<br/>Kurse + Fundamentals)]
    ch[gzip-Cache<br/>Voll-Dumps]
    dr[(Google-Drive-DB<br/>versioniert)]
  end

  subgraph L3[Orchestrierungs-Schicht]
    run[Runner<br/>Toposort · fail-closed]
    ctr[Kontrakt-Validierung]
    llm[LLM-Ensemble-Router<br/>Failover · provider-agnostisch]
  end

  subgraph L4[Betriebs-Schicht — Schicht S]
    mon[Prozess-Aufsicht<br/>Liveness · Stagnation · Frische · Quota]
    ops[(Ops-DB<br/>physisch getrennt)]
  end

  L0 --> conn
  conn --> scr --> dd --> rec
  rec --> doc
  conn --> ch --> mkt
  ch <--> dr
  run --- L1
  ctr --- L1
  llm --- scr
  mon -.misst.-> L1
  mon -.misst.-> L2
  mon --> ops

  ana[/"Analyse-Schicht — proprietär, separates Repository"/]
  doc --> ana
  mkt --> ana
  style ana stroke-dasharray:6 4,fill:#f6f6f6,color:#555
```

Die Schichten L0–L4 bilden dieses Repository. Die **Analyse-Schicht** (gestrichelt) ist der
Konsument der Daten und bewusst nicht enthalten.

---

## 2. Bitemporaler Point-in-Time-Datenfluss

Jedes Datum trägt drei Zeitachsen. Nur was zum Stichtag *wissbar* war, darf in eine rückblickende
Auswertung einfließen — der Filter ist fail-closed.

```mermaid
flowchart LR
  ev["t_event<br/>(Ereignis geschah)"] --> di["t_disclosed<br/>(öffentlich offengelegt)"]
  di --> in["t_ingest<br/>(vom System erfasst)"]

  subgraph Filter["PIT-Filter am Stichtag t0"]
    direction TB
    q{"t_disclosed ≤ t0<br/>UND t_ingest ≤ t0 ?"}
  end

  in --> q
  q -->|ja| ok["sichtbar am Stichtag<br/>→ zulässige Auswertung"]
  q -->|nein| no["unsichtbar<br/>→ ausgeschlossen (kein Look-Ahead)"]

  style ok fill:#e7f5e7,color:#1a1a1a
  style no fill:#fbeaea,color:#1a1a1a
```

Beispiel Publikations-Latenz je Quelltyp (`t_disclosed − t_event`, in Tagen), wie in der
Aufzeichnungsschicht verankert:

| Quelltyp | Latenz | Grund |
|---|---|---|
| Publikation | 2 | sofort öffentlich, nur Crawl-Latenz |
| Patent | 3 | Publikation = Anmeldung + ~18 Monate; Indexierung |
| Funding-Einreichung | 2 | Offenlegungsdatum = filingDate; Index-Latenz |
| Makro-Reihe | 0 | Vintage-Zeitstempel IST die Verfügbarkeit |

---

## 3. Ingestion-Sequenz

```mermaid
sequenceDiagram
  participant Q as Datenquelle
  participant K as Konnektor
  participant S as Sammler
  participant D as Dedup-Kern
  participant DB as scraper.db
  participant A as Aufzeichnung

  S->>K: fetch(Fenster, PIT-Grenze)
  K->>Q: HTTP-Abruf (fail-closed)
  Q-->>K: Roh-Payload
  K->>K: verifizierte Transformation → strukturiert
  K-->>S: Dokumente (datiert)
  S->>D: Kandidat prüfen
  D->>DB: nächste Nachbarn (Embedding, Zeitfenster)
  alt Near-Duplikat
    D-->>S: als Duplikat markieren (nicht löschen)
  else neu
    D-->>S: kanonisch
    S->>DB: speichern
  end
  S->>A: Dokument + Attribute aufzeichnen (auch abgelehnte)
  Note over A: verlustfrei · gepinnte Extraktor-Version
```

Die Deduplizierung ist **semantisch** (Embedding-Ähnlichkeit statt Byte-Gleichheit),
**nicht-destruktiv** (Markierung via `dup_of`, nie Löschung) und **quellentyp-bewusst** (ein Patent
wird nie als Duplikat des Papers markiert, auf dem es beruht).

---

## 4. Betriebs-Monitoring — Zustandsautomat („Stille ≠ Grün")

Ein unabhängiger Wächter-Prozess bewertet den Betrieb von außen. Alarme durchlaufen einen
entprellten Zustandsautomaten mit menschlicher Quittierung.

```mermaid
stateDiagram-v2
  [*] --> Grün
  Grün --> Gelb: Frühindikator<br/>(Stagnation/Latenz)
  Gelb --> Grün: erholt
  Gelb --> Rot: Schwelle überschritten
  Grün --> Rot: harter Ausfall<br/>(Liveness/Kontrakt)
  Rot --> Quittiert: Mensch bestätigt
  Quittiert --> Grün: behoben
  Rot --> Grün: automatische Erholung

  note right of Rot
    fail-closed:
    Unsicherheit → Rot,
    nie still Grün
  end note
```

Der Wächter läuft als **separater Prozess** und schreibt in eine **physisch getrennte Ops-Datenbank**
— so bleibt die Diagnose auch dann lesbar, wenn ein überwachter Speicher ausfällt. Ein Dead-Man's-
Switch überwacht den Wächter selbst.

---

## 5. Cache-first-Beschaffung & versionierte Archivierung

Einmal abgerufene Daten werden lokal gecacht und periodisch in eine versionierte, führende
Google-Drive-Datenbank ausgelagert (byte-genauer, verifizierter Round-Trip).

```mermaid
flowchart LR
  need[Datenbedarf<br/>Symbol/Reihe] --> hit{im Cache?}
  hit -->|ja| ret[aus Cache liefern<br/>0 API-Einheiten]
  hit -->|nein| fetch[einmaliger Voll-Abruf]
  fetch --> store[gzip-Cache schreiben]
  store --> ret
  store -.periodischer Sync.-> drive[(Google-Drive-DB<br/>inkrementell · versioniert)]
  drive -.Restore vor Lauf.-> store

  style ret fill:#e7f5e7,color:#1a1a1a
```

Der Sync ist **inkrementell** (nur geänderte Buckets), **verlustsicher** (Read-back-Verifikation vor
dem Aufräumen alter Versionen) und **redundanzfrei** (ein kanonischer Stand je Bucket).

---

## Verzeichnisstruktur

```
System/
├── connectors/     Datenquellen-Adapter (fetch + Transformation) + Cache/Drive-Sync
├── harness/        Runner, Kontrakt-Validierung, Store, LLM-Ensemble-Router
├── integration/    Datenpflege, Backfill, Markt-DB-Aufbau, Taxonomie, EOD-Scheduler
├── tests/          Betriebs-/Aufzeichnungs-/Sammler-Tests
├── scraper.py      Sammler (Ingestion-Kern)
├── aufzeichnung.py verlustfreie Aufzeichnungsschicht
└── betrieb_*.py    prozess-unabhängiges Monitoring (Schicht S)
```
