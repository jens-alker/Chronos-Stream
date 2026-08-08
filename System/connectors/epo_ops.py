"""
epo_ops.py — Konnektor: EPO OPS (Open Patent Services 3.2) -> Frühsignal-Zählung + Modul-2-Input (facts).

Patente sind die **zweite Sprosse** der Reifegradleiter (Modul 5, `SPROSSE_ORDNUNG` =
paper<patent<funding<capex<kapazitaet<news) — FRÜH auf der Zeitachse, alpha-tragend, hart datiert.
Diese Quelle war der bisher fehlende Baustein (arXiv egress-blockiert); EPO ist jetzt anbindbar
(Consumer-Key/Secret in der Cloud-Umgebung). Zwei Nutzungen aus DERSELBEN Quelle:

1. **Frühsignal-Zählung (Anker, KEIN LLM, keine Meinung):** IPC-Klasse × pd-Fenster ->
   `@total-result-count`. Reines Zählen hart datierter, klassifizierter Anmeldungen je Klasse je
   Zeitfenster -> Beschleunigung (`Kontext/Anker-Design_Cashflow-Decomposition-vs-Fruehsignal.md`,
   die FRÜHE Sprosse gegen die DCF-Decomposition, Hälfte 2). Das umgeht die Vintage-Wand (die betraf nur die
   *semantische* Extraktion, nicht das Zählen datierter Anmeldungen).
2. **Semantische Pipeline (Verortung Modul 1/2/5, KEINE Insel):** biblio -> `facts` (Titel als Text,
   quellentyp=`patent`) -> Modul 2 kategorisiert wie den arXiv-Paper-Pfad. `_QUELLENTYP["epo"]`
   führt schon nach `patent`; hier wird der Roh-Datensatz geliefert.

**PIT-Ehrlichkeit (§3.6 bitemporal) — zwei getrennte Daten:**
- `t_event`     = Anmeldedatum (application-reference/date) — das echte Frühereignis (R&D-Commitment).
- `t_disclosed` = Publikationsdatum (publication-reference/date) — die **PIT-Wand**: ein Patent ist
  erst mit Publikation (~18 Mon. nach Anmeldung) öffentlich wissbar. Deshalb ist das **Zählfenster
  `pd`** (Publikation), NICHT `ap`: an einem Stichtag T darf nur zählen, was bis T publiziert war.
- `t_ingest`    = Abrufzeit (Wissenszeit).

Auth: OAuth2 client_credentials — Basic base64(consumer_key:secret) -> Bearer access_token (~20 Min).
Keys: arg -> $EPO_API_CONSUMER_KEY/$EPO_API_CONSUMER_SECRET_KEY (Cloud-Umgebung). Nur
Standardbibliothek. Live gegen ops.epo.org validiert (OAuth + published-data search + biblio, 24.07.).

Wie die übrigen Konnektoren: verifizierbare Transformationen (offline gegen das ECHTE OPS-Schema
getestet) getrennt vom `fetch_*`-Live-Skelett.
"""
import base64
import datetime
import json
import os
import subprocess
from urllib.parse import quote

_AUTH = "https://ops.epo.org/3.2/auth/accesstoken"
_SEARCH = "https://ops.epo.org/3.2/rest-services/published-data/search"
_BIBLIO = "https://ops.epo.org/3.2/rest-services/published-data/publication/docdb/{ref}/biblio"
_ABSTRACT = "https://ops.epo.org/3.2/rest-services/published-data/publication/docdb/{ref}/abstract"


# ------------------------------------------------------------------ #
# Auth (OAuth2 client_credentials)
# ------------------------------------------------------------------ #
def _creds(key=None, secret=None):
    key = key or os.environ.get("EPO_API_CONSUMER_KEY")
    secret = secret or os.environ.get("EPO_API_CONSUMER_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "Kein EPO-Zugang (arg / $EPO_API_CONSUMER_KEY + $EPO_API_CONSUMER_SECRET_KEY).")
    return key.strip(), secret.strip()


def _basic_header(key, secret):
    """Basic-Auth-Header-Wert für den Token-Abruf: base64(consumer_key:consumer_secret)."""
    roh = f"{key}:{secret}".encode("utf-8")
    return "Basic " + base64.b64encode(roh).decode("ascii")


def fetch_token(key=None, secret=None, timeout=30):
    """LIVE: OAuth2-Access-Token (client_credentials). -> Token-String (~20 Min gültig)."""
    key, secret = _creds(key, secret)
    out = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-X", "POST", _AUTH,
         "-H", f"Authorization: {_basic_header(key, secret)}",
         "-H", "Content-Type: application/x-www-form-urlencoded",
         "-d", "grant_type=client_credentials"],
        capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"EPO-Auth nicht erreichbar: rc={out.returncode} {out.stderr[:200]}")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"EPO-Auth: keine JSON-Antwort: {out.stdout[:200]}")
    tok = data.get("access_token")
    if not tok:
        raise RuntimeError(f"EPO-Auth ohne access_token: {out.stdout[:200]}")
    return tok


def _curl_json(url, token, timeout):
    """GET url mit Bearer-Token per curl -> geparstes JSON. Fail-loud (Fehler/Fault deutlich)."""
    out = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), url,
         "-H", f"Authorization: Bearer {token}", "-H", "Accept: application/json"],
        capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"EPO nicht erreichbar: rc={out.returncode} {out.stderr[:200]}")
    body = out.stdout.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"EPO: keine JSON-Antwort (Fault/HTML?): {body[:200]}")
    return data


# ------------------------------------------------------------------ #
# CQL-Aufbau (reiner Bug-Guard, unit-getestet)
# ------------------------------------------------------------------ #
def _iso_zu_ops(d):
    """ISO YYYY-MM-DD (Systemkonvention) -> OPS-Datumsformat YYYYMMDD. Toleriert schon-kompaktes."""
    return str(d).replace("-", "")


def _ops_zu_iso(d):
    """OPS YYYYMMDD -> ISO YYYY-MM-DD (Systemkonvention, wie arXiv/EODHD). '' bei Unpassendem."""
    s = "".join(ch for ch in str(d or "") if ch.isdigit())
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else ""


def such_cql(klasse, seit, bis=None, feld="ipc"):
    """Baut die OPS-CQL-Suchanfrage: `<feld>=<klasse> and pd within "<seit> <bis>"`.

    - `feld="ipc"` matcht den Klassen-TEILBAUM (z. B. ipc=H01L -> alle H01L…-Gruppen); `cpc=` matcht
      NUR das exakte Top-Symbol (live belegt: 70 219 vs. 3 Treffer für H01L/2018). Für die
      Sprossen-Zählung ist der Teilbaum die richtige Granularität -> Default `ipc`.
    - `pd` = Publikationsdatum (Offenlegung = PIT-Wand). `seit`/`bis` als ISO oder YYYYMMDD;
      ohne `bis` = einzelnes Jahr/Fenster `pd within "<seit>"`.
    """
    s = _iso_zu_ops(seit)
    fenster = f'{s} {_iso_zu_ops(bis)}' if bis else s
    return f'{feld}={klasse} and pd within "{fenster}"'


# ------------------------------------------------------------------ #
# Zählung (das Frühsignal-Primitiv — deterministisch, LLM-frei)
# ------------------------------------------------------------------ #
def zaehle(search_json):
    """OPS-Such-JSON -> `@total-result-count` als int. **Fail-closed** (QS Claude-B3/Gemini-B1):
    ein echtes „0 Patente" liefert `@total-result-count="0"` (parst sauber zu 0); FEHLT die Zähl-
    Struktur (Fault-/Fehler-Envelope, unerwartetes Schema), wird NICHT still 0 gemeldet, sondern
    laut geworfen — sonst würde ein transienter 403/429/Fault als fabrizierte Deceleration in genau
    das alpha-tragende Frühsignal fließen (die 404->stiller-Mock-Lehre, CLAUDE.md).

    DAS ist das Zähl-Primitiv der Frühsignal-Mechanik: die Gesamttreffer je Klasse×Fenster, unabhängig
    davon, wie viele Publikationsreferenzen `Range` zurückgibt."""
    d = search_json.get("ops:world-patent-data") if isinstance(search_json, dict) else None
    bs = d.get("ops:biblio-search") if isinstance(d, dict) else None
    if not isinstance(bs, dict) or "@total-result-count" not in bs:
        raise RuntimeError(f"EPO: keine Trefferzahl (Fault/unerwartetes Schema?): "
                           f"{str(search_json)[:200]}")
    try:
        return int(bs["@total-result-count"])
    except (TypeError, ValueError):
        raise RuntimeError(f"EPO: unlesbare Trefferzahl: {bs.get('@total-result-count')!r}")


_NULL_ZAEHLUNG = {"ops:world-patent-data": {"ops:biblio-search": {"@total-result-count": "0"}}}


def fetch_search(cql, token, start=1, ende=1, timeout=40):
    """LIVE: OPS published-data search. `cql` = CQL-Query (such_cql). `Range` klein halten
    (start..ende), wenn nur die Zählung gebraucht wird (Treffer stehen in @total-result-count).
    -> geparstes Such-JSON.

    **Null-Treffer = 0, kein Fehler:** OPS drückt „keine Treffer" als XML-Fault `SERVER.EntityNotFound`
    / „No results found" aus (nicht als JSON mit count 0). Das ist ein legitimes Ergebnis, KEIN
    transienter Fehler — häufig bei Volltext-/Keyword-Suchen in frühen Fenstern. Wird abgefangen und als
    Null-Zählung zurückgegeben, damit `zaehle`/`fetch_anzahl` sauber 0 liefern (statt den Lauf zu kippen)."""
    url = f"{_SEARCH}?q={quote(cql)}&Range={int(start)}-{int(ende)}"
    try:
        return _curl_json(url, token, timeout)
    except RuntimeError as e:
        m = str(e)
        if "EntityNotFound" in m or "No results found" in m:
            return _NULL_ZAEHLUNG
        raise


def fetch_anzahl(klasse, seit, bis=None, token=None, feld="ipc", timeout=40):
    """LIVE (bequem): Trefferzahl für EINE Klasse×Fenster. fetch_search + zaehle. Token nötig."""
    if token is None:
        token = fetch_token()
    return zaehle(fetch_search(such_cql(klasse, seit, bis, feld), token, timeout=timeout))


def ist_transient(msg):
    """Transienter EPO-Fehler (Backoff+Retry statt fatal): Drossel ODER Server-Zeitfehler. `RobotDetected`
    (OPS-Anti-Robot, ~15 Suchen/min) und `DomainAccess`/„try again later"/5xx traten in langen Sweeps auf
    und kippten sonst den ganzen Lauf. (Übernommen aus dem bewährten Retro-Zähler.)"""
    m = (msg or "").lower()
    return any(s in m for s in ("robotdetected", "429", "throttl", "domainaccess",
                                "try again later", "serverbusy", "service unavailable",
                                "timed out", "nicht erreichbar", "connection", "reset by peer"))


def gedrosselter_zaehler(api_token=None, cache_pfad=None, feld="ipc",
                         throttle_s=4.5, token_ttl_s=1000, flush_alle=25):
    """Bewährte robuste Batch-Zählung (aus den langen Sweeps übernommen): ein `count_fn(klasse, seit, bis)
    -> int`, das OPS' Search-Limit respektiert und lange Läufe überlebt.
      - **Throttle** (~4.5 s ≈ 13/min, nur Cache-Miss) unter OPS' `search=green:15`.
      - **Token-Refresh** vor Ablauf (~20 min gültig) — ein Mehr-Jahres-Sweep überdauert ein Token.
      - **Transient-Retry** (Backoff 30/60/120 s + Token-Refresh) via `ist_transient`.
      - **On-Disk-Cache** (optional `cache_pfad`) — historische Zählungen sind stabil → reproduzierbar/
        quotaschonend; periodischer Flush (`flush_alle`) macht Abbrüche resümierbar.
    Rückgabe hat `.flush()`. `feld`='ipc' (Teilbaum) | 'cpc'. Nur Std-Lib + curl."""
    import json
    import os
    import time
    cache = {}
    if cache_pfad and os.path.exists(cache_pfad):
        try:
            with open(cache_pfad, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}
    st = {"neu": 0, "tok": api_token or fetch_token(), "tz": time.time(), "fest": bool(api_token)}

    def _flush():
        if cache_pfad:
            try:
                with open(cache_pfad, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            except OSError:
                pass

    def _tok():
        if not st["fest"] and time.time() - st["tz"] > token_ttl_s:
            st["tok"] = fetch_token(); st["tz"] = time.time()
        return st["tok"]

    def count_fn(klasse, seit, bis):
        key = f"{feld}:{klasse}:{seit}:{bis}"
        if key not in cache:
            for v in range(4):
                try:
                    time.sleep(throttle_s)
                    cache[key] = fetch_anzahl(klasse, seit, bis, token=_tok(), feld=feld, timeout=40)
                    break
                except RuntimeError as e:
                    if ist_transient(str(e)) and v < 3:
                        time.sleep(30 * (2 ** v))
                        if not st["fest"]:
                            st["tok"] = fetch_token(); st["tz"] = time.time()
                        continue
                    raise
            else:
                raise RuntimeError(f"EPO dauerhaft gedrosselt bei {klasse} {seit}..{bis}")
            st["neu"] += 1
            if flush_alle and st["neu"] % flush_alle == 0:
                _flush()
        return cache[key]

    count_fn.flush = _flush
    return count_fn


def zaehlung_reihe(klassen, fenster, token=None, fetch=None, feld="ipc", stichtag=None):
    """Frühsignal-Zählreihe: je (Klasse × Zeitfenster) die Anzahl publizierter Anmeldungen.

    DAS ist der deterministische Kern der Frühsignal-Mechanik (Anker-Design §2): reines Zählen hart
    datierter, klassifizierter Anmeldungen — KEIN LLM, keine Meinung. Die Beschleunigung
    (Anzahl(t) vs. Anzahl(t−1)) ist das alpha-tragende Frühsignal.

    - `klassen`:  Liste IPC-Klassen (z. B. ["H01L", "H02M"]).
    - `fenster`:  Liste (seit, bis) ISO-Datumspaare (Publikationsdatum-Fenster).
    - `fetch`:    fetch(cql, token) -> Such-JSON. Default `fetch_search` (live); injizierbar -> offline
                  testbar ohne Netz/Key.
    - `stichtag`: optionaler PIT-Riegel (ISO). Fenster, deren `bis` NACH dem Stichtag liegt (oder ohne
                  `bis`), werden **ausgelassen** (fail-closed): an einem Stichtag T zählt nur, was bis T
                  publiziert = wissbar war. None = kein Riegel (Live-Betrieb).

    -> Liste {klasse, fenster_seit, fenster_bis, anzahl}, stabil sortiert (klasse, fenster_seit).
    """
    if fetch is None:
        if token is None:
            token = fetch_token()
        fetch = lambda cql, tok=token: fetch_search(cql, tok)  # noqa: E731
    out = []
    for klasse in klassen:
        for seit, bis in fenster:
            if stichtag is not None:
                # PIT-Riegel POSITIV fail-closed (QS Claude-B1/Gemini-B2): nur zählen, wenn das
                # Fenster-ENDE `bis` sauber als volles Datum parsebar UND <= Stichtag ist. Ein
                # jahr-großes ("2018"), leeres, fehlendes oder kaputtes `bis` ist NICHT als „vor dem
                # Stichtag offengelegt" verifizierbar -> verwerfen (nie durchrutschen lassen).
                bis_iso = _ops_zu_iso(bis)
                if not bis_iso or bis_iso > _ops_zu_iso(stichtag):
                    continue
            anzahl = zaehle(fetch(such_cql(klasse, seit, bis, feld)))
            out.append({"klasse": klasse, "fenster_seit": seit, "fenster_bis": bis,
                        "anzahl": int(anzahl)})
    return sorted(out, key=lambda r: (r["klasse"], str(r["fenster_seit"])))


# ------------------------------------------------------------------ #
# biblio -> Patent-Datensatz (verifizierbar, gegen das ECHTE OPS-Schema getestet)
# ------------------------------------------------------------------ #
def _exchange_docs(biblio_json):
    """Liste der exchange-document-Knoten (OPS liefert dict ODER Liste)."""
    try:
        ed = biblio_json["ops:world-patent-data"]["exchange-documents"]["exchange-document"]
    except (KeyError, TypeError):
        return []
    return ed if isinstance(ed, list) else [ed]


def _als_liste(v):
    return v if isinstance(v, list) else ([] if v is None else [v])


def _erstes_datum(referenz):
    """publication-/application-reference -> erstes gefundenes Datum (OPS YYYYMMDD). OPS legt das
    Datum je nach docdb/epodoc-Eintrag ab (im RU/EP-Beispiel im epodoc-Eintrag) -> alle scannen."""
    for did in _als_liste((referenz or {}).get("document-id")):
        d = (did.get("date") or {}).get("$") if isinstance(did, dict) else None
        if d:
            return d
    return ""


def _titel(bib):
    """invention-title -> bester Titel-String. OPS: dict {$,@lang} ODER Liste mehrerer Sprachen.
    Bevorzugt Englisch (@lang='en'), sonst der erste nicht-leere."""
    titel = _als_liste(bib.get("invention-title"))
    kandidaten = [(t.get("@lang"), (t.get("$") or "").strip()) for t in titel if isinstance(t, dict)]
    kandidaten = [(lang, s) for lang, s in kandidaten if s]
    for lang, s in kandidaten:
        if lang == "en":
            return s
    return kandidaten[0][1] if kandidaten else ""


def _ipc_subklasse(text):
    """IPC-Text 'H01L  25/    07            A I' -> Subklasse 'H01L' (Section+Class+Subclass = 4 Zeichen).
    Die Subklasse ist die natürliche Sprossen-Zähl-Granularität."""
    kompakt = "".join((text or "").split())
    return kompakt[:4] if len(kompakt) >= 4 else kompakt


def _ipc_klassen(bib):
    """Alle IPC-Subklassen des Dokuments (dedupliziert, Reihenfolge stabil). Primär = sequence 1."""
    # `or {}` fängt den vorhandenen-aber-null-Key ab (Default greift nur bei fehlendem Key):
    ipc = _als_liste((bib.get("classifications-ipcr") or {}).get("classification-ipcr"))
    aus = []
    for c in ipc:
        if not isinstance(c, dict):
            continue
        sub = _ipc_subklasse((c.get("text") or {}).get("$", ""))
        if sub and sub not in aus:
            aus.append(sub)
    return aus


def parse_biblio(biblio_json):
    """OPS-biblio-JSON -> Liste normierter Patent-Datensätze (je exchange-document einer).

    Verifizierbarer Kern (offline gegen das ECHTE Schema getestet). PIT-getrennte Daten:
      t_event     = Anmeldedatum (application-reference), das echte Frühereignis
      t_disclosed = Publikationsdatum (publication-reference), die Wissbarkeits-Wand
    -> {patent_id, titel, klasse_ipc, klassen_ipc, t_event, t_disclosed} (ISO-Daten)."""
    aus = []
    for ed in _exchange_docs(biblio_json):
        bib = ed.get("bibliographic-data", {}) if isinstance(ed, dict) else {}
        if not bib:
            continue
        pub = _ops_zu_iso(_erstes_datum(bib.get("publication-reference")))
        app = _ops_zu_iso(_erstes_datum(bib.get("application-reference")))
        klassen = _ipc_klassen(bib)
        pid = ""
        if isinstance(ed, dict):
            pid = f"{ed.get('@country', '')}{ed.get('@doc-number', '')}{ed.get('@kind', '')}"
        aus.append({
            "patent_id": pid,
            "titel": _titel(bib),
            "klasse_ipc": klassen[0] if klassen else "",
            "klassen_ipc": klassen,
            # t_event = Anmeldedatum; bei Fehlen NICHT auf pub setzen (das täuschte einen 0-Vorlauf
            # vor, QS Gemini-B4) -> leer lassen, damit Verweildauer-Analysen die Lücke sehen.
            "t_event": app,
            "t_disclosed": pub,           # Publikation = PIT-Wand
        })
    return aus


def fetch_biblio(docdb_ref, token, timeout=40):
    """LIVE: biblio zu einer docdb-Referenz 'CC.NUMBER.KIND' (z. B. 'EP.3276660.A1'). -> biblio-JSON."""
    return _curl_json(_BIBLIO.format(ref=docdb_ref), token, timeout)


def _abstract_text(p):
    """OPS-`abstract/p` -> Text. `p` ist ein dict {$: text} ODER eine Liste mehrerer Absätze -> verbinden."""
    if isinstance(p, dict):
        return (p.get("$") or "").strip()
    if isinstance(p, list):
        return " ".join((x.get("$") or "").strip() for x in p if isinstance(x, dict)).strip()
    return ""


def parse_abstract(abstract_json):
    """OPS-/abstract-JSON -> {patent_id, abstract}. Der Abstract ist die alpha-tragende PROSA, die `/biblio`
    NICHT liefert (deshalb kamen Patente als 89-Zeichen-Stümpfe an → 1c fand ~keine Fakten, 07.08.-Komposi-
    tionsdiagnose). Englisch bevorzugt (@lang='en'); mehrere `<p>` verbunden; sonst der erste nicht-leere.
    **Fable-QS Minor-4:** eine kind-lose Referenz liefert MEHRERE exchange-documents — ALLE scannen und das
    erste mit nicht-leerem Abstract nehmen (sonst ginge ein Abstract im 2. Dokument still verloren); nur wenn
    KEINES einen Abstract trägt, den patent_id des ersten zurückgeben. Fail-safe: kein Abstract -> abstract=''.
    Reiner Parser, offline gegen das ECHTE OPS-Schema getestet (Fixture aus einem Live-Abruf)."""
    erster_pid = ""
    for ed in _exchange_docs(abstract_json):
        if not isinstance(ed, dict):
            continue
        pid = f"{ed.get('@country', '')}{ed.get('@doc-number', '')}{ed.get('@kind', '')}"
        if not erster_pid:
            erster_pid = pid
        kandidaten = []
        for a in _als_liste(ed.get("abstract")):
            if not isinstance(a, dict):
                continue
            txt = _abstract_text(a.get("p"))
            if txt:
                kandidaten.append((a.get("@lang"), txt))
        en = [t for lang, t in kandidaten if lang == "en"]
        txt = en[0] if en else (kandidaten[0][1] if kandidaten else "")
        if txt:                                             # erstes exchange-document MIT Abstract gewinnt
            return {"patent_id": pid, "abstract": txt}
    return {"patent_id": erster_pid, "abstract": ""}


def fetch_abstract(docdb_ref, token, timeout=40):
    """LIVE: Abstract zu einer docdb-Referenz. -> abstract-JSON. **Kein Abstract = leer, kein Fehler:** nicht
    jedes Patent hat einen (englischen) Abstract; OPS antwortet dann mit einem NotFound-Fault → als {}
    behandelt (fail-safe), damit ein abstract-loses Patent den Anreicherungs-Lauf nicht kippt."""
    try:
        return _curl_json(_ABSTRACT.format(ref=docdb_ref), token, timeout)
    except RuntimeError as e:
        m = str(e)
        if any(s in m for s in ("EntityNotFound", "No results found", "NotFound")):
            return {}
        raise


def zu_dokumente(patent_records, abstracts=None):
    """Patent-Records (parse_biblio) -> ROH-DOKUMENTE im scraper.db-`documents`-Schema (Sammellauf-Naht,
    kompatibel/mergebar). source_type='epo' (-> Quellentyp `patent`, frühe Sprosse der Reifegradleiter).
    title = Patent-Titel, published_at = t_disclosed (Publikations-/Wissbarkeits-Wand — PIT, NICHT das
    Anmeldedatum). Ohne Titel/Datum entfällt der Eintrag (fail-closed).

    `abstracts` ({patent_id -> Abstract-Text}, aus `hole_abstracts`): der `/abstract`-Constituent trägt die
    ALPHA-tragende Prosa, die `/biblio` NICHT liefert. Ist er gesetzt, wird der Abstract in `text` gemergt
    (Titel + ABSTRACT + IPC-Klassen) → 1c bekommt echten Text statt der 89-Zeichen-Titelzeile. None/leer =
    byte-identisch zum Alt-Verhalten (Titel + IPC). -> Liste [{source_type,title,text,url,published_at}]."""
    abstracts = abstracts or {}
    out = []
    for p in patent_records:
        titel = (p.get("titel") or "").strip()
        pub = p.get("t_disclosed")
        if not titel or not pub:
            continue
        ab = (abstracts.get(p.get("patent_id")) or "").strip()
        text = " ".join(x for x in (titel, ab, " ".join(p.get("klassen_ipc") or [])) if x)
        out.append({"source_type": "epo", "title": titel, "text": text,
                    "url": p.get("patent_id"), "published_at": pub})
    return out


def such_referenzen(search_json):
    """OPS-Such-JSON -> docdb-Referenzstrings 'CC.NUMBER.KIND' der Treffer (für den biblio-Feinabruf).
    Reiner Parser (leere Liste bei fehlenden Treffern)."""
    try:
        res = search_json["ops:world-patent-data"]["ops:biblio-search"]["ops:search-result"]
        refs = _als_liste(res.get("ops:publication-reference"))
    except (KeyError, TypeError):
        return []
    aus = []
    for r in refs:
        # `document-id` kommt als dict ODER Liste (docdb+epodoc) — wie im biblio-Pfad über
        # `_als_liste` normieren (QS Claude-B2) und den docdb-Eintrag nehmen (country/kind tragend).
        dids = [d for d in _als_liste(r.get("document-id") if isinstance(r, dict) else None)
                if isinstance(d, dict)]
        docdb = next((d for d in dids if d.get("@document-id-type") == "docdb"), None)
        did = docdb or (dids[0] if dids else {})
        cc = (did.get("country") or {}).get("$", "")
        num = (did.get("doc-number") or {}).get("$", "")
        kind = (did.get("kind") or {}).get("$", "")
        if cc and num:
            aus.append(f"{cc}.{num}.{kind}" if kind else f"{cc}.{num}")
    return aus


# ------------------------------------------------------------------ #
# Patent-Datensatz -> Modul-2 facts (Verortung Modul 1/2/5, quellentyp="patent")
# ------------------------------------------------------------------ #
def zu_facts(patent_records, t_ingest=None):
    """Normierte Patent-Datensätze (parse_biblio) -> Modul-2-`facts`-Eingang (je Patent eine Zeile).

    Spiegelt den arXiv-Pfad: Titel als Text, `quellentyp="patent"`, `rolle` technologie. Die
    Kategorisierung (Modul 2) konsumiert diese Datensätze. Keine Insel: derselbe facts-
    Kontrakt wie sammler_db/arxiv_fetch."""
    t_ingest = t_ingest or datetime.date.today().isoformat()
    out = []
    for i, p in enumerate(patent_records):
        # Fallback-Richtung PIT-sicher (QS Claude-B7): fehlt die Offenlegung, auf t_ingest (spätest-
        # konservativ) zurückfallen — NIE auf das ~18 Mon. frühere t_event (das machte den Fakt
        # scheinbar früher wissbar). t_event fällt seinerseits nur auf t_disclosed (nie später).
        t_disc = p.get("t_disclosed") or t_ingest
        t_ev = p.get("t_event") or t_disc
        out.append({
            "fact_id": p.get("patent_id") or f"epo{i}",
            "subjekt": (p.get("titel") or "")[:2000], "beziehung": "", "objekt": "",
            "quellentyp": "patent", "rolle": "technologie",
            "t_event": t_ev,
            "t_disclosed": t_disc,
            "t_ingest": t_ingest,
        })
    return out
