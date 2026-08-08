"""
edgar_form_d.py — Konnektor: SEC EDGAR Form D -> Modul-11-Input (kapital_roh).

Form D meldet Eigenkapital-/Seed-Runden (gesetzlich, frei, öffentlich). Die Transformation
(Form-D-Record -> kapital_roh) ist der verifizierbare Kern und offline getestet. `fetch_form_d`
ist LIVE lauffähig (efts.sec.gov ist über den Agent-Proxy erreichbar): EDGARs Full-Text-Search ist
eine **GET-API mit Query-Parametern** (`?q=...&forms=D&startdt=...&enddt=...`), KEIN JSON-POST — der
frühere POST-mit-Body-Entwurf lieferte 0 Treffer und war nie live validiert (Egress war geblockt). Die
URL-Konstruktion ist in `_fts_url` gekapselt und unit-getestet (Bug-Guard).

Abbildung (Form D -> kapital_roh, Modul 11):
- Form D = tatsächlich verkaufte Anteile (amountSold) -> commitment_stufe `committed`
  (Investoren haben Kapital committed — nicht bloß angekündigt; Anti-Ankündigungs-Falle, §4).
- betrag_numerisch = totalAmountSold. kat_id über die geteilte Taxonomie (kat_map: CIK/Branche -> Knoten).
- t_event = dateOfFirstSale (reales Ereignis), t_disclosed = filingDate (Offenlegung),
  t_ingest = Abrufzeit (Wissenszeit).  Nur Standardbibliothek.
"""
import json
import subprocess
from urllib.parse import urlencode

_EFTS = "https://efts.sec.gov/LATEST/search-index"      # Full-Text-Search (GET, Query-Parameter)
_UA = "Makro-Thesen-Fabrik jacand@protonmail.com"       # EDGAR verlangt Kontakt-UA


def _fts_url(query, seit, bis=None, forms="D"):
    """Baut die EDGAR-Full-Text-Search-URL (GET). Reiner Bug-Guard (unit-getestet): `seit`/`bis` gehen
    als `startdt`/`enddt`, NICHT als JSON-Body `dateRange` (der alte POST-Entwurf lieferte 0 Treffer)."""
    params = {"q": query, "forms": forms, "startdt": seit}
    if bis:
        params["enddt"] = bis
    return f"{_EFTS}?{urlencode(params)}"


def zu_kapital_roh(form_d_records, kat_map, t_ingest):
    """Form-D-Records (geparst) + Taxonomie-Map (CIK|Branche -> (kat_id, version)) -> kapital_roh.
    Records ohne Taxonomie-Treffer werden übersprungen (kein Default-Filling)."""
    out = []
    for r in form_d_records:
        schluessel = r.get("cik") or r.get("industryGroup")
        knoten = kat_map.get(schluessel)
        if not knoten:
            continue
        kat_id, version = knoten
        betrag = r.get("totalAmountSold")
        out.append({
            "kat_id": kat_id, "version": version,
            "art": "funding", "richtung": "zufluss",
            "commitment_stufe": "committed",              # Form D = real verkauft, nicht angekündigt
            "betrag_numerisch": float(betrag) if betrag is not None else None,
            "betrag_klasse_ordinal": None,
            "kapital_intransparent": False,               # US-EDGAR = transparent (F47)
            "text": r.get("entityName", ""),
            "t_event": r.get("dateOfFirstSale") or r.get("filingDate"),
            "t_disclosed": r.get("filingDate"),
            "t_ingest": t_ingest,
        })
    return out


def fetch_form_d(query, seit, bis=None, ua=_UA, timeout=30):
    """LIVE: EDGAR Full-Text-Search nach Form-D-Filings (GET, `_fts_url`). `seit`/`bis` = Offenlegungs-
    Datumsfenster (startdt/enddt, YYYY-MM-DD). Rückgabe: Liste geparster Form-D-Records für
    zu_kapital_roh(). Der Betrag (totalAmountSold) steht nicht im Index -> fetch_betrag nachladen."""
    url = _fts_url(query, seit, bis)
    out = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-H", f"User-Agent: {ua}", url],
        capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"EDGAR nicht erreichbar: rc={out.returncode} {out.stderr[:200]}")
    body = out.stdout.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"EDGAR: keine JSON-Antwort: {body[:200]}")
    return _parse_hits(data)


def _erste(v):
    """EDGAR liefert ciks/display_names/sics als Listen — nimm das erste Element."""
    return v[0] if isinstance(v, list) and v else (v if not isinstance(v, list) else None)


def _parse_hits(data):
    """EDGAR-Full-Text-Search-Treffer (echte Felder: ciks/display_names/file_date/sics) ->
    minimale Form-D-Records. Der Betrag (totalAmountSold) steht im Primärdokument (primary_doc.xml)
    und wird bei Bedarf per Feinabruf nachgeladen — hier None gelassen (Konnektor bleibt schlank)."""
    records = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        records.append({
            "cik": _erste(src.get("ciks")) or _erste(src.get("cik")),
            "adsh": src.get("adsh"),                     # Accession -> Primärdokument-Abruf
            "entityName": _erste(src.get("display_names")) or "",
            "industryGroup": _erste(src.get("sics")),
            "filingDate": src.get("file_date"),
            "dateOfFirstSale": src.get("file_date"),
            "totalAmountSold": None,
        })
    return records


def zu_dokumente(records):
    """Form-D-Records (_parse_hits) -> ROH-DOKUMENTE im scraper.db-`documents`-Schema (Sammellauf-Naht,
    kompatibel/mergebar). source_type='edgar' (-> Quellentyp `funding`, mittlere Sprosse der Reifegrad-
    leiter). title = entityName, published_at = filingDate (Offenlegung). Einträge ohne Name/Datum
    entfallen (published_at NOT NULL, fail-closed). -> Liste [{source_type,title,text,url,published_at}]."""
    out = []
    for r in records:
        name = (r.get("entityName") or "").strip()
        pub = r.get("filingDate")
        if not name or not pub:
            continue
        cik = r.get("cik")
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}" if cik else None)
        text = " ".join(x for x in (name, "Form D (Kapitalzufluss)", str(r.get("industryGroup") or "")) if x)
        out.append({"source_type": "edgar", "title": name, "text": text, "url": url, "published_at": pub})
    return out


def sammle_form_d(query, seit, bis=None, max_results=50, ua=_UA, timeout=30):
    """Bequem: aktuelle Form-D-Filings (Kapitalzufluss) im Fenster [seit,bis] als kompatible Roh-
    dokumente fürs Einsammeln. -> Liste documents-Zeilen."""
    return zu_dokumente(fetch_form_d(query, seit, bis=bis, ua=ua, timeout=timeout))[:max_results]


def fetch_betrag(cik, adsh, ua=_UA, timeout=30):
    """Lädt totalAmountSold aus dem Form-D-Primärdokument (primary_doc.xml). Der echte Betrag steht
    NICHT im Full-Text-Index. -> float oder None. Braucht Netzzugang zu www.sec.gov."""
    import re
    adsh_nd = str(adsh).replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_nd}/primary_doc.xml"
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-H", f"User-Agent: {ua}", url],
                         capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        return None
    m = re.search(r"<totalAmountSold>([^<]+)</totalAmountSold>", out.stdout)
    try:
        return float(m.group(1)) if m else None
    except (TypeError, ValueError):
        return None
