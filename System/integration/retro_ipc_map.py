"""
retro_ipc_map.py — die Brücke IPC-Patentklasse → System-Kategorie (QS-B8 des EPO-Konnektors).

Der EPO-Konnektor zählt Patente je **IPC-Subklasse** (H01L, H02J, …); der Outcome/DCF-Teil arbeitet je
**System-Kategorie** (Transformatoren, Stromnetz, …, `gemini_llm.DEFAULT_VOKABULAR`). Diese Map ist der
Join zwischen beiden — analog zu `retro_kat_map` (Symbol→Kategorie), nur auf der Signal-Seite.

**Grounding (Guardrail „realitätsnahe Daten"):** jede Klasse wurde live gegen ops.epo.org auf Volumen
geprüft (24.07., Jahrescounts 2018: H01L≈70k, H02J≈51k, G06F≈201k, H01F≈20k, H02M≈19k, H02G≈22k, H05K≈49k).
Nur die vier Technologie-Kategorien, die AUCH im hand-verifizierten Gold-Symbol-Universum (`retro_kat_map`)
liegen — so bleibt Signal- und Outcome-Seite auf DERSELBEN Kategorie-Achse (kein Mapping-Bruch im Join).

**Bewusst AUSGESCHLOSSEN — ehrlich:**
- **Kupfer** (und Rohstoffe generell): kein sauberes Patent-Beschleunigungs-Signal. Kupfer-Nachfrage ist
  ein Mengen-/Preis-Phänomen (Bergbau), keine patentgetriebene Technologie-Sprosse. Ein IPC-Zwang wäre
  konstruiert → weggelassen (kein Default-Filling, §4-Disziplin).
- Photovoltaik/Windkraft/Festkoerperbatterie: sauber mappbar (H02S/F03D/H01M, live gegroundet), aber im
  Gold-Symbol-Universum (noch) ohne Konstituenten → erst mit der survivorship-freien Verbreiterung
  (Mehr-Kategorien-Power) sinnvoll dazuzunehmen. Hier terminiert ausgelassen, damit der Join dicht bleibt.

Reine Daten + Zugriff, kein Netz. Std-Lib.
"""

# {kat_id -> [IPC-Subklassen]} — die Technologie-Sprosse je System-Kategorie.
# H01L liegt bei Halbleiter (nicht zusätzlich bei Photovoltaik) — Doppelzählung EINER Klasse über zwei
# Kategorien würde den Querschnitt verzerren; die PV-Zuordnung wartet auf eigene PV-Symbole (H02S).
IPC_KAT_MAP = {
    "Transformatoren": ["H01F", "H02M"],   # Magnete/Induktivitäten/Transformatoren + Leistungswandlung
    "Stromnetz":       ["H02J", "H02G"],   # Netz-Einspeisung/-Verteilung + Kabel/Leitungsbau
    "Halbleiter":      ["H01L"],           # Halbleiterbauelemente
    "Rechenzentrum":   ["G06F", "H05K"],   # digitale Datenverarbeitung + Leiterplatten/Kühlung
}


def ipc_kat_map():
    """-> {kat_id -> [ipc_klassen]}. Einzige Quelle der Wahrheit für die Signal-Seite des Patent-Ankers."""
    return {k: list(v) for k, v in IPC_KAT_MAP.items()}
