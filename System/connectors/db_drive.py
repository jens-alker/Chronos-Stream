"""
db_drive.py — der GETEILTE generische Einzeldatei-SQLite→Drive-Sync (gz, versioniert, Read-back-verifiziert).

Konzept B (`Kontext/Konzept_B_ScraperDB-Drive-Sync.md` §8, Fable-B8 — KEINE INSEL): `sammel_forward.
sync_zu_drive`/`restore_von_drive` waren bereits der Single-SQLite→Drive-Sync MIT Read-back-Verifikation +
Retention 2 + fail-loud. Dieser Connector zieht genau diese Mechanik als EINE Definition heraus;
`sammel_forward` (Sammel-DB, `makro_sammel_cloud`) UND `scraper_db_drive` (scraper.db, Konzept B) sind
dünne Wrapper darüber — keine zweite/dritte Storage-Definition. Delegiert an `gdrive.py` (OAuth-REST);
`_gdrive` ist injizierbar (Tests: Fake in-memory — der ECHTE Drive-Round-Trip bleibt home/creds-gated,
Schein-Test-Riegel; `gdrive.py` selbst ist live verifiziert).

**Totalverlust-Riegel (Claude-QS B1 des Sammel-Sync, hier geerbt):** die eben hochgeladene Datei wird per
`datei_lesen` ZURÜCKGEHOLT und dekomprimiert byte-gegen den lokalen Stand geprüft, BEVOR irgendein Alt-Stand
gelöscht wird. Scheitert die Verifikation, wird die neue (korrupte) Datei entfernt, die Alt-Stände bleiben,
und es wird LAUT geworfen. Retention >= 2 (nie die einzige Kopie riskieren).

**Hooks (Konzept B §8):** `pre_check(db_pfad)` (z. B. PRAGMA quick_check + Zeilenzahl-Monotonie) und
`monotonie(bestand)` (z. B. Drive-Manifest-Guard) laufen VOR dem Upload und werfen fail-loud — der
generische Kern definiert die Transport-Mechanik, die DB-spezifischen Auflagen kommen vom Wrapper.
Nur Standardbibliothek.
"""
import gzip


def _modul(_gdrive=None):
    """Das gdrive-Modul (Default) oder ein injizierter Fake (Tests: in-memory, offline)."""
    if _gdrive is not None:
        return _gdrive
    import gdrive
    return gdrive


def _at(g):
    """Preflight (fail-loud, Jens: die Orchestrierung muss überwachen können) + Access-Token —
    exakt die bisherige `sammel_forward._drive_at`-Mechanik."""
    pf = g.preflight()
    if not pf.get("ok"):
        raise RuntimeError(f"Drive preflight: {pf}")
    return g.access_token()


def _num(nm, praefix):
    """Versionsnummer aus einem Dateinamen `<praefix><n>.db.gz` (alle Ziffern nach dem Präfix)."""
    m = "".join(c for c in nm[len(praefix):] if c.isdigit())
    return int(m) if m else 0


def sync_db(db_pfad, ordner, praefix, retention=2, pre_check=None, monotonie=None, _gdrive=None):
    """Eine SQLite-Datei gz-gepackt versioniert nach Drive legen (jüngste gewinnt beim Restore) + ALTE
    Stände ausdünnen (nur die letzten `retention` behalten). Versionsnummer aus dem Maximum
    (lücken-/kollisionsfest, B4-robust). -> Dateiname `<praefix><n:04d>.db.gz`.

    Claude-QS B1 (Totalverlust-Riegel): der Upload kann mit gültiger File-ID durchlaufen, aber truncated/
    leer ankommen. Vor JEDER Löschung wird die eben hochgeladene Datei per `datei_lesen` ZURÜCKGEHOLT und
    Byte-für-Byte gegen den lokalen Inhalt geprüft (dekomprimiert). Nur bei bestätigter Lesbarkeit werden
    Alt-Stände gelöscht; scheitert die Verifikation, wird die neue (korrupte) Datei gelöscht, die
    Alt-Stände bleiben, und es wird LAUT geworfen.

    Hooks (fail-loud, Konzept B §8): `pre_check(db_pfad)` läuft VOR jedem Drive-Zugriff (lokal korrupt/
    entleert abfangen); `monotonie(bestand)` läuft nach dem Listing VOR dem Upload (`bestand` = dict
    {name: id} — z. B. Drive darf die Heim-Wahrheit nicht wegrotieren). Beide dürfen werfen."""
    g = _modul(_gdrive)
    if pre_check is not None:
        pre_check(db_pfad)                                                # fail-loud VOR jedem Drive-Zugriff
    at = _at(g)
    ordner_id = g.ordner_finden_oder_anlegen(at, ordner)
    bestand = g.liste_ordner(at, ordner_id, name_praefix=praefix)         # dict {name: id}
    if monotonie is not None:
        monotonie(bestand)                                                # fail-loud VOR dem Upload
    n = (max((_num(nm, praefix) for nm in bestand), default=0)) + 1       # aus dem Maximum, nicht len (B4-robust)
    name = f"{praefix}{n:04d}.db.gz"
    with open(db_pfad, "rb") as f:
        roh = f.read()
    gz = gzip.compress(roh)
    neu_id = g.datei_anlegen(at, name, gz, ordner_id, mime="application/gzip")
    # Read-back-Verifikation (B1): der hochgeladene Stand MUSS lesbar + inhaltsgleich sein.
    try:
        zurueck = g.datei_lesen(at, neu_id)
        if gzip.decompress(zurueck) != roh:
            raise g.DriveFehler("Read-back-Inhalt weicht vom lokalen Stand ab (truncated/korrupt).")
    except Exception as e:                                                # noqa: BLE001
        try:
            g.datei_loeschen(at, neu_id)                                  # die korrupte neue Datei entfernen
        except Exception:                                                 # noqa: BLE001
            pass
        raise g.DriveFehler(f"Upload-Verifikation fehlgeschlagen ({type(e).__name__}: {str(e)[:120]}) — "
                            f"Alt-Stände bleiben erhalten, NICHTS gelöscht.")
    # Erst NACH bestätigter Verifikation: alte Stände löschen (die jüngsten `retention` inkl. des neuen behalten)
    alle = sorted(list(bestand.items()) + [(name, None)], key=lambda kv: _num(kv[0], praefix))
    for alt_name, alt_id in alle[:-retention]:
        if alt_id is not None:
            try:
                g.datei_loeschen(at, alt_id)
            except Exception:                                             # noqa: BLE001 — Aufräumen best-effort
                pass
    print(f"  Drive-Sync: {name} ({len(gz)} B gz), Retention {retention} (Read-back verifiziert)")
    return name


def restore_db(db_pfad, ordner, praefix, schutz=None, _gdrive=None):
    """Jüngste versionierte DB (`<praefix><n>.db.gz`, höchster Name) aus Drive holen -> lokal entpacken.
    -> True bei Restore, False wenn kein Bestand (Erstlauf).

    Hook (fail-loud, Konzept B §8): `schutz(lokal_pfad, drive_name)` darf den Restore VOR dem Download
    verweigern (werfen) — Home = Wahrheit: nie eine größere/jüngere lokale DB blind überschreiben."""
    g = _modul(_gdrive)
    at = _at(g)
    ordner_id = g.ordner_finden_oder_anlegen(at, ordner)
    dateien = g.liste_ordner(at, ordner_id, name_praefix=praefix)         # dict {name: id}
    if not dateien:
        return False
    name = sorted(dateien.keys())[-1]                                     # <praefix><n>.db.gz — jüngste
    if schutz is not None:
        schutz(db_pfad, name)                                             # fail-loud-Verweigerung möglich
    roh = g.datei_lesen(at, dateien[name])
    with open(db_pfad, "wb") as f:
        f.write(gzip.decompress(roh))
    print(f"  Drive-Restore: {name} ({len(roh)} B gz)")
    return True
