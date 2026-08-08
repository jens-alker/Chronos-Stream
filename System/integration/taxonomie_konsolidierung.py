"""
taxonomie_konsolidierung.py — Kategorie-Map (Taxonomie 0/2): die feine, LLM-erzeugte Themen-Taxonomie zu
KOHAERENTEN Kategorien konsolidieren, damit die Kategorisierung AGGREGIERT statt zu fragmentieren.

Befund (Voll-Lauf 13.744 Fakten): `themen_zu_kategorie_version` erzeugt ~1.500+ Beinahe-Synonyme
(„Energieeffizienz in X" ~100x, „Technologische Innovation im Y" ~120x, „Regulierung von Krypto…" ~50x,
inkl. Tippfehler + malformte/fremdsprachige Namen). Der Embedding-kNN kategorisiert korrekt, aber in so
viele Mikro-Themen, dass NICHTS Fakten sammelt -> kein Kategorie-Signal (6b/6c bekommen keine Masse).

Mechanik (KEINE INSEL): nutzt `embedding_llm` (Embedding, gecacht) + `dedup_kern.kosinus` (DIE eine
Kosinus-Definition). Beinahe-Duplikate (Kosinus >= schwelle) werden per Union-Find zu einem Cluster; der
kuerzeste saubere Name ist der Repraesentant, ALLE Cluster-Namen (+ ihre Aliase) werden seine Aliase — so
trifft der kNN jede Phrasierung eines Fakts, aggregiert ihn aber in DIE EINE konsolidierte Kategorie.

Verortung: die Konsolidierung GEHOERT zu Modul 0/2 (Vokabular-Besitzer); der Orchestrierungs-Dirigent NUTZT
sie, formt sie nicht. Rueckgabe ist eine `kategorie_version`-Form (Drop-in fuer die V1-Anker) + ein
`rollup` (original -> Repraesentant) fuer die Provenienz. Nur Standardbibliothek + projektinterne Bausteine.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = os.path.dirname(_HERE)
for _p in (os.path.join(_SYS, "connectors"), os.path.join(_SYS, "harness"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dedup_kern import kosinus                                          # noqa: E402  (DIE eine Kosinus-Def.)


def ist_sauberer_name(name, min_laenge=3):
    """Filtert malformte/fremdsprachige Namen (der Voll-Lauf zeigte CJK/Kyrillisch/Mojibake). Deutsche
    Umlaute/ß (< 0x0400) bleiben; CJK (0x3000–0x9FFF), Kyrillisch (0x0400–0x04FF), Fullwidth (>=0xFF00) raus."""
    n = (name or "").strip()
    if len(n) < min_laenge:
        return False
    for c in n:
        o = ord(c)
        if 0x0400 <= o <= 0x04FF or 0x3000 <= o <= 0x9FFF or o >= 0xFF00:
            return False
    return True


def _clustere(vecs, schwelle):
    """Union-Find über Kosinus >= schwelle. numpy-Matrix (schnell) falls verfügbar, sonst reiner O(n²)-Kern
    (byte-äquivalent, für kleine Test-Mengen). Rückgabe: Liste von Index-Clustern."""
    n = len(vecs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    try:
        import numpy as np
        A = np.asarray(vecs, dtype=np.float64)
        norms = np.linalg.norm(A, axis=1)
        norms[norms == 0] = 1.0
        A = A / norms[:, None]
        sim = A @ A.T
        iu = np.triu_indices(n, 1)
        for i, j in zip(iu[0][sim[iu] >= schwelle], iu[1][sim[iu] >= schwelle]):
            union(int(i), int(j))
    except ImportError:
        for i in range(n):
            for j in range(i + 1, n):
                if kosinus(vecs[i], vecs[j]) >= schwelle:
                    union(i, j)

    gruppen = {}
    for i in range(n):
        gruppen.setdefault(find(i), []).append(i)
    return list(gruppen.values())


def _repraesentant(cluster_rows):
    """Der Repräsentant eines Clusters = der KÜRZESTE saubere Name (allgemeinste/kanonischste Form),
    deterministisch (Tie-Break alphabetisch). Z. B. 'Halbleiter' vor 'Energieeffizienz in der Halbleiterfertigung'."""
    return min(cluster_rows, key=lambda r: (len(r["name"]), r["name"]))


def konsolidiere(seed_rows, embed_fn, schwelle=0.85, min_name_laenge=3):
    """Feine Taxonomie -> konsolidierte, aggregierende Kategorien.

    `seed_rows`: die `kategorie_version`-Zeilen aus `themen_zu_kategorie_version`.
    `embed_fn(text)->vec`: injizierbar (Betrieb: gecachtes nomic; Test: Fake). `schwelle`: Kosinus-Grenze,
    ab der zwei Namen als „dasselbe Thema" gelten (0.85 = konservativ; höher = weniger Zusammenfassung).
    Rückgabe: (konsolidierte_rows, rollup). `konsolidierte_rows` = eine Zeile je Cluster (Repräsentant-Name
    als kat_id/name, ALLE Cluster-Namen + deren Aliase als `aliase`); `rollup` = {original_kat_id: repr_kat_id}.
    """
    sauber = [r for r in seed_rows if ist_sauberer_name(r.get("name", ""), min_name_laenge)]
    if not sauber:
        return [], {}
    vecs = [embed_fn(r["name"]) for r in sauber]
    cluster_idx = _clustere(vecs, schwelle)

    konsolidiert, rollup = [], {}
    for idxs in cluster_idx:
        rows = [sauber[i] for i in idxs]
        rep = _repraesentant(rows)
        # Alle Namen + Aliase des Clusters werden die Aliase des Repräsentanten (der kNN trifft jede
        # Phrasierung, aggregiert aber in DIE EINE Kategorie). Dedupliziert, deterministisch sortiert.
        aliase = set()
        for r in rows:
            if r["name"] != rep["name"]:
                aliase.add(r["name"])
            for a in (r.get("aliase") or []):
                if (a or "").strip():
                    aliase.add(a)
            rollup[r["kat_id"]] = rep["kat_id"]
        # reifegrad: höchster im Cluster (established > growing > emerging); gic_rollup: erster gesetzte.
        rang = {"emerging": 0, "growing": 1, "established": 2}
        reifegrad = max((r.get("reifegrad", "emerging") for r in rows),
                        key=lambda g: rang.get(g, 0))
        gic = next((r.get("gic_rollup") for r in rows if r.get("gic_rollup")), None)
        # Bitemporale + Lineage-Felder (Fable-QS B1/B2): make_vokabular greift HART auf t_valid_von/t_ingest;
        # der Kontrakt hat `vorgaenger` fuer Merge-Provenienz -> die zusammengefassten kat_ids landen dort
        # (nicht verworfen). t_valid_von/t_ingest = FRUEHESTES Cluster-Mitglied (die Kategorie existiert seit
        # ihrem aeltesten Synonym) -> PIT-schneidbar. status/t_valid_bis vom Repraesentanten (aktiv/offen).
        konsolidiert.append({
            "kat_id": rep["kat_id"], "version": rep.get("version", 1),
            "ebene": rep.get("ebene", "technologie"), "name": rep["name"],
            "aliase": sorted(aliase), "reifegrad": reifegrad, "gic_rollup": gic,
            "vorgaenger": sorted(r["kat_id"] for r in rows if r["kat_id"] != rep["kat_id"]),
            "nachfolger": [], "status_vokabular": "aktiv",
            "t_valid_von": min((r.get("t_valid_von") for r in rows if r.get("t_valid_von")), default=None),
            "t_valid_bis": rep.get("t_valid_bis"),
            "t_ingest": min((r.get("t_ingest") for r in rows if r.get("t_ingest")), default=None),
            "n_zusammengefasst": len(rows),
        })
    konsolidiert.sort(key=lambda r: (-r["n_zusammengefasst"], r["name"]))
    return konsolidiert, rollup
