"""
epo_abstract_backfill.py — die bestehenden EPO-Patent-Stümpfe mit ihrem Abstract nachreichern (Heim, 07.08.).

Die Kompositionsdiagnose (07.08.) zeigte: 846 Patent-Docs in der scraper.db haben Text% 1 % / ØLen 89 — das
ist die Titelzeile, kein Abstract. `epo_ops.zu_dokumente` mergt ab jetzt den `/abstract`-Constituent (Vorwärts-
Pfad in `sammel_forward`), aber die BESTEHENDEN Stümpfe bleiben titel-arm, bis sie einmalig nachgereichert
werden. F/TextDoc war 3.0 → jeder nachgereicherte Patent liefert ~3 Fakten (patent-Sprosse füllt sich).

Nicht-destruktiv: der Abstract wird an das vorhandene `text` ANGEHÄNGT (Titel + IPC bleiben), nie überschrieben.
Resumierbar/idempotent: nur Docs mit kurzem Text werden gezogen — ein nachgereichertes Doc fällt aus der Menge.
Gedrosselt: OPS RobotDetected (~15 Suchen/min) → `drossel_sek` zwischen Abrufen + transient-Backoff.

KEINE INSEL: reine Anreicherung des `documents.text` über den bestehenden `epo_ops`-Konnektor (fetch_abstract/
parse_abstract). Keine neue Signal-/Kategorie-Definition. Home-gated (braucht die echte scraper.db + EPO-Keys);
der reine Kern (`_patent_id_zu_ref`, `zu_backfillende`, die Update-Logik) ist offline gegen ein temp-Schema
getestet, der Live-Fetch ist der bestehende Konnektor.
"""
import argparse
import os
import re
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "connectors")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# patent_id "KR20180138273A" -> docdb-Ref "KR.20180138273.A": 2 Länder-Buchstaben + Nummer + Kind (Buchstabe[+Ziffer]).
_PID_RE = re.compile(r"^([A-Za-z]{2})(\d+)([A-Za-z]\d*)$")


def _patent_id_zu_ref(patent_id):
    """"KR20180138273A" -> "KR.20180138273.A" (docdb-Ref für /abstract). Nicht zerlegbar -> None (überspringen,
    fail-closed: lieber kein Backfill als eine geratene Referenz abrufen)."""
    m = _PID_RE.match((patent_id or "").strip())
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else None


# Erledigt-Marker (Fable-QS Blocker-2): ein DB-Marker statt der Längen-Heuristik. Ein Doc, das der Backfill
# EINMAL bearbeitet hat (angereichert ODER nachweislich abstract-los), wird nie wieder gezogen — kein Doppel-
# Anhängen (Idempotenz) und kein erneuter Abruf abstract-loser Patente (Guardrail 5, keine Redundanz). Analog
# `documents.dup_of`/`doc_embedding_done`.
_MARKER_DDL = "CREATE TABLE IF NOT EXISTS epo_abstract_done (doc_id INTEGER PRIMARY KEY, hat_abstract INTEGER)"


def _tabelle_existiert(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def zu_backfillende(db_pfad, text_max=200, limit=None):
    """Read-only: EPO-Patent-Docs mit KURZEM Text (Stümpfe), die der Backfill NOCH NICHT bearbeitet hat
    -> [(id, url, text)]. Der Längen-Filter (Titel+IPC ~89 Zeichen) grenzt die Stümpfe ein (schon angereicherte
    Vorwärts-Docs sind lang -> draußen, kein Doppel-Anhängen); der `epo_abstract_done`-Marker (falls vorhanden)
    hält EINMAL bearbeitete Docs draußen — auch kurz-gebliebene und abstract-lose (Fable-QS Blocker-2).
    source_type 'epo' ODER 'patent' (Heim-Normalisierung)."""
    with sqlite3.connect("file:" + os.path.abspath(db_pfad).replace("\\", "/") + "?mode=ro", uri=True) as c:
        marker = (" AND id NOT IN (SELECT doc_id FROM epo_abstract_done)"
                  if _tabelle_existiert(c, "epo_abstract_done") else "")
        q = ("SELECT id, url, text FROM documents WHERE source_type IN ('epo','patent') "
             f"AND length(coalesce(text,'')) < ?{marker} ORDER BY id")
        if limit:
            q += f" LIMIT {int(limit)}"
        return c.execute(q, (text_max,)).fetchall()


def backfill(db_pfad, token=None, max_pro_lauf=300, text_max=200, drossel_sek=4.5, token_refresh_n=100,
             fetch_fn=None, parse_fn=None, token_fn=None, control_fn=None, fortschritt=True):
    """Reichert bis zu `max_pro_lauf` EPO-Stümpfe mit ihrem Abstract an (append an `text`, nicht-destruktiv).
    Je bearbeitetem Doc:
      • Abstract da  -> an `text` anhängen, `facts_done`-Zeile LÖSCHEN (Fable-QS Blocker-1: sonst re-extrahiert
        1c den angereicherten Text NIE — der 1c-Selektor überspringt alles in `facts_done`), Marker setzen.
      • kein Abstract / unparsebar -> nur Marker setzen (nie wieder abrufen).
    `token_refresh_n` (Fable-QS Major-3): das OPS-Token gilt ~20 Min; alle N Docs neu holen, sonst faulten die
    späten Abrufe still. `fetch_fn`/`parse_fn`/`token_fn`/`control_fn` injizierbar (Offline-Test). -> dict."""
    if fetch_fn is None:
        from epo_ops import fetch_abstract as fetch_fn
    if parse_fn is None:
        from epo_ops import parse_abstract as parse_fn
    if token_fn is None:
        from epo_ops import fetch_token as token_fn
    if token is None:
        token = token_fn()
    offen = zu_backfillende(db_pfad, text_max=text_max, limit=max_pro_lauf)
    n_angereichert = n_kein_abstract = n_unparsebar = n_fehler = 0
    conn = sqlite3.connect(db_pfad)
    try:
        conn.execute(_MARKER_DDL)
        hat_facts_done = _tabelle_existiert(conn, "facts_done")
        for i, (doc_id, url, text) in enumerate(offen, 1):
            if control_fn and control_fn() != "run":
                if fortschritt:
                    print(f"  ⏸ abgebrochen bei {i-1}/{len(offen)} (verlustfrei)", flush=True)
                break
            if token_refresh_n and i > 1 and (i - 1) % token_refresh_n == 0:   # Token vor Ablauf erneuern
                try:
                    token = token_fn()
                except Exception as e:                          # noqa: BLE001 — Refresh-Blip: mit altem Token weiter
                    if fortschritt:
                        print(f"  ⚠ Token-Refresh fehlgeschlagen ({type(e).__name__}) — weiter mit altem", flush=True)
            ref = _patent_id_zu_ref(url)
            if not ref:
                n_unparsebar += 1
                conn.execute("INSERT OR IGNORE INTO epo_abstract_done(doc_id,hat_abstract) VALUES(?,0)", (doc_id,))
                conn.commit()
                continue
            try:
                pa = parse_fn(fetch_fn(ref, token))
            except Exception as e:                              # noqa: BLE001 — Blip/RobotDetected/Auth: NICHT markieren -> nächster Lauf holt es
                n_fehler += 1
                if fortschritt:
                    print(f"  ⚠ {ref}: {type(e).__name__} {str(e)[:60]}", flush=True)
                time.sleep(drossel_sek)
                continue
            ab = (pa.get("abstract") or "").strip()
            if ab:
                neu = ((text or "").strip() + " " + ab).strip()   # ANHÄNGEN (Titel+IPC bleiben)
                conn.execute("UPDATE documents SET text=? WHERE id=?", (neu, doc_id))
                if hat_facts_done:                              # Blocker-1: 1c muss den neuen Text neu extrahieren
                    conn.execute("DELETE FROM facts_done WHERE doc_id=?", (doc_id,))
                conn.execute("INSERT OR IGNORE INTO epo_abstract_done(doc_id,hat_abstract) VALUES(?,1)", (doc_id,))
                conn.commit()
                n_angereichert += 1
            else:
                n_kein_abstract += 1
                conn.execute("INSERT OR IGNORE INTO epo_abstract_done(doc_id,hat_abstract) VALUES(?,0)", (doc_id,))
                conn.commit()
            if fortschritt and i % 25 == 0:
                print(f"  [{i}/{len(offen)}] {n_angereichert} angereichert · {n_kein_abstract} ohne Abstract "
                      f"· {n_fehler} Fehler", flush=True)
            time.sleep(drossel_sek)
    finally:
        conn.close()
    r = {"n_offen": len(offen), "n_angereichert": n_angereichert, "n_kein_abstract": n_kein_abstract,
         "n_unparsebar": n_unparsebar, "n_fehler": n_fehler}
    if fortschritt:
        print(f"  fertig: {r}", flush=True)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="EPO-Patent-Stümpfe mit Abstract nachreichern (nicht-destruktiv).")
    ap.add_argument("--db", required=True, help="Pfad zur echten scraper.db (Heim)")
    ap.add_argument("--max-pro-lauf", type=int, default=300, help="Deckel je Lauf (OPS-Drossel; resumierbar)")
    ap.add_argument("--drossel-sek", type=float, default=4.5, help="Sekunden zwischen Abrufen (RobotDetected)")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"FEHLER: scraper.db nicht gefunden: {a.db}", file=sys.stderr)
        return 2
    offen = zu_backfillende(a.db, limit=None)
    print(f"=== EPO-Abstract-Backfill === {a.db}")
    print(f"  {len(offen)} Patent-Stümpfe offen (kurzer Text) — reichere bis {a.max_pro_lauf} an …")
    backfill(a.db, max_pro_lauf=a.max_pro_lauf, drossel_sek=a.drossel_sek)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
