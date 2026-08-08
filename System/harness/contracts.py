"""
contracts.py — Schicht-D-Kontrakte, maschinell erzwungen.

Der Kontrakt-Validator des Test-/Integrations-Harness (Feinkonzept_SchichtS §3.0).
Erzwingt die kanonischen Konventionen aus Feinkonzept_Querschnitt_Datenhaltung_v1.md,
sodass Modul-Ausgänge NICHT still von der Konvention driften. Fängt u. a.:
  - `knoten_id`-Reste (Schicht D: Knoten = (kat_id, version))   → N3-Drift
  - ordinale/geurteilte Felder als Dezimalzahl (3.12-Verstoß)
  - fehlende Pflicht-Zeitspalten je Tabellen-Typ (tri-temporal)
Nur Standardbibliothek.
"""
from dataclasses import dataclass
from typing import Optional, List

# --- Kanonische Vokabulare (Schicht D §3.2 / Konvention 3.12) ---
STATUS = {"vermutet", "beobachtet", "erwiesen"}
ORDINAL_STAERKE = {"keine", "schwach", "mittel", "stark"}

# Verbotene Felder (Schicht D §3.2: knoten_id abgeschafft -> (kat_id, version))
FORBIDDEN_FIELDS = {"knoten_id"}

# Pflicht-Zeitspalten je Tabellen-Typ (Schicht D §3.1: vier Zeit-Rollen)
TIME_REQUIREMENTS = {
    "beobachtung": {"t_event", "t_disclosed", "t_ingest"},
    "projektion":  {"t_ref", "t_ingest"},
    "referenz":    {"t_valid_von", "t_valid_bis", "t_ingest"},
    "walkforward": {"t_valid_von", "t_ingest"},
    "operativ":    {"t_ingest"},
}


@dataclass
class Field:
    name: str
    kind: str                 # id|version|time|ordinal|status|enum|gemessen|abgeleitet|text|bool|list
    enum: Optional[set] = None
    nullable: bool = False


@dataclass
class TableContract:
    name: str
    art: str                  # beobachtung|projektion|referenz|walkforward|operativ
    fields: List[Field]
    key: List[str]            # natürlicher Schlüssel (inkl. Zeitspalte -> append-only)


@dataclass
class Violation:
    table: str
    row_index: int
    field: Optional[str]
    rule: str
    detail: str

    def __str__(self):
        loc = f"{self.table}[{self.row_index}]" + (f".{self.field}" if self.field else "")
        return f"{loc}: {self.rule} — {self.detail}"


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(rows, contract):
    """Prüft rows (list[dict]) gegen einen TableContract. Gibt list[Violation]."""
    viol = []
    art = contract.art
    if art not in TIME_REQUIREMENTS:
        viol.append(Violation(contract.name, -1, None, "unbekannter-tabellen-typ",
                              f"art='{art}' nicht in {sorted(TIME_REQUIREMENTS)}"))
        return viol
    required_time = TIME_REQUIREMENTS[art]
    declared = {f.name for f in contract.fields}

    # Kontrakt-Selbstprüfung
    for t in required_time:
        if t not in declared:
            viol.append(Violation(contract.name, -1, t, "zeit-spalte-fehlt-im-kontrakt",
                                  f"Typ '{art}' verlangt '{t}'"))
    for fname in declared:
        if fname in FORBIDDEN_FIELDS:
            viol.append(Violation(contract.name, -1, fname, "verbotenes-feld",
                                  "knoten_id abgeschafft -> (kat_id, version)"))

    # Zeilen-Prüfung
    for i, row in enumerate(rows):
        for fname in row:
            if fname in FORBIDDEN_FIELDS:
                viol.append(Violation(contract.name, i, fname, "verbotenes-feld",
                                      "knoten_id -> (kat_id, version) verwenden"))
        for t in required_time:
            if row.get(t) in (None, ""):
                viol.append(Violation(contract.name, i, t, "zeit-spalte-fehlt",
                                      f"Typ '{art}' verlangt '{t}'"))
        if "kat_id" in row and "version" not in row:
            viol.append(Violation(contract.name, i, "version", "id-version-paarung",
                                  "kat_id ohne version — Knoten = (kat_id, version)"))
        for f in contract.fields:
            if f.name in required_time:
                continue                      # Zeit-Pflichtspalten schon oben geprüft (zeit-spalte-fehlt)
            present = f.name in row
            v = row.get(f.name)
            # Pflichtfeld-Riegel (QS-Fable, das tragende Loch): ein nicht-nullable Feld MUSS präsent UND
            # nicht-leer sein — sonst war „kontrakt-valide" keine Vollständigkeits-Garantie. Leer = None
            # ODER "" (symmetrisch zu den Zeit-Pflichtspalten oben; ein leerer id/text-Wert ist kein Wert).
            # Ausnahme: `list`/`bool` — [] bzw. False sind gültige Nicht-Leer-Werte.
            leer = (not present) or v is None or (v == "" and f.kind not in ("list", "bool"))
            if not f.nullable and leer:
                viol.append(Violation(contract.name, i, f.name, "pflichtfeld-fehlt-oder-null",
                                      "nicht-nullable Feld fehlt oder ist leer (None/\"\", nullable=False)"))
                continue
            if not present or v is None:
                continue                      # nullable und leer -> ok
            if f.kind in ("ordinal", "status", "enum"):
                allowed = STATUS if f.kind == "status" else f.enum
                if _is_number(v):
                    viol.append(Violation(contract.name, i, f.name, "ordinal-als-zahl",
                                          f"geurteiltes/ordinales Feld trägt Zahl {v!r} (3.12)"))
                elif allowed is not None and v not in allowed:
                    viol.append(Violation(contract.name, i, f.name, "wert-nicht-im-vokabular",
                                          f"{v!r} nicht in {sorted(allowed)}"))
            elif f.kind == "gemessen":
                if not _is_number(v):
                    viol.append(Violation(contract.name, i, f.name, "gemessen-nicht-numerisch",
                                          f"gemessenes Feld trägt {type(v).__name__} {v!r}"))
    return viol
