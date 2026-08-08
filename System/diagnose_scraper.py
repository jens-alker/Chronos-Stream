#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagnose_scraper.py — read-only Fehler-Signatur der scraper.db (fuer Ferndiagnose eines Ausfalls).

Oeffnet die DB **mode=ro** (sperrt die laufende Scraper-DB NICHT) und druckt kompakt, was ein 24h-Ausfall
verraet: Dateigroesse/Frische, Dokument-/Fakt-Zaehlung, Quellen-Zustand (last_error/fail_count/paused_until),
die letzten Log-Zeilen (v.a. Fehler), meta, Plattenplatz. Nur Standardbibliothek.

Aufruf:  python diagnose_scraper.py [pfad/zur/scraper.db]
(ohne Argument wird die scraper.db NEBEN diesem Skript verwendet)
"""
import os
import sqlite3
import sys
import time

# Standard: die scraper.db neben diesem Skript (kein hartkodierter Altinstallations-Pfad mehr).
STD_PFAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.db")


def _con(pfad):
    # read-only, damit die laufende Scraper-DB nicht gesperrt/veraendert wird
    uri = "file:" + pfad.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _tab(con, name):
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _skalar(con, sql, default="—"):
    try:
        r = con.execute(sql).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error as e:
        return f"<Fehler: {e}>"


def main():
    pfad = sys.argv[1] if len(sys.argv) > 1 else STD_PFAD
    print("=" * 70)
    print("SCRAPER-DIAGNOSE  ·", pfad)
    print("=" * 70)

    if not os.path.exists(pfad):
        print(f"!! Datei existiert NICHT: {pfad}")
        return
    st = os.stat(pfad)
    print(f"Dateigroesse : {st.st_size/1024/1024:.1f} MB")
    print(f"Zuletzt geaendert: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))} "
          f"(vor {(time.time()-st.st_mtime)/3600:.1f} h)")
    try:
        import shutil
        frei = shutil.disk_usage(os.path.dirname(pfad) or ".").free / 1024 / 1024 / 1024
        print(f"Plattenplatz frei: {frei:.1f} GB")
    except Exception as e:
        print(f"Plattenplatz: <{e}>")
    # WAL-Begleitdateien (deuten auf laufende/abgebrochene Transaktion)
    for suf in ("-wal", "-shm"):
        p = pfad + suf
        if os.path.exists(p):
            print(f"  {os.path.basename(p)}: {os.path.getsize(p)/1024/1024:.1f} MB vorhanden")

    try:
        con = _con(pfad)
    except sqlite3.Error as e:
        print(f"\n!! DB laesst sich nicht oeffnen (moeglich: Korruption/Sperre): {e}")
        return

    print("\n--- Zaehlstaende ---")
    if _tab(con, "documents"):
        sql_24h = "SELECT COUNT(*) FROM documents WHERE ingested_at > datetime('now','-1 day')"
        print(f"documents        : {_skalar(con, 'SELECT COUNT(*) FROM documents')}")
        print(f"  letzte Ingestion: {_skalar(con, 'SELECT MAX(ingested_at) FROM documents')}")
        print(f"  neu letzte 24h  : {_skalar(con, sql_24h)}")
    for t in ("facts", "facts_done", "log", "discarded", "doc_embedding"):
        if _tab(con, t):
            print(f"{t:17s}: {_skalar(con, f'SELECT COUNT(*) FROM {t}')}")

    print("\n--- Quellen (sources) ---")
    if _tab(con, "sources"):
        try:
            rows = con.execute(
                "SELECT name, kind, enabled, last_crawl, last_found, fail_count, paused_until, "
                "substr(last_error,1,80) FROM sources ORDER BY fail_count DESC, name").fetchall()
            print(f"{'Quelle':32s} {'an':2s} {'fail':4s} {'letzter Crawl':19s} {'Fehler'}")
            for name, kind, en, lc, lf, fc, pu, err in rows:
                print(f"{(name or '?')[:32]:32s} {('J' if en else 'n'):2s} {str(fc or 0):4s} "
                      f"{str(lc or '—')[:19]:19s} {('PAUSIERT ' if pu else '') + (err or '')}")
        except sqlite3.Error as e:
            print(f"  <Fehler: {e}>")

    print("\n--- Letzte 60 Log-Zeilen (juengste zuletzt) ---")
    if _tab(con, "log"):
        try:
            rows = con.execute("SELECT at, stage, message FROM log ORDER BY id DESC LIMIT 60").fetchall()
            for at, stage, msg in reversed(rows):
                print(f"{str(at)[:19]}  [{stage}] {str(msg)[:180]}")
        except sqlite3.Error as e:
            print(f"  <Fehler: {e}>")

    print("\n--- Fehler-Zeilen im Log (Filter) ---")
    if _tab(con, "log"):
        try:
            rows = con.execute(
                "SELECT at, stage, message FROM log WHERE message LIKE '%Fehler%' OR message LIKE '%error%' "
                "OR message LIKE '%SCHWERER%' OR message LIKE '%tot%' OR message LIKE '%gestoppt%' "
                "ORDER BY id DESC LIMIT 40").fetchall()
            if not rows:
                print("  (keine expliziten Fehler-Zeilen im Log)")
            for at, stage, msg in reversed(rows):
                print(f"{str(at)[:19]}  [{stage}] {str(msg)[:200]}")
        except sqlite3.Error as e:
            print(f"  <Fehler: {e}>")

    print("\n--- meta ---")
    if _tab(con, "meta"):
        try:
            for k, v in con.execute("SELECT key, substr(value,1,120) FROM meta ORDER BY key").fetchall():
                print(f"  {k} = {v}")
        except sqlite3.Error as e:
            print(f"  <Fehler: {e}>")
    con.close()
    print("\n" + "=" * 70)
    print("Bitte diese komplette Ausgabe an Claude zurueckgeben.")


if __name__ == "__main__":
    main()
