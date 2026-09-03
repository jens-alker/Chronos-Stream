# Architecture — Chronos-Stream

How the point-in-time guarantee is actually enforced, in diagrams. All diagrams are Mermaid and
render directly on GitHub.

---

## 1. Layered model

Four layers, one direction. Nothing below reaches upward, and the analysis layer that consumes
this data is outside the repository entirely.

```mermaid
flowchart TB
  subgraph Q[Public sources]
    direction LR
    arxiv[arXiv] ~~~ edgar[SEC EDGAR] ~~~ epo[EPO OPS]
    fred[FRED/ALFRED] ~~~ treas[US Treasury] ~~~ eodhd[EODHD]
  end

  subgraph C[Connectors — fetch and transform]
    direction LR
    trans["transformation core<br/>(pure, offline-tested)"]
    fetch["live fetch<br/>(keys from env)"]
  end

  subgraph S[Storage — bitemporal, append-only]
    direction LR
    sdb[("sammler_db<br/>documents + facts")]
    mdb[("markt_db<br/>prices + fundamentals")]
    rec[("aufzeichnung<br/>lossless record")]
  end

  subgraph I[Integrity core]
    direction LR
    con["contracts.py<br/>contract validator"]
    run["runner.py<br/>fail-closed pipeline"]
    sto["store.py<br/>append-only"]
  end

  Q --> C --> S
  I -. validates every write .-> S

  ana[/"Analysis and valuation<br/>(proprietary — NOT in this repo)"/]
  S --> ana

  style ana stroke-dasharray:6 4,fill:#f6f6f6,color:#666
```

The split inside every connector is deliberate: the **transformation** from raw payload to
structured rows is a pure function and is tested offline against recorded payloads; the **live
fetch** is a thin shell around it. Correctness is therefore checkable without network or keys.

---

## 2. The four temporal roles

The heart of the design. A record is not one point in time but four, and every collapse of two
of them is a way for future knowledge to leak backwards.

```mermaid
timeline
  title One fact, four timestamps
  t_event       : the thing happened
  t_disclosed   : it was published — and again on every revision
  t_ingest      : this system learned of it
  t_ref         : the period the value describes
```

An as-of query at time `T` admits a row only if `t_disclosed <= T` **and** `t_ingest <= T`. The
second condition is the one usually forgotten: a value may have been public before this system
could have known it, and using it anyway is look-ahead through the back door.

```mermaid
flowchart LR
  q["as-of query at T"] --> f{"t_disclosed ≤ T<br/>AND t_ingest ≤ T?"}
  f -->|yes| ok["row is visible"]
  f -->|no| skip["row does not exist yet<br/>at time T"]
```

### Restatement is an append, never an edit

```mermaid
flowchart TB
  v1["GDP Q1 = 2.1%<br/>t_disclosed 2026-04-30"]
  v2["GDP Q1 = 1.8%<br/>t_disclosed 2026-05-28<br/>(revision)"]
  v1 --> v2
  v1 -.->|"as-of 2026-05-01<br/>still answers 2.1%"| a1[" "]
  v2 -.->|"as-of 2026-06-01<br/>answers 1.8%"| a2[" "]
  style a1 fill:none,stroke:none
  style a2 fill:none,stroke:none
```

If the revision overwrote its predecessor, the first answer would be unreachable — and a
backtest run today would quietly use a number nobody had in May.

---

## 3. Fail-closed validation

Every step's output is validated against its table contract **before** it is stored. A violation
aborts the pipeline; it does not log a warning and continue.

```mermaid
flowchart TB
  step["step produces rows"] --> val{"contract satisfied?"}
  val -->|yes| app["append to store"] --> next["next step"]
  val -->|no| stop(["ContractError —<br/>pipeline aborts,<br/>nothing is stored"])
  style stop fill:#fdd,stroke:#c00,color:#900
```

The order matters and is tested: validation precedes the append, so an aborted run leaves no
partial rows for a later consumer to find. What the validator enforces:

| Rule | Why it exists |
|---|---|
| The temporal roles required by the table type are present and non-empty | Without them a cutoff cannot be evaluated at all |
| A contract that omits a required role is itself rejected | Otherwise every row passes while nothing is checked |
| Judged fields stay ordinal, measured fields stay numeric | A judgement dressed as a decimal invites arithmetic that means nothing |
| A category id never appears without its version | An unversioned id silently pins to "latest" — look-ahead in disguise |
| Non-nullable fields must be present and non-empty | Otherwise "contract-valid" is not a completeness statement |
| An unknown table type stops validation | Fail-closed: unrecognised must not mean unconstrained |

---

## 4. Lossless recording and its conservation law

Rejected documents are recorded too. The set of everything seen is the denominator of any later
statement about what was filtered out; keeping only the accepted ones destroys it irrecoverably.

```mermaid
flowchart LR
  found["documents found<br/>by a run"] --> dec{"relevance ≥ threshold?"}
  dec -->|yes| acc["stored, angenommen = 1"]
  dec -->|no| rej["stored, angenommen = 0"]
  acc --> law
  rej --> law
  law{{"pruefe_lauf:<br/>n_gefunden == #decisions<br/>n_neu == #raw documents"}}
  law -->|holds| ok["run accepted"]
  law -->|differs| err(["AufzeichnungFehler —<br/>fail loud"])
  style err fill:#fdd,stroke:#c00,color:#900
```

The two sides of the comparison come from **different sources** — what the run reported versus
what was actually written. An identity computed from a single source could never break, and
would be decoration rather than a check.

---

## Directory structure

```
System/
  harness/
    contracts.py            contract validator — the invariants, machine-enforced
    runner.py               fail-closed pipeline with dependency ordering
    store.py                append-only store (in-memory and SQLite)
    tests/                  test_contracts · test_runner · test_store
  aufzeichnung.py           lossless recording + the conservation law
  connectors/
    arxiv_fetch.py          preprints
    edgar_form_d.py         SEC Form D — funding rounds
    edgar_segment.py        SEC segment reporting
    epo_ops.py              European patents (OAuth2)
    fred_series.py          macro series, vintage-aware
    treasury_rates.py       risk-free yield curve
    eodhd_prices.py         prices and fundamentals
    sammler_db.py           document/fact schema, bitemporal
    markt_db.py             market data schema, bitemporal
    eod_cache.py            fetch-once caches
    fundamentals_cache.py
    dedup_kern.py           one canonical semantic dedup definition
    hf_embedding.py         embeddings for that definition
    tests/
  integration/
    epo_abstract_backfill.py
    tests/
  tests/                    test_aufzeichnung
docs/
  ARCHITECTURE.md           this file
```

Run the whole suite offline, without keys:

```bash
for t in $(find System -name 'test_*.py' | sort); do
  (cd "$(dirname "$t")" && python3 "$(basename "$t")") || echo "FAILED: $t"
done
```
