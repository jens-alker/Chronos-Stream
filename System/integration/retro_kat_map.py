"""
retro_kat_map.py — Gold-Symbol→Kategorie-Map für den Modul-8-Datenlauf (F102, von Jens bestätigt 22.07.).

Punkt 3 der Datenlauf-Readiness: `verdichte_kategorie`/`baue_folds` brauchen `{symbol_id -> kat_id}`,
um Symbol-Returns (EODHD) auf die These-Kategorien zu verdichten. Klein & hand-verifiziert (Gold-Subset,
deckt sich mit dem Modul-2d-Gold-Subset). Erweiterbar; die survivorship-freie VOLL-Kohorte braucht später
eine breite (SIC-basierte) Map (Readiness-Doc, v1) — diese Gold-Map ist der erste reale Lauf.

kat_id-Vokabular = harness/gemini_llm.DEFAULT_VOKABULAR (die Extraktions-Kategorien).
"""

# {symbol_id (EODHD, .EXCHANGE-Suffix) -> kat_id}
GOLD_KAT_MAP = {
    # Transformatoren / Leistungselektronik am Netz
    "ABBN.SW": "Transformatoren", "ENR.DE": "Transformatoren", "GEV.US": "Transformatoren",
    "6501.TSE": "Transformatoren", "267260.KO": "Transformatoren",   # Hitachi, Hyundai Electric
    # Stromnetz / Kabel / Netzbau
    "PRY.MI": "Stromnetz", "NEX.PA": "Stromnetz", "PWR.US": "Stromnetz", "NG.LSE": "Stromnetz",
    # Kupfer
    "FCX.US": "Kupfer", "SCCO.US": "Kupfer", "ANTO.LSE": "Kupfer",
    # Halbleiter
    "TSM.US": "Halbleiter", "ASML.US": "Halbleiter", "NVDA.US": "Halbleiter",
    # Rechenzentrum
    "EQIX.US": "Rechenzentrum", "DLR.US": "Rechenzentrum",
}


def kat_map():
    """-> {symbol_id -> kat_id}. Einzige Quelle der Wahrheit für den Gold-Datenlauf."""
    return dict(GOLD_KAT_MAP)
