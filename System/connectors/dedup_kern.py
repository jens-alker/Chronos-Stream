"""
dedup_kern.py — die EINE Definition des semantischen Like-Dedups (Jens 30.07.: „die Scraper-Funktion zu
einer Definition zusammenführen"). Heim (`scraper.py`, nomic) UND Cloud (`sammler_db`, HF-MiniLM) rufen
DIESELBE Politik + DASSELBE Scoring — keine zwei Definitionen (keine Insel).

**Kanonisch = die HEIM-Version** (Jens-Entscheid): der Heim-Bestand (70k → nach zwei Wochen Lauf bis zu
~500k Dokumente) ist bereits UNTER dieser Definition dedupt; ein Re-Dedup wäre teuer und unnötig — UND die
Heim-Definition ist die konzept-treuere. Die Cloud-Sammlung (klein, forward) passt sich an.

DEFINITION (modell-agnostisch; nur die Schwelle ist ein pro-Embed-Modell kalibrierter Parameter):
  - **Blocking nach source_type** — nur Dokumente DERSELBEN Reifegrad-Sprosse (paper/patent/funding/news)
    werden verglichen. **KONZEPT-KRITISCH:** ein Patent darf NIE als Dup des Papers markiert werden, auf dem
    es beruht — sonst kollabiert die Kette Paper→Patent→Funding→News, die das System gerade DETEKTIERT
    (die Kette IST das System). Die frühere Cloud-Dedup OHNE source_type-Block war ein
    latenter Bug, kein Vorteil.
  - **Zeitfenster** ±`fenster_tage` um `published_at` — fängt Republikationen 2–3 Tage später, meidet O(N²).
  - **Best-Match** — unter den Kandidaten die HÖCHSTE Kosinus-Ähnlichkeit; ist sie ≥ `schwelle` → Near-Dup
    (nicht First-Match: der ähnlichste Kanon gewinnt, deterministisch bei gegebener Kandidatenreihenfolge).
  - **Kanonisch = ein früheres, bereits gespeichertes Dokument** — der Aufrufer liefert per SQL nur
    Kandidaten mit KLEINERER id als das gerade markierte Dokument (`e.doc_id < did`) UND `dup_of IS NULL`
    (nur Wurzel-Kanons). dup_of zeigt also IMMER auf eine kleinere id → streng abfallende Kette = DAG (3.16),
    kein Zyklus/Selbstbezug. Bei EXAKTEM Score-Gleichstand (praktisch nur bei bytegleichem Text) gewinnt der
    zuerst gelistete Kandidat — Heim/Cloud listen doc_id DESC, also der jüngste der früheren Wurzel-Kanons;
    das bleibt ein gültiger früherer Kanon (Gemini-QS B2: keine „kleinste id"-Garantie, aber stets ein
    früheres Dokument; folgenlos, weil nicht-destruktiv).
  - **Nicht-destruktiv** — nur `documents.dup_of=<kanonisch>` setzen, NIE löschen; Konsumenten filtern
    `dup_of IS NULL`.
  - `schwelle`: pro Embed-Modell kalibriert (nomic ~0.92, MiniLM ~0.93; beide Defaults noch unkalibriert,
    ehrlich markiert). Die DEFINITION ist modell-agnostisch — die Schwelle der einzige Knopf.

Der EMBED-Backend (nomic lokal / MiniLM API) und die KANDIDATEN-Beschaffung (SQL über die jeweilige
Embedding-Tabelle) werden vom Aufrufer INJIZIERT — Heim und Cloud teilen die POLITIK + das SCORING, nicht
die DB-Naht. Nur Standardbibliothek.
"""
import math

# Politik-Konstanten der EINEN Definition (die Aufrufer bauen ihr Kandidaten-SQL danach).
FENSTER_TAGE = 21          # ±Tage um published_at (Heim-Default; Republikationen fangen)
BLOCK_SOURCE_TYPE = True   # KONZEPT-KRITISCH: nie über Reifegrad-Sprossen hinweg deduppen


def kosinus(a, b):
    """Kosinus-Ähnlichkeit zweier Float-Vektoren. 0 bei Null-Norm ODER Dim-Mismatch (fail-safe: nie Div-0,
    nie ein falsches „identisch" bei ungleichen Dimensionen — z. B. nomic 768d vs. MiniLM 384d)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def beste_uebereinstimmung(vec, kandidaten, schwelle):
    """Die EINE Dedup-Entscheidung: unter `kandidaten` [(id, vec), …] die HÖCHSTE Kosinus-Ähnlichkeit zu
    `vec`; ist sie ≥ `schwelle`, gib deren id (das kanonische Dokument) zurück, sonst None.

    Best-Match (nicht First-Match) — der ähnlichste Kanon gewinnt. Bei exaktem Gleichstand gewinnt der
    zuerst gelistete Kandidat (`sim > best`), die Reihenfolge liegt also beim Aufrufer (Heim liefert
    doc_id DESC = neuester Kanon zuerst; byte-identisch zum Alt-Verhalten). Der Aufrufer garantiert per
    SQL, dass `kandidaten` schon nach source_type + Zeitfenster geblockt UND `dup_of IS NULL` sind."""
    if not vec:
        return None
    best_id, best = None, 0.0
    for kid, kvec in kandidaten:
        if not kvec:
            continue
        sim = kosinus(vec, kvec)
        if sim > best:
            best, best_id = sim, kid
    return best_id if best >= schwelle else None
