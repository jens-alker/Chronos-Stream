# Architecture — Chronos-Stream Data Infrastructure

This document describes the architecture of the open data infrastructure in diagrams. All diagrams
are written in [Mermaid](https://mermaid.js.org/) and render directly on GitHub.

The design follows four core principles:

1. **Separation of acquisition and computation** — the compute/analysis path never fetches live; it
   works exclusively on pre-stored data (cache-first). Live fetching is solely the job of the
   asynchronous data-maintenance layer.
2. **Point-in-time (PIT) / bitemporal** — every row separately carries "when it happened", "when it
   was disclosed", and "when we knew it". Retrospective evaluations can therefore never use future
   knowledge (no look-ahead).
3. **Lossless & redundancy-free** — every document is stored exactly once (dedup), nothing is deleted
   destructively, and data fetched once is not fetched again (cache).
4. **"Silence ≠ green"** — operations are actively measured by an independent process; the absence of
   errors does not count as proof of correct operation.

---

## 1. Layered model

```mermaid
flowchart TB
  subgraph L0[External data sources]
    src[arXiv · EDGAR · EPO · EODHD · FRED · Treasury]
  end

  subgraph L1[Ingestion layer]
    conn[Connectors<br/>fetch + verified transformation]
    scr[Collector / scraper<br/>quality cascade · multi-threaded]
    dd[Dedup core<br/>semantic similarity · non-destructive]
    rec[Recording layer<br/>lossless · pinned extractor version]
  end

  subgraph L2[Data storage layer]
    doc[(scraper.db<br/>documents + facts)]
    mkt[(markt.db<br/>prices + fundamentals)]
    ch[gzip cache<br/>full dumps]
    dr[(Google Drive DB<br/>versioned)]
  end

  subgraph L3[Orchestration layer]
    run[Runner<br/>topological sort · fail-closed]
    ctr[Contract validation]
    llm[LLM ensemble router<br/>failover · provider-agnostic]
  end

  subgraph L4[Operations layer — Layer S]
    mon[Process watchdog<br/>liveness · stagnation · freshness · quota]
    ops[(Ops DB<br/>physically separate)]
  end

  L0 --> conn
  conn --> scr --> dd --> rec
  rec --> doc
  conn --> ch --> mkt
  ch <--> dr
  run --- L1
  ctr --- L1
  llm --- scr
  mon -.measures.-> L1
  mon -.measures.-> L2
  mon --> ops

  ana[/"Analysis layer — proprietary, separate repository"/]
  doc --> ana
  mkt --> ana
  style ana stroke-dasharray:6 4,fill:#f6f6f6,color:#555
```

Layers L0–L4 make up this repository. The **analysis layer** (dashed) is the consumer of the data and
is deliberately not included.

---

## 2. Bitemporal point-in-time data flow

Every datum carries three time axes. Only what was *knowable* as of the observation date may enter a
retrospective evaluation — the filter is fail-closed.

```mermaid
flowchart LR
  ev["t_event<br/>(event occurred)"] --> di["t_disclosed<br/>(publicly disclosed)"]
  di --> in["t_ingest<br/>(captured by the system)"]

  subgraph Filter["PIT filter at observation date t0"]
    direction TB
    q{"t_disclosed ≤ t0<br/>AND t_ingest ≤ t0 ?"}
  end

  in --> q
  q -->|yes| ok["visible as of the date<br/>→ admissible for evaluation"]
  q -->|no| no["not visible<br/>→ excluded (no look-ahead)"]

  style ok fill:#e7f5e7,color:#1a1a1a
  style no fill:#fbeaea,color:#1a1a1a
```

Example disclosure latency by source type (`t_disclosed − t_event`, in days), as anchored in the
recording layer:

| Source type | Latency | Reason |
|---|---|---|
| Publication | 2 | immediately public, only crawl latency |
| Patent | 3 | publication = filing + ~18 months; indexing |
| Funding filing | 2 | disclosure date = filing date; index latency |
| Macro series | 0 | the vintage timestamp IS the availability |

---

## 3. Ingestion sequence

```mermaid
sequenceDiagram
  participant Q as Data source
  participant K as Connector
  participant S as Collector
  participant D as Dedup core
  participant DB as scraper.db
  participant A as Recording

  S->>K: fetch(window, PIT boundary)
  K->>Q: HTTP fetch (fail-closed)
  Q-->>K: raw payload
  K->>K: verified transformation → structured
  K-->>S: documents (dated)
  S->>D: check candidate
  D->>DB: nearest neighbors (embedding, time window)
  alt near-duplicate
    D-->>S: mark as duplicate (do not delete)
  else new
    D-->>S: canonical
    S->>DB: store
  end
  S->>A: record document + attributes (including rejected)
  Note over A: lossless · pinned extractor version
```

Deduplication is **semantic** (embedding similarity rather than byte equality),
**non-destructive** (marking via `dup_of`, never deletion), and **source-type-aware** (a patent is
never marked as a duplicate of the paper it builds on).

---

## 4. Operations monitoring — state machine ("silence ≠ green")

An independent watchdog process evaluates operations from the outside. Alerts pass through a
debounced state machine with human acknowledgement.

```mermaid
stateDiagram-v2
  [*] --> Green
  Green --> Yellow: early indicator<br/>(stagnation/latency)
  Yellow --> Green: recovered
  Yellow --> Red: threshold exceeded
  Green --> Red: hard failure<br/>(liveness/contract)
  Red --> Acknowledged: human confirms
  Acknowledged --> Green: resolved
  Red --> Green: automatic recovery

  note right of Red
    fail-closed:
    uncertainty → Red,
    never silently Green
  end note
```

The watchdog runs as a **separate process** and writes into a **physically separate ops database** —
so the diagnosis stays readable even if a monitored store fails. A dead-man's switch monitors the
watchdog itself.

---

## 5. Cache-first acquisition & versioned archiving

Data fetched once is cached locally and periodically offloaded to a versioned, authoritative Google
Drive database (byte-exact, verified round-trip).

```mermaid
flowchart LR
  need[data need<br/>symbol/series] --> hit{in cache?}
  hit -->|yes| ret[serve from cache<br/>0 API units]
  hit -->|no| fetch[single full fetch]
  fetch --> store[write gzip cache]
  store --> ret
  store -.periodic sync.-> drive[(Google Drive DB<br/>incremental · versioned)]
  drive -.restore before run.-> store

  style ret fill:#e7f5e7,color:#1a1a1a
```

The sync is **incremental** (only changed buckets), **loss-safe** (read-back verification before old
versions are cleaned up), and **redundancy-free** (one canonical state per bucket).

---

## Directory structure

```
System/
├── connectors/     data-source adapters (fetch + transformation) + cache/Drive sync
├── harness/        runner, contract validation, store, LLM ensemble router
├── integration/    data maintenance, backfill, market-DB construction, taxonomy, EOD scheduler
├── tests/          operations / recording / collector tests
├── scraper.py      collector (ingestion core)
├── aufzeichnung.py lossless recording layer
└── betrieb_*.py    process-independent monitoring (Layer S)
```
