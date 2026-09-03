# Chronos-Stream — point-in-time data integrity

**Chronos-Stream** is a small Python layer that makes **look-ahead bias structurally impossible**
in datasets built from public economic sources. Every record carries explicit temporal roles, a
restatement is a new row rather than an edit, and a contract validator refuses — fail-closed —
to hand on rows that do not satisfy the rules.

The problem it addresses is quiet by nature. When historical data is revised in place, or a value
disclosed later leaks into the evaluation of an earlier moment, models look *more* accurate, not
noisier. Survivorship bias compounds it: a study of the entities that still exist today has
silently excluded the ones that failed. None of these errors announce themselves, which is why
they survive review — and why the remedy has to be structural rather than procedural.

> **Status: working prototype, not a released library.** The mechanisms below run and are covered
> by tests; the packaging, the specification document, the English public API and the verification
> tool are not built yet. See [Roadmap](#roadmap) for what is and isn't here.

---

## The four temporal roles

The centre of the design. Every record answers four separate questions, and collapsing any two of
them is how look-ahead enters:

| Role | Question it answers |
|---|---|
| `t_event` | When did it actually happen? |
| `t_disclosed` | When did it become public — including each later revision? |
| `t_ingest` | When did this system learn of it? |
| `t_ref` | Which period does the value describe? |

Reference data adds validity intervals (`t_valid_von` / `t_valid_bis`) for bitemporal
reconstruction. An as-of query at a past timestamp returns what was knowable **then** — not
today's corrected view of it.

These roles occur **345 times across 17 modules**; they are the schema, not a convention.

## The invariants

| Invariant | Where it is enforced |
|---|---|
| A row is rejected unless it carries the temporal roles its table type requires | `harness/contracts.py` |
| A restatement is appended; nothing is ever overwritten | `harness/store.py` (no `update`, no `delete`) |
| A contract violation aborts the pipeline instead of passing rows downstream | `harness/runner.py` (fail-closed) |
| Judged values stay ordinal; measured values stay numeric — never mixed | `harness/contracts.py` |
| Rejected documents are recorded too, so the denominator survives | `aufzeichnung.py` |
| What a run reported and what it wrote must agree, or the run fails loudly | `aufzeichnung.pruefe_lauf` |

The last one is a conservation law rather than a counter: a check that only asked `n > 0` would
report a run that lost half its documents as healthy.

## What is here

```
System/
  harness/          contracts.py · store.py · runner.py   — the integrity core
  aufzeichnung.py                                          — lossless recording
  connectors/       arxiv_fetch · edgar_form_d · edgar_segment · epo_ops
                    fred_series · treasury_rates · eodhd_prices
                    sammler_db · markt_db          — bitemporal storage schemas
                    eod_cache · fundamentals_cache — fetch-once caches
                    dedup_kern · hf_embedding      — one canonical dedup definition
  integration/      epo_abstract_backfill.py
```

**18 modules, ~3,600 lines of production code; 10 test files, ~1,500 lines, 146 tests, all green
and all offline.** The core transformations, the contract validation and the recording logic are
pure and require no network. Live fetches (`fetch_*`) need the respective API keys as environment
variables and are never exercised by the test suite.

### Data sources — all public

| Source | Content | Access |
|---|---|---|
| arXiv | preprints | public, keyless |
| SEC EDGAR | US corporate filings (Form D, segments) | public, keyless (contact UA required) |
| EPO OPS | European patents | OAuth2, free registration |
| FRED / ALFRED | US macro series, vintage-aware | free API key |
| US Treasury | par yield curve | public, keyless |
| EODHD | prices and fundamentals | API key |

API keys and contact addresses come from environment variables and are never committed.

## What is deliberately *not* here

The analysis and valuation logic that consumes this data is proprietary and lives elsewhere.
Chronos-Stream ends exactly at the boundary between **data integrity** and **interpretation**.

Also removed on purpose, to keep the repository to one story: collection orchestration, cloud
storage synchronisation, LLM routing, and industry-taxonomy tables. They belong to a research
pipeline, not to a data-integrity component, and their presence made this repository harder to
read for the one thing it is about.

## Honest limitations

- **The public interface is German** (identifiers, docstrings). Internationalising it is the
  first thing a wider audience needs.
- **Coverage is uneven.** The integrity core, the caches and the storage schemas are covered;
  most connectors are exercised only through their offline transformation cores, and the live
  fetch paths not at all.
- **Not packaged.** There is no `pyproject.toml` and nothing on PyPI; you can read and run this
  code, but you cannot yet depend on it.
- **No verification tool.** The invariants guard *this* system's writes. Nothing here lets you
  point a checker at a dataset **you** produced — which is the gap that matters most.

## Getting started

```bash
git clone https://github.com/jens-alker/chronos-stream
cd chronos-stream

# Full test suite — standard library only, no network, no keys
for t in $(find System -name 'test_*.py' | sort); do
  (cd "$(dirname "$t")" && python3 "$(basename "$t")") || echo "FAILED: $t"
done
```

Each test file is standalone and can be run directly.

## Roadmap

1. **A specification** of the temporal invariants — precise enough to be machine-checked and
   implemented by someone else, with the ambiguous cases named rather than glossed over.
2. **A public conformance corpus** — small datasets carrying documented violations and their
   expected findings, so any implementation can be measured, not just this one.
3. **A packaged library** with an English public API and coverage that justifies a dependency.
4. **`chronos-stream verify`** — check a dataset against the specification and name the rows that
   violate it.
5. **A documented connector contract**, first-print archival, and a 1.0 release.

The order is deliberate. A specification and a corpus are useful to people who never install this
library — and an integrity guarantee only its own author can verify is not much of a guarantee.

## License

[MIT](LICENSE) — free reuse including commercial, provided the licence notice is retained.
