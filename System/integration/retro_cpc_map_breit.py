"""
retro_cpc_map_breit.py — BREITE IPC→Branchen-Zuordnung über die GANZE Wirtschaft.

Diese Map ordnet den survivorship-freien GicSubIndustry-Kategorien (`retro_kat_map_gic_breit.json`) ihre
Patent-Klasse(n) zu — quer über Pharma, Agrar, Chemie, Energie, Metalle, Maschinenbau, Rüstung, Tech —
und macht so den Patent-Datenstrom je Branche adressierbar.

**Ehrliche Struktur-Grenze:** Patent-Daten existieren nur dort, wo Firmen patentieren. Patent-STARKE
Sektoren (Pharma A61K, Biotech C12N, Öl&Gas E21B,
Chemie C07/C08, Auto B60, Rüstung B64/F41, Halbleiter H01L, Metalle C22 …) werden echt getestet.
Patent-SCHWACHE Sektoren (Banken, Versicherer, Asset Manager, REITs, Handel, Gastro, Medien,
Fluglinien, Dienstleistungen) bleiben **bewusst UNBELEGT** — kein konstruierter CPC-Zwang (§4-Disziplin).
Ihre Frühsignale sitzen in ANDEREN Dokumenttypen (Funding/EDGAR, Filings) → eigener Anker-Kanal.

IPC-Subklasse (feld='ipc') matcht in OPS den Klassen-Teilbaum (breit, wie `ipc=H01L`); für die
patent-starken Branchen ist die 4-stellige Subklasse die natürliche Zähl-Granularität. Kategorie-Namen
EXAKT wie in der GIC-Map (CRISP-Tech-Namen deutsch: Halbleiter/Stromnetz/…; Rest englische GicSubIndustry).
Std-Lib.
"""

# {GicSubIndustry / CRISP-kat -> [IPC-Subklassen]} — nur patent-AKTIVE Branchen.
IPC_BRANCHE_MAP = {
    # --- Gesundheit / Life Science (patent-intensivster Block) ---
    "Biotechnology":                          ["C12N", "C12Q"],   # Gentechnik, Enzym-/Nukleinsäure-Assays
    "Pharmaceuticals":                        ["A61K", "A61P"],   # Wirkstoff-Präparate, therapeutische Wirkung
    "Health Care Equipment":                  ["A61B", "A61M"],   # Diagnose/Chirurgie, Applikationsgeräte
    "Health Care Supplies":                   ["A61F", "A61L"],   # Implantate/Prothesen, Sterilisation
    "Life Sciences Tools & Services":         ["G01N", "C12Q"],   # Materialuntersuchung, Bio-Assays
    "Health Care Technology":                 ["G16H"],           # Gesundheits-Informatik
    # --- Informationstechnik / Elektronik ---
    "Halbleiter":                             ["H01L", "G03F"],   # Halbleiterbauelemente, Photolithografie
    "Application Software":                   ["G06F", "G06N"],   # Datenverarbeitung, KI/ML
    "Systems Software":                       ["G06F"],
    "Data Processing & Outsourced Services":  ["G06F", "G06Q"],
    "Internet Services & Infrastructure":     ["H04L", "G06F"],   # Datenübertragung
    "Technology Hardware, Storage & Peripherals": ["G06F", "G11B"],  # Datenspeicherung
    "Communications Equipment":               ["H04B", "H04L"],   # Übertragung, digitale Übertragung
    "Electronic Equipment & Instruments":     ["G01R", "H01"],    # Messtechnik
    "Electronic Components":                  ["H01G", "H05K"],   # Kondensatoren, Leiterplatten
    "Electronic Manufacturing Services":      ["H05K"],
    "Electrical Components & Equipment":      ["H02K", "H01"],    # Maschinen/Generatoren
    "Rechenzentrum":                          ["G06F", "G06N"],
    "Stromnetz":                              ["H02J", "H02G"],
    "Transformatoren":                        ["H01F", "H02M"],
    # --- Energie / Öl & Gas ---
    "Oil & Gas Exploration & Production":     ["E21B"],           # Bohrtechnik
    "Oil & Gas Equipment & Services":         ["E21B", "F04B"],   # Bohren, Pumpen
    "Oil & Gas Storage & Transportation":     ["F17C", "F17D"],   # Druckbehälter, Pipelines
    "Integrated Oil & Gas":                   ["E21B", "C10G"],   # Bohren, Raffination
    "Electric Utilities":                     ["H02J", "F03D"],   # Netz, Windenergie (Erzeuger-Tech)
    # --- Grundstoffe / Chemie / Metalle ---
    "Specialty Chemicals":                    ["C07C", "C08L", "C09D"],  # organische Chemie, Polymere, Lacke
    "Commodity Chemicals":                    ["C07C", "C08L"],
    "Diversified Metals & Mining":            ["C22B", "E21C"],   # Metallgewinnung, Bergbau
    "Kupfer":                                 ["C22B", "C25C"],   # Metallurgie, Elektrolyse (Kupfer patentiert doch — Verhüttung)
    "Steel":                                  ["C21B", "C21D"],   # Roheisen, Wärmebehandlung
    "Gold":                                   ["C22B", "E21C"],
    # --- Industrie / Maschinen / Transport ---
    "Industrial Machinery & Supplies & Components": ["F16H", "B23"],   # Getriebe, Werkzeugmaschinen
    "Construction Machinery & Heavy Transportation Equipment": ["E02F", "B60P"],  # Bagger, Nutzfahrzeuge
    "Aerospace & Defense":                    ["B64C", "F41"],    # Flugzeuge, Waffen
    "Automotive Parts & Equipment":           ["B60", "F02"],     # Fahrzeuge, Verbrennungsmotoren
    "Building Products":                       ["E04B", "E06B"],   # Bauelemente, Türen/Fenster
    # --- Konsum / Nahrung ---
    "Packaged Foods & Meats":                 ["A23L"],           # Lebensmittel-Zubereitung
    "Household Products":                      ["A47L", "C11D"],   # Reinigungsgeräte, Waschmittel
    # --- Umwelt ---
    "Environmental & Facilities Services":     ["B09B", "C02F"],   # Abfall, Wasseraufbereitung
}


def ipc_branche_map(vorhandene_kategorien=None):
    """-> {kat_id -> [ipc_klassen]}, gefiltert auf `vorhandene_kategorien` (die im GIC-Universum
    tatsächlich besetzten Branchen), falls gegeben. So bleibt Signal- und Outcome-Seite deckungsgleich."""
    if vorhandene_kategorien is None:
        return {k: list(v) for k, v in IPC_BRANCHE_MAP.items()}
    vk = set(vorhandene_kategorien)
    return {k: list(v) for k, v in IPC_BRANCHE_MAP.items() if k in vk}


# SIC (US-Branchencode) -> Branche, deckungsgleich mit IPC_BRANCHE_MAP. Für die EDGAR-Sprossen
# (Funding/CapEx je SIC) der Konvergenz-/Gap-Rechnung. Nur Datenkarte (Taxonomie, Modul 0/2), kein Signal.
SIC_BRANCHE = {
    "2834": "Pharmaceuticals", "2836": "Biotechnology", "8731": "Biotechnology",
    "3841": "Health Care Equipment", "3845": "Health Care Equipment",
    "3674": "Halbleiter", "3559": "Halbleiter",
    "7372": "Application Software", "7370": "Application Software",
    "1311": "Oil & Gas Exploration & Production", "1381": "Oil & Gas Equipment & Services",
    "3714": "Automotive Parts & Equipment", "3711": "Automotive Parts & Equipment",
    "3728": "Aerospace & Defense", "3760": "Aerospace & Defense", "3812": "Aerospace & Defense",
    "2860": "Specialty Chemicals", "2890": "Specialty Chemicals", "2820": "Commodity Chemicals",
    "3310": "Steel", "3312": "Steel", "1000": "Diversified Metals & Mining", "1040": "Gold",
    "3560": "Industrial Machinery & Supplies & Components",
    "3663": "Communications Equipment", "3661": "Communications Equipment",
    "2000": "Packaged Foods & Meats", "2090": "Packaged Foods & Meats",
}


def sic_branche_map():
    """-> {sic -> kat_id}. Einzige Quelle der SIC->Branche-Zuordnung (EDGAR-Sprossen)."""
    return dict(SIC_BRANCHE)
