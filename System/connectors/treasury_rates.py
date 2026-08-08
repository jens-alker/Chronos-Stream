"""
treasury_rates.py — Konnektor für die risikofreie Zinsreihe rf(t) (US-Treasury-Par-Yield-Curve).

Dieser Konnektor liefert eine ECHTE, zeitvariable risikofreie Zinsreihe rf(t) — der US-Treasury-Endpoint
kommt durch die Egress-Policy (live 200 bestätigt), anders als FRED. Bislang war der Diskontsatz fest
verdrahtet bzw. Fixture; hier wird er zur echten, tagesaktuellen Reihe.

**Keine Insel:** dies ist der rf(t)-PRODUZENT (Ingestion/Konnektor-Schicht). Der KONSUMENT ist die
Analyse-/Bewertungs-Schicht, die einen Diskontsatz braucht — der Konnektor füllt nur den externen
`r`-Parameter, er definiert keine Bewertungslogik.

**Quelle:** home.treasury.gov Daily Treasury Par Yield Curve (CSV je Jahr), keyless. Die Par-Yields sind die
kanonische risikofreie Kurve (1 Mo … 30 Yr). Werte in PROZENT → hier auf Dezimal normiert (0.93 → 0.0093).

**PIT (3.6):** Treasury-Renditen werden NICHT revidiert → t_event = t_disclosed = Handelstag. `rf_am_stichtag`
nimmt die jüngste Rate ON-OR-BEFORE dem Stichtag (kein Look-Ahead). Der reine Parser/PIT-Kern ist offline
testbar (echtes CSV-Schema), der Live-Abruf ist gated.

Nur Standardbibliothek + curl.
"""
import subprocess

_BASE = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
# Kanonische Tenor-Spalten des Treasury-CSV (Reihenfolge = CSV-Header).
TENORE = ("1 Mo", "2 Mo", "3 Mo", "6 Mo", "1 Yr", "2 Yr", "3 Yr", "5 Yr", "7 Yr", "10 Yr", "20 Yr", "30 Yr")
_DEFAULT_TENOR = "10 Yr"                 # 10-jährige = Standard-Risikofrei-Anker für den Aktien-DCF


def _iso(datum_us):
    """'MM/DD/YYYY' -> 'YYYY-MM-DD'. None bei kaputtem/leerem Datum (fail-closed, kein Fake-Datum)."""
    teile = (datum_us or "").strip().split("/")
    if len(teile) != 3:
        return None
    m, d, y = teile
    if not (m.isdigit() and d.isdigit() and y.isdigit()) or len(y) != 4:
        return None
    mi, di = int(m), int(d)
    if not (1 <= mi <= 12 and 1 <= di <= 31):
        return None
    return f"{y}-{mi:02d}-{di:02d}"


def _split_csv_zeile(zeile):
    """Eine CSV-Zeile robust splitten (die Header-Tenöre sind in Anführungszeichen: \"1 Mo\"). Minimal-Parser
    für das flache Treasury-Schema (keine eingebetteten Kommata in Werten) — Standardbib statt csv-Modul-Overhead."""
    felder, cur, in_q = [], [], False
    for ch in zeile:
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            felder.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    felder.append("".join(cur))
    return [f.strip() for f in felder]


def parse_treasury_csv(text):
    """REIN (offline testbar): das Treasury-Par-Yield-CSV -> sortierte Liste
    [{date:'YYYY-MM-DD', raten:{tenor: dezimal}}], aufsteigend nach Datum. Werte PROZENT→Dezimal (0.93→0.0093).
    Zeilen mit kaputtem Datum / N/A-Werten werden robust übersprungen (fail-closed, kein Fake-0)."""
    zeilen = [z for z in (text or "").splitlines() if z.strip()]
    if not zeilen:
        return []
    header = _split_csv_zeile(zeilen[0])
    # Spaltenindex je bekanntem Tenor (Header kann Reihenfolge/Umfang ändern → aus dem Header lesen, nicht raten).
    idx = {t: header.index(t) for t in TENORE if t in header}
    out = []
    for z in zeilen[1:]:
        f = _split_csv_zeile(z)
        if not f or not f[0]:
            continue
        iso = _iso(f[0])
        if not iso:
            continue
        raten = {}
        for t, i in idx.items():
            if i < len(f) and f[i] not in ("", "N/A"):
                try:
                    raten[t] = float(f[i]) / 100.0          # Prozent -> Dezimal
                except ValueError:
                    continue                                # unparsebarer Wert -> Tenor fehlt (kein Fake)
        if raten:
            out.append({"date": iso, "raten": raten})
    out.sort(key=lambda r: r["date"])
    return out


def rf_am_stichtag(rows, stichtag, tenor=_DEFAULT_TENOR):
    """REIN/PIT: die risikofreie Rate (Dezimal) des `tenor` am jüngsten Handelstag ON-OR-BEFORE `stichtag`
    (kein Look-Ahead). `rows` aus `parse_treasury_csv` (aufsteigend). None, wenn kein Datum ≤ Stichtag den
    Tenor trägt (fail-closed — der Aufrufer darf dann NICHT still auf einen Default-Zins ausweichen)."""
    stichtag = (stichtag or "")[:10]
    wert = None
    for r in rows:                                          # aufsteigend → letzter ≤ Stichtag gewinnt
        if r["date"] <= stichtag and tenor in r["raten"]:
            wert = r["raten"][tenor]
        elif r["date"] > stichtag:
            break
    return wert


# ------------------------------------------------------------------ #
# Live (gated) — je Jahr ein CSV (keyless). Rates sind historisch/immutabel → per-Jahr cachebar.
# ------------------------------------------------------------------ #
_cache = {}                                                 # {jahr: rows} — Prozess-lokal (ein Abruf je Jahr)


def fetch_yield_curve(jahr, timeout=30, _cache_nutzen=True):
    """LIVE: die Daily-Par-Yield-Curve eines Jahres -> `parse_treasury_csv`-Rows. Keyless. Prozess-Cache je
    Jahr (immutable Historie). Wirft bei rc≠0 / leerer/ Nicht-CSV-Antwort (fail-loud, kein stiller Leer-Cache)."""
    if _cache_nutzen and jahr in _cache:
        return _cache[jahr]
    url = (f"{_BASE}/{jahr}/all?type=daily_treasury_yield_curve&field_tdr_date_value={jahr}")
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                         capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"Treasury nicht erreichbar: rc={out.returncode} {out.stderr[:120]}")
    body = out.stdout.strip()
    if not body or not body.lower().startswith("date"):     # echtes CSV beginnt mit dem 'Date'-Header
        raise RuntimeError(f"Treasury: unerwartete Antwort (kein CSV-Header): {body[:80]}")
    rows = parse_treasury_csv(body)
    if _cache_nutzen:
        _cache[jahr] = rows
    return rows


def fetch_rf(stichtag, tenor=_DEFAULT_TENOR, timeout=30):
    """LIVE + PIT: die risikofreie Rate (Dezimal) am Stichtag. Lädt das Stichtags-Jahr (und bei sehr frühen
    Stichtagen zusätzlich das Vorjahr, falls der Jahresanfang noch keinen Handelstag ≤ Stichtag hat) und nimmt
    `rf_am_stichtag`. -> float | None (None = keine Rate ≤ Stichtag; der DCF darf dann NICHT still defaulten)."""
    jahr = int(stichtag[:4])
    rows = fetch_yield_curve(jahr, timeout=timeout)
    rf = rf_am_stichtag(rows, stichtag, tenor)
    if rf is None:                                          # Jahresanfang vor dem ersten Handelstag → Vorjahr
        rows_prev = fetch_yield_curve(jahr - 1, timeout=timeout)
        rf = rf_am_stichtag(rows_prev, stichtag, tenor)
    return rf
