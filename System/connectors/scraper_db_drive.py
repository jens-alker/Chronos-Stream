"""
scraper_db_drive.py — scraper.db → Google-Drive-Sync (Konzept B, dünner Wrapper über `db_drive`).

Konzept B (`Kontext/Konzept_B_ScraperDB-Drive-Sync.md` §8): die 70k-Doc-`scraper.db` (Signal-Rohquelle)
lebt zu Hause; dieser Connector hält eine **automatische, verifizierte, reclaim-feste** Drive-Kopie —
Richtung home → Drive → cloud, **Home ist und bleibt die Wahrheit**, die Cloud restauriert read-only.

KEINE INSEL (Fable-B8): die Transport-Mechanik (Versionierung, gz, Read-back-Verifikation VOR Löschung,
Retention-Rotation) ist der geteilte Kern `db_drive.sync_db`/`restore_db` (delegiert an `gdrive.py`);
hier leben NUR die scraper-spezifischen Auflagen:
  - **Snapshot (Fable-B12):** `VACUUM INTO` temp (konsistent auch bei laufendem WAL-Scraper),
    `busy_timeout` + Retry; ein Snapshot-Fehler kippt NICHTS (fail-safe — der Scraper läuft weiter).
  - **pre_check (Fable-B10):** `PRAGMA quick_check` auf dem Snapshot (NIE übergehbar) + documents-
    Zeilenzahl-Monotonie gegen den letzten Upload (Drive-Manifest) → fail-loud („lokal korrupt/entleert").
  - **Rollen-Gate (Fable-B9):** Upload NUR bei `MTF_SCRAPER_SYNC_ROLE=home` — nur die Heim-Maschine
    (Wahrheit) lädt hoch; sonst inaktiv mit fail-loud-Hinweis.
  - **Drive-Monotonie-Guard (Fable-B9):** Upload verweigert, wenn das Drive-Manifest jünger/größer ist
    (doc_count/timestamp) als der lokale Stand — außer explizitem `force` (die Cloud darf die
    Heim-Wahrheit nicht wegrotieren).
  - **Manifest (Gemini-B2/B3):** `scraper_db_manifest.json` (sha256 · timestamp · doc_count ·
    schema_version · datei); der Cloud-Restore liest ZUERST das kleine Manifest und lädt die 78-MB-Datei
    NUR bei Hash-Abweichung; **Schema-Version-Guard** (Restore bricht fail-loud ab, wenn die Home-DB eine
    neuere `PRAGMA user_version` trägt, als der Cloud-Pfad versteht).
  - **Orphan-Cleanup (Gemini-B1):** verwaiste `.gz`-Fragmente/Upload-Leichen vor jedem Upload entfernen.
  - **Gestaffelte Retention (Fable-B10):** `retention` tägliche Versionen (Default 2, geteilter Kern)
    + 1 Wochenstand (`scraper_db_woche_<jahr>W<ww>.db.gz`, eigener Präfix, von der Rotation unberührt).
  - **Restore-Schutz (Home=Wahrheit):** nie eine größere lokale DB blind überschreiben — fail-loud;
    `force` erst nach Sicherung (Rename, nichts gelöscht). Der Ersatz wird IMMER erst hash-verifiziert,
    DANN gesichert + atomar ersetzt (die lokale Kopie wird vor verifiziertem Ersatz nie angefasst).

**Fluss-Doku (Gesamtschau-G2):** die scraper.db-Drive-Kopie (`makro_scraper_db`) ist eine home→cloud
READ-ONLY-Kopie; `makro_sammel_cloud` bleibt die cloud→home-Merge-Quelle. NIE die restaurierte scraper.db
als Sammel-DB zurückmergen. Aufsicht (Fable-B11): `betrieb_aufsicht.drive_stand_frische_pruefung` bewertet
das Manifest-Alter („Stille ≠ Grün").

Der ECHTE Drive-Round-Trip ist home/creds-gated (Schein-Test-Riegel); offline getestet sind Snapshot,
Guards, Manifest, Round-Trip gegen einen Fake-gdrive. Nur Standardbibliothek.
"""
import gzip
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

import db_drive

_DRIVE_ORDNER = "makro_scraper_db"
_PRAEFIX = "scraper_db_v"                       # tägliche Versionen: scraper_db_v0001.db.gz
_WOCHE_PRAEFIX = "scraper_db_woche_"            # ein Wochenstand:    scraper_db_woche_2026W32.db.gz
_MANIFEST_NAME = "scraper_db_manifest.json"
_FAMILIE = "scraper_db_"                        # der ganze Namensraum dieses Syncs (Orphan-Scan)
_RETENTION = 2                                  # täglich (der Wochenstand kommt on top — Fable-B10)

# Gemini-B3: die scraper.db-Schema-Version (`PRAGMA user_version`), die der CLOUD-Auswertungspfad
# versteht. Die Home-scraper.db trägt aktuell user_version=0 (SQLite-Default; scraper.py setzt keine).
# Nach einer Home-Schema-Migration MUSS home `PRAGMA user_version` erhöhen und diese Konstante erst nach
# dem Nachziehen des Cloud-Pfads angehoben werden — bis dahin bricht der Restore kontrolliert ab.
VERSTANDENE_SCHEMA_VERSION = 0

_RE_VERSION = re.compile(re.escape(_PRAEFIX) + r"\d{4}\.db\.gz")
_RE_WOCHE = re.compile(re.escape(_WOCHE_PRAEFIX) + r"\d{4}W\d{2}\.db\.gz")


def _jetzt_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ Snapshot (Fable-B12)

def snapshot_erstellen(db_pfad, versuche=3, busy_timeout_ms=15000):
    """Konsistenter Snapshot der (ggf. laufend beschriebenen WAL-)DB via `VACUUM INTO` in eine
    Temp-Datei. `busy_timeout` + Retry mit Backoff. -> Temp-Pfad ODER None (fail-safe: ein
    Snapshot-Fehler kippt den Scraper NIE — nur laute Warnung, kein Upload)."""
    import tempfile
    import time
    letzte = None
    for i in range(max(1, versuche)):
        fd, tmp = tempfile.mkstemp(suffix=".scraper_snapshot.db")
        os.close(fd)
        os.unlink(tmp)                                   # VACUUM INTO verlangt eine NICHT-existente Zieldatei
        try:
            conn = sqlite3.connect(db_pfad)
            try:
                conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
                conn.execute("VACUUM INTO ?", (tmp,))
            finally:
                conn.close()
            return tmp
        except Exception as e:                           # noqa: BLE001 — fail-safe, aber laut
            letzte = e
            if os.path.exists(tmp):
                os.unlink(tmp)
            if i < versuche - 1:
                time.sleep(1 + i)                        # 1s, 2s — Lock/Blip abklingen lassen
    print(f"  ⚠ scraper.db-Snapshot fehlgeschlagen ({type(letzte).__name__}: {str(letzte)[:100]}) — "
          f"Sync übersprungen (fail-safe, Fable-B12: der Scraper wird NIE gekippt).")
    return None


def _quick_check_und_count(snapshot_pfad):
    """`PRAGMA quick_check` (fail-loud bei allem außer 'ok') + documents-Zeilenzahl + `PRAGMA
    user_version` des Snapshots. Eine scraper.db OHNE documents-Tabelle ist korrupt → wirft (fail-loud)."""
    conn = sqlite3.connect(snapshot_pfad)
    try:
        r = conn.execute("PRAGMA quick_check").fetchone()
        if not r or r[0] != "ok":
            raise RuntimeError(f"PRAGMA quick_check fehlgeschlagen: {r!r} — korrupter Snapshot, "
                               f"Upload verweigert (Fable-B10).")
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        sv = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return int(n), int(sv)


# ------------------------------------------------------------------ Manifest (Gemini-B2/B3)

def _lies_manifest(g, at, dateien, force=False):
    """Das Drive-Manifest lesen. Fehlt es → None (Kaltstart/Legacy). Ist es UNLESBAR/korrupt → fail-loud
    (Monotonie/Herkunft nicht verifizierbar); `force=True` behandelt es wie fehlend (laut)."""
    fid = dateien.get(_MANIFEST_NAME)
    if not fid:
        return None
    try:
        man = json.loads(g.datei_lesen(at, fid).decode("utf-8"))
        if not isinstance(man, dict):
            raise ValueError(f"Manifest ist kein Objekt: {type(man).__name__}")
        return man
    except Exception as e:                               # noqa: BLE001
        if force:
            print(f"  ⚠ {_MANIFEST_NAME} unlesbar ({type(e).__name__}) — force: wie fehlend behandelt.")
            return None
        raise RuntimeError(f"{_MANIFEST_NAME} unlesbar/korrupt ({type(e).__name__}: {str(e)[:100]}) — "
                           f"Monotonie/Herkunft nicht verifizierbar, fail-loud (force=True übergeht).")


def _sha256_bytes(roh):
    return hashlib.sha256(roh).hexdigest()


def _sha256_datei(pfad):
    with open(pfad, "rb") as f:
        return _sha256_bytes(f.read())


# ------------------------------------------------------------------ Guards (Fable-B9/B10)

def _mach_pre_check(fern, toleranz, force, zustand):
    """pre_check-Hook für `db_drive.sync_db` (Fable-B10): quick_check (NIE übergehbar — eine korrupte
    Datei wird unter keinen Umständen hochgeladen) + documents-Zeilenzahl-Monotonie gegen den letzten
    Upload (Drive-Manifest): Count ≥ letzter Upload − Toleranz, sonst fail-loud („lokal entleert/
    korrupt"); nur `force` übergeht die Monotonie (bewusstes Schrumpfen), nie den quick_check.
    Schreibt doc_count/schema_version nach `zustand` (für das Manifest nach dem Upload)."""
    def pre_check(pfad):
        n, sv = _quick_check_und_count(pfad)
        zustand["doc_count"], zustand["schema_version"] = n, sv
        fc = (fern or {}).get("doc_count")
        if isinstance(fc, int) and n < fc - toleranz and not force:
            raise RuntimeError(
                f"Zeilenzahl-Monotonie verletzt (Fable-B10): lokale documents={n} < letzter Upload "
                f"{fc} − Toleranz {toleranz} — lokale DB entleert/korrupt? Upload verweigert "
                f"(force=True übergeht bewusst).")
    return pre_check


def _mach_monotonie(fern, jetzt, force):
    """monotonie-Hook für `db_drive.sync_db` (Fable-B9, Drive-Monotonie-Guard): ist der Drive-Stand
    (Manifest-timestamp) JÜNGER als dieser Lauf, schreibt gerade eine andere (die Heim-)Maschine —
    die Cloud darf die Heim-Wahrheit nicht wegrotieren. Nur `force` übergeht."""
    def monotonie(bestand):
        ts = str((fern or {}).get("timestamp") or "")
        if ts and str(jetzt) < ts and not force:
            raise RuntimeError(
                f"Drive-Monotonie-Guard (Fable-B9): Drive-Manifest ({ts}) ist jünger als dieser Stand "
                f"({jetzt}) — eine andere Maschine hat zuletzt geschrieben. Upload verweigert "
                f"(force=True übergeht bewusst).")
    return monotonie


# ------------------------------------------------------------------ Orphan-Cleanup (Gemini-B1)

def _orphan_cleanup(g, at, dateien):
    """Verwaiste `.gz`-Fragmente/Upload-Leichen im `scraper_db_`-Namensraum entfernen (Gemini-B1:
    Verbindungsabbrüche bei 78 MB dürfen das Kontingent nicht unbemerkt erschöpfen): alles, was weder
    gültige Tages-Version noch Wochenstand noch das Manifest ist. Best-effort (Löschfehler kippt nichts);
    entfernte Namen werden aus `dateien` genommen. -> Anzahl entfernt."""
    n = 0
    for nm in sorted(dateien):
        if not nm.startswith(_FAMILIE):
            continue                                     # fremde Dateien nie anfassen
        if nm == _MANIFEST_NAME or _RE_VERSION.fullmatch(nm) or _RE_WOCHE.fullmatch(nm):
            continue
        try:
            g.datei_loeschen(at, dateien[nm])
            del dateien[nm]
            n += 1
        except Exception:                                # noqa: BLE001 — best-effort
            pass
    if n:
        print(f"  Orphan-Cleanup: {n} verwaiste Fragmente entfernt (Gemini-B1).")
    return n


# ------------------------------------------------------------------ Wochenstand (Fable-B10)

def _wochenstand(g, at, ordner_id, roh, jetzt):
    """Gestaffelte Retention: EIN Wochenstand `scraper_db_woche_<isojahr>W<ww>.db.gz` zusätzlich zu den
    täglichen Versionen (eigener Präfix → von der täglichen Rotation unberührt). Nur einmal je ISO-Woche
    hochgeladen (Read-back-verifiziert); ältere Wochenstände werden erst NACH Verifikation gelöscht.
    Fail-safe: der Wochenstand ist Zusatz-Redundanz — ein Fehler kippt den Sync nicht (laute Warnung)."""
    try:
        import datetime as _dt
        iy, iw, _ = _dt.date.fromisoformat(str(jetzt)[:10]).isocalendar()
        name = f"{_WOCHE_PRAEFIX}{iy}W{iw:02d}.db.gz"
        vorhandene = g.liste_ordner(at, ordner_id, name_praefix=_WOCHE_PRAEFIX)
        if name in vorhandene:
            return name                                  # Wochenstand dieser Woche existiert schon
        gz = gzip.compress(roh)
        neu_id = g.datei_anlegen(at, name, gz, ordner_id, mime="application/gzip")
        try:
            if gzip.decompress(g.datei_lesen(at, neu_id)) != roh:
                raise g.DriveFehler("Wochenstand-Read-back weicht ab (truncated/korrupt).")
        except Exception:
            try:
                g.datei_loeschen(at, neu_id)
            except Exception:                            # noqa: BLE001
                pass
            raise
        for nm, fid in vorhandene.items():               # erst NACH Verifikation: alte Wochen weg
            if nm != name:
                try:
                    g.datei_loeschen(at, fid)
                except Exception:                        # noqa: BLE001
                    pass
        return name
    except Exception as e:                               # noqa: BLE001 — Zusatz-Redundanz, fail-safe
        print(f"  ⚠ Wochenstand fehlgeschlagen ({type(e).__name__}: {str(e)[:100]}) — "
              f"tägliche Versionen sind unberührt.")
        return None


# ------------------------------------------------------------------ Sync (home → Drive)

def sync_scraper_db(db_pfad, force=False, retention=_RETENTION, toleranz=0, jetzt=None,
                    rolle=None, snapshot_versuche=3, _gdrive=None):
    """Der scraper.db-Sync (Konzept B §8): Rollen-Gate → VACUUM-INTO-Snapshot → Orphan-Cleanup →
    Manifest lesen → Guards (quick_check/Monotonie) → `db_drive.sync_db` (Read-back + Retention) →
    Manifest schreiben → Wochenstand. -> Status-dict.

    Fail-Verhalten: Rollen-Gate/Snapshot-Fehler geben ein Status-dict zurück (fail-safe — der Scraper
    wird nie gekippt; der AUFRUFER hängt den Sync als Post-Prozess an den erfolgreichen Zyklus);
    Guard-Verstöße (quick_check, Monotonie, Read-back) werfen fail-loud. `jetzt`/`rolle`/`_gdrive`
    injizierbar (Tests offline; Rolle sonst aus `MTF_SCRAPER_SYNC_ROLE`)."""
    rolle = rolle if rolle is not None else os.environ.get("MTF_SCRAPER_SYNC_ROLE", "")
    if rolle != "home":
        print(f"  ⚠ scraper.db-Drive-Sync INAKTIV (Rollen-Gate Fable-B9): MTF_SCRAPER_SYNC_ROLE="
              f"{rolle!r} != 'home'. Nur die Heim-Maschine (Wahrheit) lädt hoch; die Cloud restauriert "
              f"read-only (`restore_scraper_db`).")
        return {"status": "inaktiv", "grund": f"rolle={rolle!r}"}
    jetzt = jetzt or _jetzt_iso()
    snap = snapshot_erstellen(db_pfad, versuche=snapshot_versuche)
    if snap is None:
        return {"status": "snapshot_fehler", "grund": "VACUUM INTO fehlgeschlagen (fail-safe, siehe Warnung)"}
    try:
        g = db_drive._modul(_gdrive)
        at = db_drive._at(g)
        ordner_id = g.ordner_finden_oder_anlegen(at, _DRIVE_ORDNER)
        alle = g.liste_ordner(at, ordner_id)
        n_orphans = _orphan_cleanup(g, at, alle)
        fern = _lies_manifest(g, at, alle, force=force)
        zustand = {}
        name = db_drive.sync_db(snap, _DRIVE_ORDNER, _PRAEFIX, retention=retention,
                                pre_check=_mach_pre_check(fern, toleranz, force, zustand),
                                monotonie=_mach_monotonie(fern, jetzt, force), _gdrive=g)
        with open(snap, "rb") as f:
            roh = f.read()
        sha = _sha256_bytes(roh)
        manifest = {"sha256": sha, "timestamp": jetzt, "doc_count": zustand["doc_count"],
                    "schema_version": zustand["schema_version"], "datei": name}
        g.datei_anlegen(at, _MANIFEST_NAME, json.dumps(manifest, sort_keys=True).encode("utf-8"),
                        ordner_id, mime="application/json")
        alt_mid = alle.get(_MANIFEST_NAME)               # das ALTE Manifest erst NACH dem neuen löschen
        if alt_mid:
            try:
                g.datei_loeschen(at, alt_mid)
            except Exception:                            # noqa: BLE001 — best-effort
                pass
        woche = _wochenstand(g, at, ordner_id, roh, jetzt)
        return {"status": "ok", "datei": name, "doc_count": zustand["doc_count"],
                "schema_version": zustand["schema_version"], "sha256": sha,
                "wochenstand": woche, "orphans_entfernt": n_orphans}
    finally:
        if os.path.exists(snap):
            os.unlink(snap)


# ------------------------------------------------------------------ Restore (Drive → cloud, read-only-Kopie)

def _doc_count_sicher(db_pfad):
    """documents-Count der lokalen DB, None wenn nicht lesbar (dann schützt die Sicherung allein)."""
    try:
        conn = sqlite3.connect("file:" + os.path.abspath(db_pfad).replace("\\", "/") + "?mode=ro", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        return None


def _sichere(pfad, jetzt):
    """Lokale DB per Rename sichern (nichts gelöscht) -> Sicherungs-Pfad."""
    ts = str(jetzt).replace(":", "").replace(" ", "_")
    ziel = f"{pfad}.sicherung_{ts}"
    os.replace(pfad, ziel)
    print(f"  Restore-Sicherung: {pfad} -> {ziel} (nichts gelöscht)")
    return ziel


def restore_scraper_db(ziel_pfad, force=False, verstandene_schema_version=VERSTANDENE_SCHEMA_VERSION,
                       jetzt=None, _gdrive=None):
    """Cloud-Restore der scraper.db (read-only-Kopie der Heim-Wahrheit). Reihenfolge (Gemini-B2/B3):
    1. NUR das kleine Manifest lesen; lokale Kopie hash-gleich → KEIN 78-MB-Download (`status=aktuell`).
    2. Schema-Version-Guard: Drive-`schema_version` > `verstandene_schema_version` → fail-loud (Gemini-B3).
    3. Restore-Schutz (Home=Wahrheit): lokale DB mit MEHR documents als der Drive-Stand → fail-loud
       (`force=True` erzwingt — aber erst nach Sicherung).
    4. Download → gunzip → sha256 GEGEN das Manifest verifizieren (Totalverlust-Riegel) → erst DANN die
       lokale Kopie sichern (Rename) + atomar ersetzen. Ohne Manifest (Kaltstart/Legacy): Fallback auf
    den geteilten `db_drive.restore_db` mit konservativem Schutz-Hook (eine vorhandene lokale DB wird ohne
    Manifest-Beleg nie ohne `force` überschrieben; auch dann erst nach Sicherung). -> Status-dict."""
    g = db_drive._modul(_gdrive)
    at = db_drive._at(g)
    ordner_id = g.ordner_finden_oder_anlegen(at, _DRIVE_ORDNER)
    dateien = g.liste_ordner(at, ordner_id)
    jetzt = jetzt or _jetzt_iso()
    fern = _lies_manifest(g, at, dateien, force=force)
    if fern is None:
        def schutz(lokal, drive_name):
            if os.path.exists(lokal):
                if not force:
                    raise RuntimeError(
                        f"Restore-Schutz: lokale DB {lokal!r} existiert, aber kein Drive-Manifest zum "
                        f"Verifizieren (Kaltstart/Legacy) — kein blindes Überschreiben (force=True + "
                        f"Sicherung übergeht).")
                _sichere(lokal, jetzt)
        ok = db_drive.restore_db(ziel_pfad, _DRIVE_ORDNER, _PRAEFIX, schutz=schutz, _gdrive=g)
        return {"status": "restauriert" if ok else "leer", "manifest": False, "download": bool(ok)}
    sv = fern.get("schema_version")
    if isinstance(sv, int) and sv > verstandene_schema_version:
        raise RuntimeError(
            f"Schema-Version-Guard (Gemini-B3): Drive-Stand schema_version={sv} > verstanden="
            f"{verstandene_schema_version} — Restore fail-loud abgebrochen (den Cloud-Auswertungspfad "
            f"erst auf die Home-Migration nachziehen, dann VERSTANDENE_SCHEMA_VERSION heben).")
    if os.path.exists(ziel_pfad) and _sha256_datei(ziel_pfad) == fern.get("sha256"):
        return {"status": "aktuell", "sha256": fern.get("sha256"), "download": False}
    if os.path.exists(ziel_pfad):
        lokal_n = _doc_count_sicher(ziel_pfad)
        fern_n = fern.get("doc_count")
        # BEWUSST nur doc_count (kein mtime-Vergleich): die mtime einer restaurierten Kopie ist die
        # Wall-Clock des Downloads, nicht der Datenstand — ein mtime-Guard würde jeden Folge-Restore
        # nach Home-Update fälschlich blocken.
        if isinstance(lokal_n, int) and isinstance(fern_n, int) and lokal_n > fern_n and not force:
            raise RuntimeError(
                f"Restore-Schutz (Home=Wahrheit): lokale DB hat documents={lokal_n} > Drive-Stand "
                f"{fern_n} — kein blinder Daten-Rückschritt (force=True erzwingt nach Sicherung).")
    name = fern.get("datei") or ""
    fid = dateien.get(name)
    if not fid:
        raise RuntimeError(f"Manifest referenziert {name!r}, aber die Datei fehlt auf Drive "
                           f"(Rotations-/Upload-Fehler?) — Restore abgebrochen, lokal NICHTS angefasst.")
    roh = gzip.decompress(g.datei_lesen(at, fid))
    if _sha256_bytes(roh) != fern.get("sha256"):
        raise RuntimeError("Restore-Hash weicht vom Manifest ab (truncated/korrupt) — lokal NICHTS "
                           "überschrieben (Totalverlust-Riegel).")
    sicherung = _sichere(ziel_pfad, jetzt) if os.path.exists(ziel_pfad) else None
    # Gemini-B2: eindeutige Temp-Datei IM Zielverzeichnis (gleiches Dateisystem -> atomares os.replace;
    # kein fester Suffix-Namenskonflikt bei konkurrierenden Prozessen).
    import tempfile as _tf
    _d = os.path.dirname(os.path.abspath(ziel_pfad)) or "."
    _fd, tmp = _tf.mkstemp(dir=_d, suffix=".restore_tmp")
    try:
        with os.fdopen(_fd, "wb") as f:
            f.write(roh)
        os.replace(tmp, ziel_pfad)
    except Exception:                                        # noqa: BLE001 — kein tmp-Leak bei Schreibfehler
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"  scraper.db-Restore: {name} ({fern.get('doc_count')} documents, sha verifiziert)")
    return {"status": "restauriert", "datei": name, "doc_count": fern.get("doc_count"),
            "sicherung": sicherung, "download": True}
