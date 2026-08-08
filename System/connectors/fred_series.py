"""
fred_series.py — Konnektor: FRED/ALFRED-Reihen -> Modul-12-Input (knappheit_roh).

FRED liefert freie makro-/kapazitäts-Zeitreihen (Kapazitätsauslastung, Lieferzeiten/Supplier
Deliveries, Lager). Die Transformation (FRED-Observations -> knappheit_roh) ist der verifizierbare
Kern und offline getestet. `fetch_series` ist ein dokumentiertes Skelett: es braucht Netzzugang zu
api.stlouisfed.org, der in DIESEM Container per Egress-Policy geblockt ist (403 CONNECT). Live-Lauf
-> breitere Policy oder lokaler Deploy. ALFRED-Vintages für echtes Point-in-Time (kein Look-Ahead).

Abbildung (FRED -> knappheit_roh, Modul 12): je Beobachtung eine Zeile; Modul 12 bildet daraus
bindungsgrad (Perzentil) + richtung. series_id -> (kat_id, version, signal_art) über series_map.
Signalart-Beispiele: TCU/Kapazitätsauslastung -> auslastung; Supplier Deliveries -> lieferzeit;
Lager-Ratio -> lager.  Nur Standardbibliothek.
"""
import subprocess

_BASE = "https://api.stlouisfed.org/fred/series/observations"    # Skelett


def zu_knappheit_roh(series_id, observations, series_map, t_ingest):
    """FRED-Observations [{date, value}] + series_map[series_id] = (kat_id, version, signal_art)
    -> knappheit_roh (je Beobachtung eine Zeile, chronologisch). '.'-Werte (FRED-NA) übersprungen."""
    ziel = series_map.get(series_id)
    if not ziel:
        return []
    kat_id, version, signal_art = ziel
    out = []
    for ob in observations:
        wert = ob.get("value")
        if wert in (None, "", "."):        # FRED kodiert fehlende Werte als "."
            continue
        try:
            wert_num = float(wert)
        except (TypeError, ValueError):
            continue
        datum = ob.get("date")
        out.append({
            "kat_id": kat_id, "version": version, "signal_art": signal_art,
            "wert_numerisch": wert_num,
            "t_event": datum,
            "t_disclosed": ob.get("realtime_start", datum),   # ALFRED-Vintage, sonst Beobachtungsdatum
            "t_ingest": t_ingest,
        })
    return out


def fetch_series(series_id, key, seit=None, vintage=None, timeout=30):
    """SKELETT — FRED/ALFRED-Observations. Braucht Netzzugang zu api.stlouisfed.org (in diesem
    Container per Egress-Policy geblockt) + FRED-API-Key. `vintage` (realtime) = echtes PIT (ALFRED).
    Rückgabe: Liste [{date, value, realtime_start}] für zu_knappheit_roh()."""
    url = f"{_BASE}?series_id={series_id}&api_key={key}&file_type=json"
    if seit:
        url += f"&observation_start={seit}"
    if vintage:                            # ALFRED: nur was zu diesem Vintage bekannt war
        url += f"&realtime_start={vintage}&realtime_end={vintage}"
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                         capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"FRED nicht erreichbar (Egress-Policy?): rc={out.returncode} "
                           f"{out.stderr[:200]}")
    import json
    return json.loads(out.stdout).get("observations", [])
