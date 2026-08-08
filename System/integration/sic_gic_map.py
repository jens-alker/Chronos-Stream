"""
sic_gic_map.py — die Brücke SIC-Branchencode → GIC-System-Kategorie (funding-in-Modul-11, Teil 2/4, 07.08.).

EDGAR Form D trägt je Filing einen SIC-Code (`sics`); der Rechenpfad rechnet je GIC-Kategorie (GicSubIndustry,
`retro_kat_map_gic_breit`). Diese Map ist der Join — der funding-Analog zu `retro_ipc_map` (IPC→Kategorie,
Patent-Seite). `zu_kapital_roh(form_d_records, kat_map, …)` konsumiert genau das Format {SIC → (kat_id, version)}.

**Fail-closed gegen das echte GIC-Universum (harter Riegel):** ein SIC, dessen Zielkategorie NICHT im geladenen
GIC-Universum liegt (z. B. „Semiconductors" — im breiten Universum gar nicht klassifiziert), wird GEDROPPT statt
eine tote Kategorie ohne Outcome-Symbole zu erzeugen (die Signal- und Outcome-Achse müssen dieselbe sein — sonst
Mapping-Bruch im Join, wie bei `retro_ipc_map` dokumentiert). version=1 (GIC-Kategorien tragen v1, betrieb_lauf:95).

KEINE INSEL: reine Taxonomie-Zuordnung (0/2), keine Signal-/Kategorie-Definition. Die kuratierte SIC→GIC-Tabelle
ist hand-autorisiert (Jens-Review-Kandidat) und deckt die Form-D-relevanten Sektoren (Biotech/Health/Software/
Energie/Industrie/Materialien). Reiner Kern offline gegen das echte GIC-Universum getestet.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_GIC_DEFAULT = os.path.join(_HERE, "retro_kat_map_gic_breit.json")

# {SIC (4-stellig, EDGAR) -> GicSubIndustry-Name}. Nur Ziele, die im breiten GIC-Universum existieren (der
# Validator wirft den Rest raus). Kuratiert für die Sektoren, in denen Form-D-/VC-Runden auftreten.
SIC_GIC = {
    # Pharma / Biotech / Life Sciences
    "2834": "Pharmaceuticals",              # Pharmaceutical Preparations
    "2835": "Life Sciences Tools & Services",  # In Vitro & In Vivo Diagnostics
    "2836": "Biotechnology",                # Biological Products
    "8731": "Life Sciences Tools & Services",  # Commercial Physical & Biological Research
    "3826": "Life Sciences Tools & Services",  # Laboratory Analytical Instruments
    # Health Care Equipment / Supplies / Services
    "3841": "Health Care Equipment",        # Surgical & Medical Instruments
    "3845": "Health Care Equipment",        # Electromedical Apparatus
    "3842": "Health Care Supplies",         # Orthopedic, Prosthetic
    "8000": "Health Care Services",         # Health Services
    "8011": "Health Care Services",         # Offices of Physicians
    # Software / Internet
    "7372": "Application Software",          # Prepackaged Software
    "7371": "Application Software",          # Computer Programming Services
    "7370": "Application Software",          # Computer Services
    # 7389 (Business Services NEC) RAUS (Fable-QS Minor): größter EDGAR-Catch-all (Consulting/Marketing/
    # Fintech-Services) -> keine saubere Software-Zuordnung.
    "7374": "Internet Services & Infrastructure",  # Data Processing
    "7375": "Interactive Media & Services",  # Information Retrieval Services
    "7373": "Systems Software",             # Computer Integrated Systems Design
    # Communications / Electronic Components
    "3661": "Communications Equipment",     # Telephone & Telegraph Apparatus
    "3663": "Communications Equipment",     # Radio & TV Broadcasting Equipment
    "3669": "Communications Equipment",     # Communications Equipment NEC
    "3672": "Electronic Components",        # Printed Circuit Boards
    "3679": "Electronic Components",        # Electronic Components NEC
    # Energie
    "1311": "Oil & Gas Exploration & Production",   # Crude Petroleum & Natural Gas
    "1381": "Oil & Gas Drilling",           # Drilling Oil & Gas Wells
    "1389": "Oil & Gas Equipment & Services",  # Oil & Gas Field Services
    "2911": "Oil & Gas Refining & Marketing",  # Petroleum Refining
    "4911": "Electric Utilities",           # Electric Services
    # 4931 (Electric & Other Services Combined) RAUS (Fable-QS Minor): konventionelle Verbund-Versorger,
    # NICHT Renewable — die alte Zuordnung war ein systematischer Bias in die Hype-Kategorie.
    # Fahrzeuge / Luftfahrt
    "3711": "Automobile Manufacturers",     # Motor Vehicles & Car Bodies
    "3728": "Aerospace & Defense",          # Aircraft Parts
    "3760": "Aerospace & Defense",          # Guided Missiles & Space Vehicles
    "3812": "Aerospace & Defense",          # Search, Detection, Navigation
    # Chemie / Materialien / Metalle
    "2860": "Specialty Chemicals",          # Industrial Organic Chemicals
    "2890": "Specialty Chemicals",          # Industrial Chemicals NEC
    "2820": "Commodity Chemicals",          # Plastics Materials & Resins
    "2870": "Fertilizers & Agricultural Chemicals",  # Agricultural Chemicals
    "3310": "Steel", "3312": "Steel",       # Steel Works
    "1040": "Gold",                         # Gold Mining
    "1000": "Diversified Metals & Mining",  # Metal Mining
    # Industrie-Maschinen
    "3559": "Industrial Machinery & Supplies & Components",  # Special Industry Machinery
    "3560": "Industrial Machinery & Supplies & Components",  # General Industrial Machinery
    "3674": "Semiconductors",               # Semiconductors — ABSICHTLICH: fehlt im Universum -> Validator dropt's
}


def _gic_universum(gic_map_pfad=None):
    """Die Menge der GIC-kat_ids, die das breite Universum tatsächlich klassifiziert (die Outcome-Achse)."""
    pfad = gic_map_pfad or _GIC_DEFAULT
    try:
        roh = json.load(open(pfad, encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    m = roh.get("map", roh) if isinstance(roh, dict) else {}
    return {v for v in m.values() if isinstance(v, str)}


def sic_gic_map(gic_map_pfad=None, universum=None, version=1, warnen=True):
    """{SIC -> (kat_id, version)} für `zu_kapital_roh`. FAIL-CLOSED: nur SICs, deren Zielkategorie im GIC-
    Universum existiert (sonst tote Kategorie ohne Outcome-Symbole). `universum` injizierbar (Test); sonst aus
    der breiten GIC-Map. -> dict. Gedroppte (Ziel nicht im Universum) werden gezählt/gewarnt (Stille≠Grün)."""
    uni = universum if universum is not None else _gic_universum(gic_map_pfad)
    aus, gedroppt = {}, []
    for sic, kat in SIC_GIC.items():
        if kat in uni:
            aus[sic] = (kat, version)
        else:
            gedroppt.append((sic, kat))
    if warnen and gedroppt:
        import sys
        namen = sorted({k for _, k in gedroppt})
        print(f"  ⚠ sic_gic_map: {len(gedroppt)} SIC-Zuordnungen gedroppt (Ziel nicht im GIC-Universum): "
              f"{', '.join(namen)}", file=sys.stderr)
    return aus


def cik_sic_gic(cik_sic_map, gic_map_pfad=None, universum=None, version=1):
    """Optionaler Präzisions-Pfad: {CIK -> SIC} (aus den Form-D-Filings) -> {CIK -> (kat_id, version)}, über
    dieselbe fail-closed SIC→GIC-Brücke. So kann `zu_kapital_roh` per CIK (feiner als SIC) auflösen. -> dict."""
    sg = sic_gic_map(gic_map_pfad, universum, version, warnen=False)
    return {cik: sg[sic] for cik, sic in cik_sic_map.items() if sic in sg}
