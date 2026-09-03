"""
retro_kat_map_breit.py — Breite, survivorship-freie Symbol→Kategorie-Map (industrie-getrieben).

Der `survivorship_frei=False`-Blocker der Anker-Retro (retro_anker_run) kam vom Gold-Universum (aktive
Mega-Caps, hand-verifiziert). Diese Map hebt ihn: statt tausende Ticker von Hand zu mappen (Jens-Stunden =
teuer), wird eine **kleine hand-verifizierte Branchen→Kategorie-Lookup** (`_INDUSTRIE_KATEGORIE`) über EODHDs
Klassifikationsfeld `General::GicSubIndustry` maschinell auf das **volle aktive + delistete Universum**
expandiert (Zielfunktion: kleiner Jens-Einsatz, Maschinenzeit trägt die Abdeckung).

**Survivorship-frei belegt:** delistete Namen tragen dieselbe GIC-Klassifikation (z. B. XLNX/ATML/CY →
`Semiconductors`, `IsDelisted=True`) → sie fallen NICHT aus der Kohorte. Das ist der Unterschied zur
Gold-Aktiv-Liste (0 delistete).

**Deterministisch, kein LLM → kein Outcome-/Modell-Vintage-Leck** (konsistent mit der leckfreien Anker-
Eigenschaft): die Branchen→Kategorie-Zuordnung ist *Kategorie-Definition* (erlaubt), KEIN Outcome-Wissen. ⚠
**Ehrlich (QS-Gemini-B1):** EODHD liefert die HEUTIGE GIC-Klassifikation; GICS-Taxonomien werden über Jahre
umgruppiert → die Zugehörigkeit ist „as-of-heute", nicht per-Stichtag-PIT (ein **Taxonomie-Vintage**, bewusste
Approximation — weit kleiner als ein Outcome-Leck, aber nicht null; parallel zum `wert_vintage` der Anker-Retro).

**Zwei Klassifikationswege:** (A) **CRISP** — `GicSubIndustry` allein für **Halbleiter/Kupfer/Rechenzentrum**
(eigene Sub-Industries: `Semiconductors`, `Copper`, `Data Center REITs`). (B) **FEIN** — für **Transformatoren/
Stromnetz** ist GicSubIndustry zu grob & inkonsistent (GEV→`Independent Power Producers`; Quanta UND Fluor→
`Construction & Engineering`), daher **GIC-Kandidatenmenge + PFLICHT-Keyword-Guard** aus der deterministischen
Firmenbeschreibung (`klassifiziere_fein`, an echten EODHD-Descriptions kalibriert). Präzisionsorientiert:
generisch beschriebene/misklassifizierte Namen (GEV, National Grid) werden bewusst NICHT gefangen → bleiben
kuratiert (`GOLD_KAT_MAP` ergänzt via `kombiniere`) statt falsch zugeordnet. So kommen Trafo/Netz jetzt
größtenteils survivorship-frei aus der feinen Klassifikation (nicht mehr nur aktiv-only kuratiert).

Der Live-Batch ist gated + **gecacht** (`.cache_klassifikation.json`, gitignored → reproduzierbar/quotaschonend)
+ **stratifiziert gedeckelt** (`n_aktiv`/`n_delistet` steuern Wall-Clock). Reiner Kern (Lookup/Assemblierung)
injizierbar/offline getestet. Nur Standardbibliothek + curl-Konnektor.

⚠️ **QS-Verdikt (doppelt geprüft, 2026-07-23):** Diese Map
in ihrer HYBRID-Form + über die aktuelle Konsum-Naht **hebt den survivorship-Blocker NOCH NICHT** — drei
zusammenwirkende Gründe: (B1) `retro_voll_run._outcome_map` hartkodiert `delisted=False` → der Flag erreicht
die Kohorte nie; (B2) Hybrid stellt survivorship-freie breite Kategorien gegen aktiv-only kuratierte (Trafo/
Netz) → gerichteter Vergleichsbias ZWISCHEN Kategorien; (Dichte) bei machbarem Sample findet der Zufalls-Scan
0 delistete Kategorie-Mitglieder (Kategorien sind Nadeln im Heuhaufen → braucht Voll-Universum-Klassifikation).
Der reine Klassifikations-Kern ist sauber; der Weg zum survivorship-freien Anker ist der korrigierte Bau.
"""
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = os.path.dirname(_HERE)
for _p in (os.path.join(_SYS, "connectors"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# (A) CRISP: GicSubIndustry -> kat_id (Halbleiter/Kupfer/Rechenzentrum sind eindeutig; nur gic nötig).
_INDUSTRIE_KATEGORIE = {
    "Semiconductors": "Halbleiter",
    "Semiconductor Materials & Equipment": "Halbleiter",
    "Copper": "Kupfer",
    "Data Center REITs": "Rechenzentrum",
}
_CRISP_KATEGORIEN = set(_INDUSTRIE_KATEGORIE.values())         # {Halbleiter, Kupfer, Rechenzentrum}

# (B) FEIN: Trafo/Netz — GicSubIndustry ist zu grob & inkonsistent (GEV→'Independent Power Producers';
# Quanta UND Fluor→'Construction & Engineering'). Deshalb GIC-Kandidatenmenge + PFLICHT-Keyword-Guard aus
# der (deterministischen) Firmenbeschreibung. Kalibriert an echten EODHD-Descriptions (kein LLM → leckfrei;
# Kategorie-DEFINITION, kein Outcome-Wissen). Präzisionsorientiert: generisch beschriebene Namen (z. B. GEV,
# National Grid) werden bewusst NICHT gefangen → bleiben kuratiert (GOLD_KAT_MAP) statt falsch zugeordnet.
def _norm(s):
    """Casefold + Whitespace-Kollaps (QS-Claude-B7): GIC-Vergleich robust gegen Casing/Spacing-Drift."""
    return " ".join((s or "").split()).casefold()


_ELEKTRO_GIC = {_norm(g) for g in ("Electrical Components & Equipment", "Heavy Electrical Equipment")}
_ENGINEERING_GIC = {_norm("Construction & Engineering")}
# PRODUKT-spezifische Trafo-Keywords (QS-Claude-B1/B2): NUR echte Produkt-Nomen — 'electrification' RAUS
# (Themenwort: 'vehicle/rail electrification'). Diese werden ZUERST geprüft, damit ein Trafo-Hersteller,
# dessen Beschreibung auch 'grid'/'transmission' nennt (die Mehrheit), nicht fälschlich Stromnetz wird.
_TRAFO_KW = ("transformer", "switchgear", "circuit breaker")
# Netz-Keywords im Elektro-Pfad (Firma ist bereits E-Ausrüster): 'transmission' NUR als 'power transmission'/
# 'transmission line' (nicht bare → kein 'data transmission'); 'cable' bleibt (Nexans 'cables').
_NETZ_KW_ELEKTRO = ("cable", "grid", "power transmission", "transmission line", "power line", "distribution network")
# Netz-Keywords im Engineering-Pfad: STRIKT elektrisch (schließt Fluor/Gas-/Wasser-/Logistikbauer aus).
_NETZ_KW_ENG = ("power transmission", "transmission line", "power line", "electric power", "electrical grid", "power grid")
_FEIN_KATEGORIEN = {"Transformatoren", "Stromnetz"}
_CACHE_PFAD = os.path.join(_HERE, ".cache_klassifikation.json")


# ------------------------------------------------------------------ #
# Reiner Kern (offline testbar) — Lookup + Assemblierung
# ------------------------------------------------------------------ #
_INDUSTRIE_KATEGORIE_NORM = {_norm(k): v for k, v in _INDUSTRIE_KATEGORIE.items()}


def klassifiziere(gic_subindustry):
    """CRISP: GicSubIndustry -> kat_id (Halbleiter/Kupfer/Rechenzentrum) oder None. Casefold (QS-Claude-B7)."""
    return _INDUSTRIE_KATEGORIE_NORM.get(_norm(gic_subindustry))


def _hat_keyword(description, keywords):
    """Keyword-Guard mit WORTGRENZEN + optionalem Plural-s (`\\b…s?\\b`, kein nackter Substring) →
    matcht „transformer"/„transformers", verhindert aber Teilstring-Falsch-Positive („grid" in „Ingrid",
    „cable" in „cablegram"). Deterministisch, case-insensitiv."""
    d = (description or "").lower()
    return any(re.search(r"\b" + re.escape(k) + r"s?\b", d) for k in keywords)


def klassifiziere_fein(gic_subindustry, description):
    """FEIN: Trafo/Netz aus GIC-Kandidatenmenge + PFLICHT-Keyword-Guard (Beschreibung, Wortgrenzen).
    **PRODUKT-spezifische Trafo-Keywords ZUERST (QS-Claude-B1)** → ein Trafo-Hersteller, dessen Beschreibung
    auch 'grid'/'transmission' nennt, wird nicht fälschlich Stromnetz. Sonst Netz-Keywords → Stromnetz. Engineering-
    Pfad nur mit STRIKT elektrischen Netz-Keywords (Fluor raus). Ohne Treffer: None (Lookalikes fallen weg)."""
    gic = _norm(gic_subindustry)
    if gic in _ELEKTRO_GIC:
        if _hat_keyword(description, _TRAFO_KW):
            return "Transformatoren"                  # Trafo/Schaltanlagen ZUERST (ABB, Hyundai Electric)
        if _hat_keyword(description, _NETZ_KW_ELEKTRO):
            return "Stromnetz"                        # Kabel/Netz-Ausrüster (Nexans, Prysmian)
    elif gic in _ENGINEERING_GIC and _hat_keyword(description, _NETZ_KW_ENG):
        return "Stromnetz"                            # Netzbauer (Quanta ja, Fluor nein)
    return None


_GIC_UNGUELTIG = {"", "n/a", "na", "none", "-", "unknown"}   # leere/Platzhalter-GicSubIndustry


def klassifiziere_gic_direkt(gic_subindustry):
    """VIELE Kategorien (Power/Entkorrelation): die GicSubIndustry SELBST ist die Kategorie — für jedes
    Symbol mit gültiger, nicht-leerer GicSubIndustry. Roher, aber breiter Querschnitt (Dutzende echte
    Branchen-Kategorien) statt der 5 kuratierten Themen. Gültigkeits-Guard gegen leere/Platzhalter-Werte.
    -> kat_id (die normalisierte, aber lesbare GicSubIndustry) | None."""
    g = (gic_subindustry or "").strip()
    if _norm(g) in _GIC_UNGUELTIG:
        return None
    return " ".join(g.split())                       # lesbares, whitespace-normalisiertes Label


def _klassifiziere_symbol(felder, gic_direkt=False):
    """felder={gic, desc} -> kat_id | None: erst CRISP (nur gic), dann FEIN (gic+desc). Mit
    `gic_direkt=True` fällt ein Nicht-Themen-Symbol auf seine GicSubIndustry als Kategorie zurück
    (viele Kategorien statt None) — für den entkorrelierenden Breit-Querschnitt (Power)."""
    gic = felder.get("gic") if isinstance(felder, dict) else felder
    desc = felder.get("desc", "") if isinstance(felder, dict) else ""
    kat = klassifiziere(gic) or klassifiziere_fein(gic, desc)
    if kat is None and gic_direkt:
        kat = klassifiziere_gic_direkt(gic)
    return kat


def baue_breite_map(klassifikation_map, gic_direkt=False):
    """{symbol_id -> {gic, desc}} -> {symbol_id -> kat_id}. CRISP (Halbleiter/Kupfer/RZ) + FEIN (Trafo/Netz).
    `gic_direkt=True`: zusätzlich jedes übrige Symbol nach seiner GicSubIndustry kategorisieren (VIELE
    Kategorien → entkorrelierte Folds → Power für schwache Signale, Jens 24.07.).
    (Rückwärtskompatibel: ein reiner gic-String je Symbol wird als {gic} behandelt; Default = 5 Themen.)"""
    out = {}
    for sid, felder in klassifikation_map.items():
        kat = _klassifiziere_symbol(felder, gic_direkt=gic_direkt)
        if kat:
            out[sid] = kat
    return out


def kombiniere(breit_map, gold_map=None):
    """HYBRID: breite (industrie-getriebene, survivorship-freie) Map ∪ kuratierte Gold-Ticker.
    Die Gold-Ticker (auch der breiten Kategorien) werden ergänzt/bestätigt; Transformatoren/Stromnetz
    kommen NUR von dort. -> {symbol_id -> kat_id}."""
    if gold_map is None:
        from retro_kat_map import kat_map as _gold
        gold_map = _gold()
    aus = dict(breit_map)
    aus.update(gold_map)                 # verifizierte Gold-Ticker ergänzen (kein echter Konflikt: Ticker→feste Kat)
    return aus


def kategorie_bericht(hybrid_map, universum=None):
    """Ehrlicher Deckungs-Bericht: je Kategorie #Symbole; wenn `universum` ({symbol_id->{delisted}}) gegeben,
    auch der delistete Anteil (die Survivorship-Metrik). -> dict."""
    je_kat, delistet_je_kat = {}, {}
    for sid, kat in hybrid_map.items():
        je_kat[kat] = je_kat.get(kat, 0) + 1
        if universum and universum.get(sid, {}).get("delisted"):
            delistet_je_kat[kat] = delistet_je_kat.get(kat, 0) + 1
    return {
        "n_symbole": len(hybrid_map),
        "je_kategorie": dict(sorted(je_kat.items())),
        "delistet_je_kategorie": dict(sorted(delistet_je_kat.items())),
        "crisp_kategorien": sorted(_CRISP_KATEGORIEN),          # GicSubIndustry eindeutig
        "feine_kategorien": sorted(_FEIN_KATEGORIEN),           # GIC-Kandidat + Keyword-Guard
    }


# ------------------------------------------------------------------ #
# Live-Batch (EODHD) — gated, gecacht, gedeckelt
# ------------------------------------------------------------------ #
def _lade_cache(pfad=_CACHE_PFAD):
    if os.path.exists(pfad):
        try:
            with open(pfad, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _schreibe_cache(cache, pfad=_CACHE_PFAD):
    try:
        with open(pfad, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


class KlassifikationFehler(RuntimeError):
    """fetch_klassifikation: harter Abruf-Fehler (Netz/Auth) — fail-loud, NICHT als leer cachen (QS-B4)."""


class TageslimitErreicht(RuntimeError):
    """EODHD-Tageslimit erschöpft — KEIN transienter Fehler (kein Backoff/Retry im Tag). Der Voll-Batch
    BRICHT sauber ab (Fortschritt gesichert), statt durch alle Rest-Symbole zu spinnen (je ~14 s Backoff
    = Stunden für nichts). Der stündliche Heartbeat nimmt den Lauf nach dem Quota-Reset wieder auf."""


def _feld(full, *pfad, flach=None):
    """Verschachtelungs-TOLERANT ein Feld lesen: erst den flachen `A::B`-Schlüssel (gefilterte EODHD-Antwort),
    dann den verschachtelten Pfad (Voll-Dump `full[A][B]`). Gibt None zurück, wenn nichts passt. Damit
    funktioniert die Extraktion sowohl auf dem alten gefilterten als auch auf dem neuen Voll-Dump-Response."""
    if flach and isinstance(full, dict) and flach in full:
        return full[flach]
    cur = full
    for k in pfad:
        cur = cur.get(k) if isinstance(cur, dict) else None
    return cur


def _fetch_full_fundamentals(symbol_id, api_token=None, timeout=20, versuche=4):
    """LIVE: der VOLLE (ungefilterte) EODHD-Fundamentals-Dump eines Symbols — Jens (26.07.): ein Abruf je
    Symbol (10 Einheiten), gespeichert für ALLE Konsumenten (Klassifikation + Modul 9), statt je Feld erneut.
    **Fail-loud (QS-B4):** rc≠0 / Forbidden / Unauthorized → KlassifikationFehler (nie Auth-/Netzausfall als
    leer cachen). **Backoff (QS-B8):** transiente Fehler (rc≠0 / Rate-Limit) 2/4/8 s bis `versuche`.
    **Tageslimit (nicht-transient):** „daily API requests limit" → SOFORT `TageslimitErreicht` (kein Retry).
    **Keine-Fundamentaldaten (Quota-Fix):** leerer Container ({}/[]/null/"") ODER eindeutiges Not-Found
    ({"code":404}/„not found") NACH den Fehler-Checks = Symbol ohne Fundamentaldatensatz → `{}` (No-Data-
    Marker, KEIN Fehler); der Aufrufer/Cache hakt es ab und ruft es NIE WIEDER ab (deckt den delisteten
    Schwanz). Ein generisches/transientes Fehler-Objekt bleibt fail-loud (Retry, nie als leer cachen)."""
    import time
    from eodhd_prices import _token
    url = f"https://eodhd.com/api/fundamentals/{symbol_id}?api_token={_token(api_token)}&fmt=json"
    body = ""
    for v in range(versuche):
        out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                             capture_output=True, text=True, timeout=timeout + 10)
        body = out.stdout.strip()
        if "daily api requests limit" in body.lower():   # Tageslimit: nicht-transient → sofort abbrechen
            raise TageslimitErreicht(f"EODHD-Tageslimit erschöpft: {body[:80]}")
        rate = body.startswith("Too Many Requests") or '"429"' in body or "rate limit" in body.lower()
        if out.returncode == 0 and not rate:
            break
        if v < versuche - 1:
            time.sleep(2 ** (v + 1))                 # 2, 4, 8 s
    else:
        raise KlassifikationFehler(f"EODHD-Fundamentals nach {versuche} Versuchen nicht erreichbar: {body[:80]}")
    if body.startswith("Forbidden") or body in ("Unauthorized", "NA"):
        raise KlassifikationFehler(f"EODHD-Zugriff verweigert (Key?): {body[:80]}")
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        raise KlassifikationFehler(f"EODHD: keine JSON-Antwort: {body[:80]}")
    # Genuine „keine Fundamentaldaten" (häufig delistet): ein LEERER Container nach den Fehler-Checks → {}-Marker.
    if d is None or d == {} or d == [] or d == "":
        return {}
    # Voll-Dump hat den verschachtelten `General`-Block; die alte gefilterte Antwort den flachen Schlüssel.
    _hat_general = isinstance(d, dict) and (
        isinstance(d.get("General"), dict) or ({"General::GicSubIndustry", "General::Description"} & set(d)))
    # QS-Gemini-B2: eindeutiges Not-Found-Objekt ({"code":404}/„not found") ohne General → ebenfalls No-Data.
    if isinstance(d, dict) and not _hat_general:
        _code = str(d.get("code", "")).strip()
        _msg = str(d.get("message", "") or d.get("error", "")).lower()
        if _code == "404" or "not found" in _msg or "no data" in _msg:
            return {}
    # QS-Claude-B4 (erhalten): NICHT-leeres Objekt ohne General und ohne Not-Found-Marker (generisch/transient)
    # → fail-loud (Retry, nie als leer cachen).
    if not _hat_general:
        raise KlassifikationFehler(f"EODHD: unerwartete Antwort-Form (kein General-Block): {body[:80]}")
    return d


def klassifikation_aus_fundamentals(full):
    """Reiner, verschachtelungs-toleranter Extraktor: Voll-Dump (oder alte gefilterte Antwort) → {gic, desc,
    mcap}. Leeres {} (No-Data) → leere Felder. Offline testbar, keine I/O."""
    if not isinstance(full, dict) or not full:
        return {"gic": "", "desc": "", "mcap": 0.0}
    gic = _feld(full, "General", "GicSubIndustry", flach="General::GicSubIndustry")
    desc = _feld(full, "General", "Description", flach="General::Description")
    mcap_roh = _feld(full, "Highlights", "MarketCapitalization", flach="Highlights::MarketCapitalization")
    try:
        mcap = float(mcap_roh or 0) or 0.0
    except (TypeError, ValueError):
        mcap = 0.0
    return {"gic": (gic or "").strip(), "desc": desc or "", "mcap": mcap}


def fetch_klassifikation(symbol_id, api_token=None, timeout=20, versuche=4):
    """`{gic, desc, mcap}` eines Symbols. **Cache-first (Jens 26.07.):** liest den geteilten Voll-Dump-Cache
    (`fundamentals_cache`); bei Miss wird der VOLLE Fundamentals-Dump EINMAL gezogen, gespeichert (auch für
    Modul 9) und die Klassifikation daraus extrahiert. Ein bereits von Modul 9 gezogenes Symbol kostet hier
    KEINEN zweiten API-Call. No-Data ({}) → leere Felder (Aufrufer markiert `checked`, nie wieder abrufen)."""
    import fundamentals_cache
    full = fundamentals_cache.hole(
        symbol_id, lambda s: _fetch_full_fundamentals(s, api_token=api_token, timeout=timeout, versuche=versuche))
    return klassifikation_aus_fundamentals(full)


def klassifikation_map_live(symbol_ids, api_token=None, cache_pfad=_CACHE_PFAD):
    """{symbol_id -> {gic, desc}} live, mit On-Disk-Cache (reproduzierbar/quotaschonend). Bereits gecachte
    Symbole werden NICHT erneut gezogen. **QS-B4:** nur genuin geladene Felder werden gecacht; ein harter
    Abruf-Fehler (KlassifikationFehler) wird NICHT als leer persistiert → beim nächsten Lauf erneut versucht."""
    cache = _lade_cache(cache_pfad)
    neu = 0
    for sid in symbol_ids:
        if sid in cache:
            continue
        try:
            cache[sid] = fetch_klassifikation(sid, api_token=api_token)
        except KlassifikationFehler:
            continue                                # Fehler NICHT cachen (retry beim nächsten Lauf)
        neu += 1
        if neu % 25 == 0:
            _schreibe_cache(cache, cache_pfad)      # Zwischenstand sichern (lange Batches)
    _schreibe_cache(cache, cache_pfad)
    return {sid: cache[sid] for sid in symbol_ids if sid in cache}


# Echte US-Listing-Börsen (der Rest = OTC/PINK/GREY-Schrott ohne saubere Klassifikation).
_US_ECHTE_BOERSEN = {"NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS", "NYSE American"}


def _spreize(lst, n):
    """Deterministische, über die (sortierte) Liste GESPREIZTE Stichprobe von n Elementen — vermeidet den
    Alphabet-Anfangs-Klumpen (lauter 'AAA…'-OTC-Shells) der Naiv-Ersten-n. Kein Random."""
    if n <= 0 or not lst:
        return []
    if n >= len(lst):
        return list(lst)
    schritt = len(lst) / n
    return [lst[int(i * schritt)] for i in range(n)]


def universum_kandidaten(n_aktiv=150, n_delistet=150, exchange="US", api_token=None, typ="common_stock"):
    """Survivorship-freies, STRATIFIZIERT gedeckeltes Universum (n_aktiv aktive + n_delistet delistete
    Common-Stock-Symbole), GESPREIZT über die sortierte Liste (reproduzierbar, kein Random), auf echte
    Listing-Börsen gefiltert und mit '.US'-Suffix normalisiert (behebt den EODHD-Quirk: exchange-symbol-list
    gibt die Betriebsbörse, die Daten-Endpunkte wollen '{Code}.US'; OTC/PINK/GREY-Schrott ohne GIC fliegt raus).
    Stratifiziert (getrennte aktiv/delistet-Ziehung) → unverzerrtes Mischungsverhältnis.
    -> (uni, n_aktiv_verfuegbar, n_delistet_verfuegbar)."""
    from eodhd_prices import fetch_symbol_list
    aktiv = fetch_symbol_list(exchange, delisted=False, typ=typ, api_token=api_token)
    tot = fetch_symbol_list(exchange, delisted=True, typ=typ, api_token=api_token)

    def _aufbereiten(rows, is_delisted):
        aus = {}
        for r in rows:
            code, ex = r.get("Code"), r.get("Exchange")
            if not code or (exchange == "US" and ex not in _US_ECHTE_BOERSEN):
                continue
            sid = f"{code}.US" if exchange == "US" else f"{code}.{ex}"
            aus.setdefault(sid, {"symbol_id": sid, "code": code, "exchange": ex,
                                 "name": r.get("Name"), "delisted": is_delisted})
        return sorted(aus.values(), key=lambda x: x["symbol_id"])

    aktive_alle = _aufbereiten(aktiv, False)
    aktive_ids = {u["symbol_id"] for u in aktive_alle}
    delistete_alle = [u for u in _aufbereiten(tot, True) if u["symbol_id"] not in aktive_ids]
    uni = _spreize(aktive_alle, n_aktiv) + _spreize(delistete_alle, n_delistet)
    return uni, len(aktive_alle), len(delistete_alle)


# ------------------------------------------------------------------ #
# Persistenter, wiederaufnehmbarer Ergebnis-Store (Jens: Cache-Persistenz VOR dem Voll-Batch)
# ------------------------------------------------------------------ #
# COMMITTBAR (nicht gitignored): speichert NUR unser abgeleitetes Ergebnis + die geprüften Ticker —
# NICHT die rohen EODHD-Descriptions (deren proprietärer Inhalt bleibt im ephemeren .cache).
#   map:      {symbol_id -> kat_id}   (unsere Kategorie-Definition = unser IP)
#   delisted: {symbol_id -> bool}     (nur Treffer; für die B1-Naht)
#   checked:  [symbol_id, …]          (nur Ticker; für Resumability über Container-Restarts)
_PERSISTENT_PFAD = os.path.join(_HERE, "retro_kat_map_persistent.json")


def lade_persistent(pfad=_PERSISTENT_PFAD):
    """Lädt den committbaren Ergebnis-Store (oder leer). -> {map, delisted, checked(set)}."""
    if os.path.exists(pfad):
        try:
            with open(pfad, encoding="utf-8") as f:
                d = json.load(f)
            return {"map": d.get("map", {}), "delisted": d.get("delisted", {}),
                    "checked": set(d.get("checked", []))}
        except (OSError, json.JSONDecodeError):
            pass
    return {"map": {}, "delisted": {}, "checked": set()}


def _speichere_persistent(store, pfad=_PERSISTENT_PFAD):
    try:
        with open(pfad, "w", encoding="utf-8") as f:
            json.dump({"map": store["map"], "delisted": store["delisted"],
                       "checked": sorted(store["checked"])}, f, ensure_ascii=False, indent=0)
    except OSError:
        pass


def _merge_ergebnis(store, sid, kat, delisted):
    """REINER KERN (offline testbar): ein Klassifikationsergebnis in den Store aufnehmen. Immer als geprüft
    markieren (Resumability); nur Treffer (kat != None) in map+delisted (skip _REST_)."""
    store["checked"].add(sid)
    if kat:
        store["map"][sid] = kat
        store["delisted"][sid] = bool(delisted)
    return store


_REPO_ROOT = os.path.dirname(_SYS)     # …/System/integration -> _SYS=…/System -> Repo


def _git_persist(pfad=_PERSISTENT_PFAD, n=0, repo=_REPO_ROOT):
    """Committet+pusht NUR die (kleine) Klassifikations-Map (Reclaim-Festigkeit). Der Fundamentals-Voll-Dump-
    Cache liegt NICHT im Code-Repo (Jens 26.07.: separate Ablage, gitignored) — seine Reclaim-Festigkeit
    trägt das separate Backend (git-Daten-Repo / GDrive-Archiv). Fehler-tolerant; No-op ohne Änderung."""
    msg = (f"Voll-Batch: Klassifikations-Fortschritt ({n} geprueft) [auto]\n\n"
           "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>\n"
           "Claude-Session: https://claude.ai/code/session_01VV2NXeGeTGo9VouDSbDoXU")
    try:
        subprocess.run(["git", "-C", repo, "add", pfad], capture_output=True, timeout=30)
        r = subprocess.run(["git", "-C", repo, "commit", "-m", msg], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:                           # nur pushen, wenn wirklich committed
            subprocess.run(["git", "-C", repo, "push"], capture_output=True, timeout=120)
    except Exception:                                   # noqa: BLE001 — git-Fehler darf den Batch nie kippen
        pass


# US + EU-Börsen (Jens: ganzes Universum US+EU). Per-Symbol über Tage (Watchdog grindet je Tag ein
# Quota-Kontingent). _bounded_universum liefert je Börse den korrekten Daten-Suffix.
_EU_EXCHANGES = ["LSE", "XETRA", "PA", "MI", "AS", "SW", "MC", "BR", "ST", "HE", "CO", "OL", "VI", "LS", "IR"]
# Large-Cap-Asien (Jens): Japan/Korea/HK/Taiwan/China/Indien/Singapur. Trafo/Netz/Halbleiter-Kern in Asien
# (Hitachi.TSE, Hyundai Electric.KO, TSMC.TW, asiatische Halbleiter/Kupfer). Large-Cap via mcap-Floor (unten).
_ASIA_EXCHANGES = ["TSE", "KO", "KQ", "HK", "TW", "SS", "SZ", "NSE", "SG"]
_US_EU = ["US"] + _EU_EXCHANGES
_GLOBAL = _US_EU + _ASIA_EXCHANGES
_LARGE_CAP_FLOOR = 2e9              # $2 Mrd — „large cap"-Untergrenze (mcap < Floor → nicht in die Map)


def klassifiziere_universum(n_aktiv=None, n_delistet=None, exchange="US", api_token=None,
                            speicher_alle=200, commit_alle=0, gic_cache=_CACHE_PFAD, pfad=_PERSISTENT_PFAD,
                            exchanges=None, min_mcap=0.0, gic_direkt=False, nach_commit=None):
    """WIEDERAUFNEHMBARER Voll-Batch: klassifiziert das (stratifizierte) survivorship-freie Universum,
    überspringt bereits geprüfte Symbole (persistenter `checked`-Set), schreibt inkrementell alle
    `speicher_alle` Treffer → übersteht Container-Restarts (Jens: Cache-Persistenz). `n_aktiv`/`n_delistet`
    None => VOLL (kein Cap). `exchanges` (Liste) => über mehrere Börsen (US+EU); None => nur `exchange`.
    -> (hybrid_map, delisted_map, bericht). Gated Live-Runner."""
    store = lade_persistent(pfad)
    uni = []
    for code in (exchanges or [exchange]):
        try:
            u, _a, _d = universum_kandidaten(n_aktiv or 10 ** 9, n_delistet or 10 ** 9, code, api_token)
            uni.extend(u)
        except Exception:                           # noqa: BLE001 — eine Börse darf den Lauf nicht kippen
            continue
    delflag = {r["symbol_id"]: bool(r.get("delisted")) for r in uni if r.get("symbol_id")}
    offen = [sid for sid in delflag if sid not in store["checked"]]
    seit_speicher = 0
    tageslimit = False
    for sid in offen:
        try:
            felder = fetch_klassifikation(sid, api_token=api_token)
        except TageslimitErreicht:
            tageslimit = True
            break                                       # Quota erschöpft → sauber abbrechen (Heartbeat nimmt wieder auf)
        except KlassifikationFehler:
            continue                                    # transient/Auth → nicht als geprüft markieren (retry)
        # Large-Cap-Floor (Jens): unter der Marktkap-Schwelle NICHT mappen (aber als geprüft markieren →
        # kein Re-Fetch). mcap==0 (fehlend) wird NICHT gefiltert (Floor greift nur bei bekanntem mcap).
        kat = _klassifiziere_symbol(felder, gic_direkt=gic_direkt)   # gic_direkt: viele Kategorien (Power)
        if kat and min_mcap and 0 < felder.get("mcap", 0) < min_mcap:
            kat = None                                  # zu klein → aus der Map (Micro-Cap-Rauschen raus)
        _merge_ergebnis(store, sid, kat, delflag.get(sid, False))
        seit_speicher += 1
        if seit_speicher >= speicher_alle:
            _speichere_persistent(store, pfad)
            if commit_alle and len(store["checked"]) % commit_alle < speicher_alle:
                _git_persist(pfad, len(store["checked"]))   # Reclaim-fest: Fortschritt nach git (Option a)
                if nach_commit:
                    nach_commit()                           # z. B. Fundamentals-Cache nach Drive syncen
            seit_speicher = 0
    _speichere_persistent(store, pfad)
    if commit_alle:
        _git_persist(pfad, len(store["checked"]))           # Schluss-Commit
        if nach_commit:
            nach_commit()                                   # Schluss-Sync (auch nach Tageslimit-Abbruch)
    hybrid = kombiniere(dict(store["map"]))             # Gold ergänzt
    delisted_map = dict(store["delisted"])
    bericht = kategorie_bericht(hybrid, {s: {"delisted": d} for s, d in delisted_map.items()})
    bericht["n_geprueft"] = len(store["checked"])
    bericht["n_universum"] = len(delflag)
    bericht["n_offen"] = len(delflag) - len(store["checked"] & set(delflag))
    bericht["tageslimit_erreicht"] = tageslimit          # True → Quota erschöpft, Rest nach Reset
    return hybrid, delisted_map, bericht


def _gic_coverage(uni, klass):
    """QS-Gemini-B3 (Metadaten-Survivorship): GIC-Klassifikations-Coverage GETRENNT für aktiv vs. delistet.
    Fehlt bei delisteten (obskuren) Namen häufiger GIC, wäre ihr leises Wegfallen ein selektiver Filter."""
    z = {"aktiv": {"mit": 0, "ohne": 0}, "delistet": {"mit": 0, "ohne": 0}}
    for r in uni:
        sid = r.get("symbol_id")
        bucket = "delistet" if r.get("delisted") else "aktiv"
        z[bucket]["mit" if klass.get(sid, {}).get("gic") else "ohne"] += 1
    for b in z.values():
        n = b["mit"] + b["ohne"]
        b["coverage"] = round(b["mit"] / n, 3) if n else 0.0
    return z


def baue_map_live(n_aktiv=150, n_delistet=150, exchange="US", api_token=None, cache_pfad=_CACHE_PFAD):
    """VOLL: stratifiziertes survivorship-freies Universum -> GIC-Klassifikation (gecacht) -> breite Map ->
    HYBRID mit Gold. -> (hybrid_map, bericht). `n_aktiv`/`n_delistet` steuern den Umfang je Lauf
    (Zielfunktion: Maschinenzeit gegen Abdeckung; getrennt, damit delistete garantiert vertreten sind)."""
    uni, n_akt_verf, n_del_verf = universum_kandidaten(n_aktiv, n_delistet, exchange, api_token)
    sids = [r["symbol_id"] for r in uni if r.get("symbol_id")]
    klass = klassifikation_map_live(sids, api_token=api_token, cache_pfad=cache_pfad)
    breit = baue_breite_map(klass)
    hybrid = kombiniere(breit)
    universum_flags = {r["symbol_id"]: {"delisted": r.get("delisted", False)} for r in uni}
    bericht = kategorie_bericht(hybrid, universum_flags)
    bericht["n_universum"] = len(sids)
    bericht["n_klassifiziert"] = sum(1 for v in klass.values() if v.get("gic"))
    bericht["gic_coverage"] = _gic_coverage(uni, klass)     # QS-Gemini-B3: aktiv vs. delistet
    # QS-Claude-B3: die FEIN-Kategorien verlangen zusätzlich eine Description-Keyword-Treffer → ein
    # delisteter Name mit gültigem GIC aber LEERER Description fällt still aus der Fein-Klassifikation.
    # Description-Coverage GETRENNT (aktiv/delistet) NUR über die Fein-Kandidaten-GICs sichtbar machen.
    fein_kand = _ELEKTRO_GIC | _ENGINEERING_GIC
    dc = {"aktiv": {"mit": 0, "ohne": 0}, "delistet": {"mit": 0, "ohne": 0}}
    for r in uni:
        f = klass.get(r["symbol_id"], {})
        if _norm(f.get("gic")) in fein_kand:
            b = dc["delistet" if r.get("delisted") else "aktiv"]
            b["mit" if (f.get("desc") or "").strip() else "ohne"] += 1
    for b in dc.values():
        n = b["mit"] + b["ohne"]
        b["coverage"] = round(b["mit"] / n, 3) if n else None
    bericht["description_coverage_fein"] = dc                # survivorship-frei NUR bis zu dieser Coverage
    bericht["universum_verfuegbar"] = {"aktiv": n_akt_verf, "delistet": n_del_verf}
    # delisted_map für die B1-Naht (lauf_anker(delisted_map=…)): echte Flags je Map-Symbol; Gold-ergänzte
    # Symbole (nicht im Universum) = aktiv (False).
    delisted_map = {sid: bool(universum_flags.get(sid, {}).get("delisted", False)) for sid in hybrid}
    return hybrid, delisted_map, bericht


_GIC_BREIT_PFAD = os.path.join(_HERE, "retro_kat_map_gic_breit.json")   # US (kein Floor)
_GIC_EU_PFAD = os.path.join(_HERE, "retro_kat_map_gic_eu.json")         # EU (kein Floor)
_GIC_ASIA_PFAD = os.path.join(_HERE, "retro_kat_map_gic_asia.json")     # Asien (Large-Cap-Floor)

# Regions-Ziele für den GIC-Direkt-Batch: (börsen, min_mcap, persistente-datei). Getrennte Dateien, damit
# der US-Ablations-Lauf (USD) nicht still mit EU/Asien vermischt wird — Vereinigung/regionale Läufe bleiben
# eine BEWUSSTE Entscheidung. US+EU: kein Floor (Makro+Small-Cap, repräsentativer Anker). Asien: nur
# Large-Cap (Jens: „Asien Large Cap Universum" — der Trafo/Netz/Halbleiter-Kern; mcap-Floor $2 Mrd).
_GIC_REGIONEN = {
    "us":   (["US"], 0.0, _GIC_BREIT_PFAD),
    "eu":   (_EU_EXCHANGES, 0.0, _GIC_EU_PFAD),
    "asia": (_ASIA_EXCHANGES, _LARGE_CAP_FLOOR, _GIC_ASIA_PFAD),
}
_REGION_REIHENFOLGE = ["us", "eu", "asia"]                              # --region=all: US zuerst (fast halb fertig)


def _drive_setup():
    """Optionaler Sync des Fundamentals-Caches in die führende Google-Drive-DB. **Preflight fail-loud**
    (Jens: Orchestrierung muss überwachen, ob's läuft): ohne OAuth-Env-Creds inaktiv (kein Lärm); mit Creds,
    aber kaputtem Drive → LAUTE Warnung + Weiterlauf (der Grind darf nicht abbrechen, die Klassifikations-Map
    ist ohnehin git-persistent — nur der Voll-Dump-Cache wäre dann nicht in Drive gesichert). Bei OK: einmal
    Restore aus der Drive-DB + Rückgabe eines Sync-Callbacks (frisches Token je Sync)."""
    try:
        import gdrive
        import fundamentals_drive
    except Exception:                                    # noqa: BLE001
        return None
    st = gdrive.preflight()
    if not st.get("ok"):
        fehler = st.get("fehler") or ""
        if "Fehlende OAuth-Credentials" not in fehler:   # Creds da, aber Drive kaputt → laut
            print(f"  ⚠ Drive-Sync AUS (Preflight bei '{st.get('schritt')}'): {fehler}")
        return None
    print(f"  💾 Drive-DB verbunden ({st['konto']}, {st.get('quota')}).")
    try:
        n = fundamentals_drive.sync_restore(gdrive.access_token())
        print(f"  💾 Restore: {n} Buckets aus der Drive-DB in den lokalen Cache geholt.")
    except Exception as e:                               # noqa: BLE001 — Restore-Fehler kippt den Grind nie
        print(f"  ⚠ Drive-Restore fehlgeschlagen (Grind läuft weiter): {e}")

    def _sync():
        try:
            m = fundamentals_drive.sync_hoch(gdrive.access_token())
            if m:
                print(f"  💾 Drive-Sync: {m} Bucket(s) in die DB aktualisiert.")
        except Exception as e:                           # noqa: BLE001 — Sync-Fehler kippt den Grind nie
            print(f"  ⚠ Drive-Sync übersprungen (Grind läuft weiter): {e}")

    return _sync


def main():
    print("=== Breite survivorship-freie Symbol→Kategorie-Map (industrie-getrieben) ===")
    # VOLL-Batch (Schritt 4): wiederaufnehmbar + reclaim-fest (periodisch git commit+push).
    if "--voll" in sys.argv and os.environ.get("EODHD_API_KEY"):
        commit_alle = 1000
        speicher = 50                                    # häufig auf Disk (kleines Verlustfenster bei Reclaim)
        min_mcap_override = None
        for a in sys.argv:
            if a.startswith("--commit="):
                commit_alle = int(a.split("=", 1)[1])
            elif a.startswith("--speicher="):
                speicher = int(a.split("=", 1)[1])
            elif a.startswith("--min-mcap="):
                min_mcap_override = float(a.split("=", 1)[1])
        gic_direkt = "--gic-direkt" in sys.argv
        # --gic-direkt: die MEHR-KATEGORIEN-POWER-Variante (Jens: survivorship-freie Kategorien-Breite ist der
        # bindende Deckel gegen F86). Jedes Symbol -> seine GicSubIndustry (VIELE Kategorien). --region wählt das
        # Ziel (us|eu|asia|all); getrennte Dateien + je eigener Floor. Ohne --gic-direkt: alter thematischer Pfad.
        region_arg = "us"
        for a in sys.argv:
            if a.startswith("--region="):
                region_arg = a.split("=", 1)[1].strip().lower()
        # Rückwärtskompatibel: --eu / --global (thematischer Pfad nutzt weiter die globale Börsenmenge).
        if not gic_direkt:
            exchanges = _GLOBAL if "--global" in sys.argv else (_US_EU if "--eu" in sys.argv else ["US"])
            min_mcap = _LARGE_CAP_FLOOR if min_mcap_override is None else min_mcap_override
            ziele = [(exchanges, min_mcap, _PERSISTENT_PFAD, "thematisch")]
        else:
            regionen = _REGION_REIHENFOLGE if region_arg == "all" else [region_arg]
            unbekannt = [r for r in regionen if r not in _GIC_REGIONEN]
            if unbekannt:
                print(f"  Unbekannte Region(en): {unbekannt}. Erlaubt: us|eu|asia|all.")
                return None
            ziele = []
            for r in regionen:
                ex, floor, pf = _GIC_REGIONEN[r]
                ziele.append((ex, floor if min_mcap_override is None else min_mcap_override, pf, r))
        # Fundamentals-Cache ↔ führende Google-Drive-DB: Restore vor dem Lauf + Sync-Callback in der
        # Commit-Kadenz (Preflight fail-loud; ohne Env-Creds sauber inaktiv). --no-drive schaltet ab.
        drive_sync = None if "--no-drive" in sys.argv else _drive_setup()
        letztes = None
        for exchanges, min_mcap, pfad, label in ziele:
            floor_txt = f"≥${min_mcap:.0e}" if min_mcap else "KEIN Floor (Makro+Small-Cap)"
            modus = f"GIC-DIREKT [{label}]" if gic_direkt else "kuratiert-thematisch"
            print(f"  VOLL-Batch {modus} über {len(exchanges)} Börse(n), {floor_txt}, wiederaufnehmbar "
                  f"({pfad.split('/')[-1]}). Per-Symbol über Tage (Disk je {speicher}, git-persistent je "
                  f"{commit_alle} → reclaim-fest).")
            hybrid, dmap, b = klassifiziere_universum(commit_alle=commit_alle, exchanges=exchanges,
                                                      min_mcap=min_mcap, gic_direkt=gic_direkt, pfad=pfad,
                                                      speicher_alle=speicher, nach_commit=drive_sync)
            print(f"  [{label}] geprüft: {b['n_geprueft']} | offen: {b['n_offen']} | Map-Symbole: {b['n_symbole']}")
            if b.get("tageslimit_erreicht"):
                print("  ⏸  EODHD-TAGESLIMIT erreicht → sauber abgebrochen. Rest nach Quota-Reset (Heartbeat).")
                letztes = hybrid
                break                                    # Quota erschöpft → weitere Regionen erst nach Reset
            if b["n_offen"] == 0:
                print(f"  ✅ [{label}] Universum vollständig klassifiziert (n_offen=0).")
            print(f"  [{label}] je Kategorie:   {b['je_kategorie']}")
            print(f"  [{label}] davon delistet: {b['delistet_je_kategorie']}  (>0 = survivorship-frei belegt)")
            letztes = hybrid
        # QS-Gemini-B1 Größen-Checkpoint: der Voll-Dump-Cache liegt git-committet im Repo (bei jedem Reclaim
        # neu geklont). Größe je Chunk melden, damit das Repo-Gewicht nicht blind wächst. Faustregel: nähert
        # sich der Cache ~1 GB, ist der externe Speicher zu erwägen (Jens-Entscheidung).
        try:
            import fundamentals_cache
            n_c, bytes_c = fundamentals_cache.bestand()
            print(f"  📦 Fundamentals-Cache: {n_c} Einträge, {bytes_c / 1e6:.1f} MB (lokal, gitignored — auf Google Drive gesichert).")
            if bytes_c > 1e9:
                print("  ⚠  Cache > 1 GB — Repo-Gewicht/Reclaim-Klon prüfen (ggf. externen Speicher mit Jens klären).")
        except Exception:                                # noqa: BLE001 — Reporting darf den Batch nie kippen
            pass
        return letztes
    if "--smoke" not in sys.argv or not os.environ.get("EODHD_API_KEY"):
        grund = "kein --smoke/--voll" if "--smoke" not in sys.argv else "kein $EODHD_API_KEY"
        print(f"  Kein Live-Batch ({grund}). Reiner Kern ist offline getestet (test_retro_kat_map_breit.py).")
        return None
    n = 120
    for a in sys.argv:
        if a.startswith("--n="):
            n = int(a.split("=", 1)[1])
    print(f"  Live-Batch: US Common Stock, stratifiziert {n} aktiv + {n} delistet, GIC-Klassifikation gecacht.")
    hybrid, delisted_map, b = baue_map_live(n_aktiv=n, n_delistet=n, exchange="US")
    print(f"  Universum: {b['n_universum']} (verfügbar {b['universum_verfuegbar']}) | "
          f"klassifiziert: {b['n_klassifiziert']} | Map-Symbole: {b['n_symbole']}")
    print(f"  je Kategorie:        {b['je_kategorie']}")
    print(f"  davon delistet:      {b['delistet_je_kategorie']}  (>0 = survivorship-frei belegt)")
    print(f"  GIC-Coverage:        {b['gic_coverage']}  (aktiv vs. delistet — Metadaten-Survivorship, QS-B3)")
    print(f"  crisp (GIC):         {b['crisp_kategorien']}")
    print(f"  fein (GIC+Keyword):  {b['feine_kategorien']}")
    return hybrid


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nHINWEIS: {type(e).__name__}: {e}")
