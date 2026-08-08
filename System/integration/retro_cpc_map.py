"""
retro_cpc_map.py — FEINERE Patent-Taxonomie: emerging-tech CPC-Gruppen je Kategorie (statt breiter IPC-Subklassen).

Grobe IPC-Subklassen (H01L ≈70k/Jahr) sind zu breit, um die technologische Vorderkante einer Branche
abzubilden. Diese Map ordnet je Kategorie **schmalere, emerging-tech-fokussierte CPC-Klassen** zu (der
`feld="cpc"`-Pfad des Konnektors), je Kategorie ihre technologische Vorderkante:

- **Halbleiter → G03F** (Photolithografie): der eigentliche Fortschritts-/Engpass-Vektor der Chip-Fertigung
  (ASMLs Domäne), 7 144/2018 statt H01L 70 219 — zielgerichteter auf die Kapazitäts-/Node-Verschiebung.
- **Stromnetz → Y04S** (Smart Grid): die CPC-Y-Klasse „Technologien zur Anpassung des Stromnetzes"
  (Digitalisierung/Integration), 8 089/2018 — die aufkommende Kante gegenüber H02J (reiner Netzbetrieb).
- **Transformatoren → H02M** (Leistungswandlung): Halbleiter-Leistungselektronik (WBG-getrieben),
  12 036/2018 — schmaler als H01F (klassische Magnetik).
- **Rechenzentrum → G06N** (KI/maschinelles Lernen): der Wachstumstreiber der Rechenzentrums-Nachfrage,
  18 393/2018 statt G06F 201 175 (alle Datenverarbeitung) — die emerging-Nachfrage statt der Trägheit.

**Kupfer** bleibt ausgeschlossen (Rohstoff, kein Patent-Signal — s. `retro_ipc_map`).

**Live gegen ops.epo.org gegroundet (24.07.):** jede Klasse auf Volumen + Narrowness geprüft. CPC matcht in
OPS die Klasse selbst (Feld `cpc`, nicht `ipc`). Datierte-Untergruppen mit `/` (z. B. H01L21/02) sind über OPS
nicht direkt zählbar (Fault) → 4-stellige CPC-Klassen / Y-Schema, die tragen. Std-Lib.
"""

# {kat_id -> [emerging-tech CPC-Klassen]} — die technologische Vorderkante je Kategorie (feld="cpc").
CPC_KAT_MAP = {
    "Halbleiter":     ["G03F"],   # Photolithografie (Fertigungs-Engpass/Node-Fortschritt)
    "Stromnetz":      ["Y04S"],   # Smart Grid (Netz-Digitalisierung/-Integration)
    "Transformatoren": ["H02M"],  # Leistungswandlung (WBG-Leistungselektronik)
    "Rechenzentrum":  ["G06N"],   # KI/ML (Rechenzentrums-Nachfragetreiber)
}


def cpc_kat_map():
    """-> {kat_id -> [cpc_klassen]}. Feinere Signal-Klassen für den Patent-Anker (feld='cpc')."""
    return {k: list(v) for k, v in CPC_KAT_MAP.items()}
