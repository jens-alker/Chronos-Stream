"""
entschlacke_drive.py — Lösch-Skript für Alt-Artefakte in der Google-Drive-DB (nach dem Umstieg auf EINE DB).

Jens (07.08.): alle Daten in EINER SQLite-DB (`markt_cache__<n>.db`). Der alte gzip-Shard-/Manifest-Cache
(`<bucket>__<hash>.json.gz` + `manifest__<n>.json` + `eod_`-Pendants) ist damit obsolet — und liegt als
VERWAISTER Ballast im Drive-Ordner, weil kein Sync ihn mehr referenziert oder aufräumt. Dieses Skript findet
+ löscht (a) alle Alt-Shards/-Manifeste des früheren Schemas und (b) überschriebene alte DB-Versionen
(`markt_cache__<n>.db` außer der jüngsten).

**KEINE INSEL:** nutzt `gdrive` (list/delete/ordner) + `fundamentals_drive._DATEI_RE`/`_neueste_db` (die EINE
DB-Namenskonvention) — keine zweite Definition. **Sicher by default:** DRY-RUN; erst `--loeschen` löscht real.
Behält IMMER die jüngste `markt_cache__<n>.db`; löscht nur bekannte Alt-Muster (nie unbekannte Dateien).
Home-/creds-gated (`gdrive.preflight`).
"""
import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "connectors"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gdrive                                                        # noqa: E402
import fundamentals_drive as fd                                     # noqa: E402

# Alt-Schema-Muster (das frühere gzip-Shard-/Manifest-System, beide Namespaces "" und "eod_"):
_ALT_SHARD_RE = re.compile(r"\.json\.gz$")                          # <bucket>__<hash>.json.gz / eod_…json.gz
_ALT_MANIFEST_RE = re.compile(r"^(eod_)?manifest__\d+\.json$")      # manifest__<n>.json / eod_manifest__<n>.json


def _ist_alt_artefakt(name):
    """True für eine Datei aus dem OBSOLETEN gzip-Shard-/Manifest-Schema (die nach dem DB-Umstieg Ballast ist)."""
    return bool(_ALT_SHARD_RE.search(name) or _ALT_MANIFEST_RE.match(name))


def plane_entschlackung(at, ordner_id, **_ignored):
    """-> {'loeschen': [(name, id), …], 'behalten': int}. Reine Planung (löscht nichts).

    Löschbar: alle Alt-Shards/-Manifeste (obsoletes Schema) + alle `markt_cache__<n>.db` AUSSER der jüngsten.
    Alles andere (die jüngste DB + unbekannte Fremddateien) bleibt unangetastet."""
    dateien = gdrive.liste_ordner(at, ordner_id)                  # {name: id}
    _neueste_id, neueste_n = fd._neueste_db(dateien)
    zu_loeschen, behalten = [], 0
    for name, fid in dateien.items():
        m = fd._DATEI_RE.match(name)
        if m:                                                     # eine DB-Version: nur die jüngste behalten
            if int(m.group(1)) < neueste_n:
                zu_loeschen.append((name, fid))                  # überschriebene Alt-DB-Version
            else:
                behalten += 1
        elif _ist_alt_artefakt(name):
            zu_loeschen.append((name, fid))                      # Alt-Shard/-Manifest (obsoletes Schema)
        else:
            behalten += 1                                        # unbekannte Fremddatei -> nie anfassen
    # Deterministische, dublettenfreie Reihenfolge.
    gesehen, uniq = set(), []
    for name, fid in sorted(zu_loeschen):
        if fid not in gesehen:
            gesehen.add(fid)
            uniq.append((name, fid))
    return {"loeschen": uniq, "behalten": behalten}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Alt-Shards/-Manifeste + überschriebene DB-Versionen in der "
                                             "Drive-DB löschen (dry-run default).")
    ap.add_argument("--loeschen", action="store_true", help="WIRKLICH löschen (sonst nur anzeigen)")
    a = ap.parse_args(argv)
    st = gdrive.preflight()
    if not st.get("ok"):
        print(f"Drive nicht erreichbar (Preflight '{st.get('schritt')}'): {st.get('fehler')}")
        return 2
    at = gdrive.access_token()
    ordner_id = gdrive.ordner_finden_oder_anlegen(at)
    plan = plane_entschlackung(at, ordner_id)
    loeschen = plan["loeschen"]
    print(f"Drive-DB: {plan['behalten']} Dateien behalten · {len(loeschen)} verwaiste Alt-Versionen "
          f"{'werden gelöscht' if a.loeschen else 'löschbar (dry-run — nichts gelöscht)'}")
    for name, _fid in loeschen[:50]:
        print(f"  {'lösche' if a.loeschen else 'würde löschen'}: {name}")
    if len(loeschen) > 50:
        print(f"  … +{len(loeschen) - 50} weitere")
    if a.loeschen:
        n = 0
        for _name, fid in loeschen:
            try:
                gdrive.datei_loeschen(at, fid)
                n += 1
            except Exception as e:                               # noqa: BLE001 — ein Fehler stoppt den Rest nicht
                print(f"  ⚠ Löschen fehlgeschlagen ({_name}): {e}")
        print(f"  → {n}/{len(loeschen)} gelöscht.")
    else:
        print("  (Zum realen Löschen: --loeschen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
