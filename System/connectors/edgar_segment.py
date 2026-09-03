"""
edgar_segment.py — Konnektor: SEC 10-K/10-Q Segmentbericht (CapEx je Segment) -> Modul-11 (kapital_roh).

F92 (entschieden 22.07., Jens): Form D erfasst nur private VC-Runden — Trafo-/Netz-CapEx kommt von
BÖRSENNOTIERTEN Großkonzernen (Siemens Energy, Hitachi, GE, Eaton). Deren real investiertes CapEx
steht im **Segmentbericht** der 10-K/10-Q (XBRL, `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`
je Segment-Dimension), NICHT in Form D. Dieser Konnektor formt Segment-CapEx in `kapital_roh`:
- `commitment_stufe = eingesetzt` (10-K/10-Q berichtet TATSÄCHLICH ausgegebenes CapEx — nicht
  Ankündigung; höchste Grounding-Stufe, §4 Modul 11).
- **SIC-Filter** gegen Fehlalarm: der Live-Run zeigte, dass Volltext-'transformer' Fonds/Healthcare
  fängt. `filter_nach_sic` behält nur die relevanten Elektro-/Energie-SIC-Klassen VOR jedem Mapping.

Die verifizierbare Transformation (Segment-Record -> kapital_roh) ist offline getestet; `fetch_*` ist
ein dokumentiertes Skelett (braucht Netzzugang zu data.sec.gov / XBRL). Nur Standardbibliothek.

Abbildung: t_event = Segment-Periodenende (reales Ereignis), t_disclosed = filingDate (Offenlegung),
t_ingest = Abrufzeit. kat_id über die geteilte Taxonomie (segment_map: (CIK, Segment) -> Knoten).
"""
import os

#: SEC EDGAR requires a User-Agent carrying a real contact address (fair-access policy).
#: It is read from the environment rather than hard-coded: a contact address baked into a
#: public repository is published to everyone, not just to the SEC.
_UA = os.environ.get("CHRONOS_CONTACT_UA", "Chronos-Stream jens@alker.org")

# Default-SIC-Whitelist für die Trafo/Grid-Kette (Präfix-Match erlaubt). Erweiterbar je Kette.
# 3612 Power/Distribution Transformers · 3613 Switchgear · 362x Motors/Generators ·
# 351x Engines/Turbines · 4911 Electric Services · 3548/3569 Industrieausrüstung.
SIC_WHITELIST_GRID = ("3612", "3613", "3620", "3621", "3510", "3511", "3548", "3569", "4911")


def passt_sic(sic, whitelist):
    """SIC-Präfix-Match: '3612' passt zu Whitelist-Eintrag '3612' oder '36'. Leeres/None-SIC -> False
    (kein Default-Durchlass — Anti-Fehlalarm, F92/Live-Run)."""
    if sic is None:
        return False
    s = str(sic).strip()
    return any(s.startswith(str(w)) for w in whitelist) if s else False


def filter_nach_sic(records, whitelist, sic_key="sic"):
    """Behält nur Records, deren SIC in der Whitelist liegt (Fonds/Healthcare fallen raus, bevor
    ein Mapping/LLM läuft). Gefilterte werden verworfen, nicht still gezählt — der Aufrufer loggt."""
    return [r for r in records if passt_sic(r.get(sic_key), whitelist)]


def zu_kapital_roh(segment_records, segment_map, t_ingest, sic_whitelist=None):
    """Segment-CapEx-Records + Taxonomie-Map ((CIK, Segment) | Segment -> (kat_id, version)) ->
    kapital_roh (Modul 11). Optionaler SIC-Filter VOR dem Mapping (F92). Records ohne Taxonomie-
    Treffer werden übersprungen (kein Default-Filling). commitment_stufe = eingesetzt (real ausgegeben)."""
    recs = filter_nach_sic(segment_records, sic_whitelist) if sic_whitelist else segment_records
    out = []
    for r in recs:
        knoten = segment_map.get((r.get("cik"), r.get("segment"))) or segment_map.get(r.get("segment"))
        if not knoten:
            continue
        kat_id, version = knoten
        betrag = r.get("capex")
        out.append({
            "kat_id": kat_id, "version": version,
            "art": "capex", "richtung": "zufluss",
            "commitment_stufe": r.get("commitment_stufe", "eingesetzt"),   # 10-K/10-Q = tatsächlich ausgegeben
            "betrag_numerisch": float(betrag) if betrag is not None else None,
            "betrag_klasse_ordinal": None,
            "kapital_intransparent": False,                # US-EDGAR = transparent (F47)
            "text": f"{r.get('entityName', '')} — Segment '{r.get('segment', '')}' CapEx",
            "t_event": r.get("periodeEnde") or r.get("filingDate"),
            "t_disclosed": r.get("filingDate"),
            "t_ingest": t_ingest,
        })
    return out
