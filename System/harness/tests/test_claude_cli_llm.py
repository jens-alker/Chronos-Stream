"""
test_claude_cli_llm.py — Offline-Tests für den reinen Kern der Haiku-CLI-Bridge.

Netz/CLI-Aufruf ist nicht Teil des Tests (braucht Session-Auth); getestet wird der robuste
JSON-Parser (`_extrahiere_json`), die Vokabular-/Ordinal-Filterung + Fail-closed-Semantik von
`kategorisiere` (subprocess/CLI gemockt) und die Fehlerpfade von `_cli_call`. Konvention 3.12:
geschlossenes Vokabular, ordinale Stärke, keine Dezimalen. QS-Runde-3-Regressionsfälle (B3/B6)
mit POSITIV-Assertion — die erwartete Kategorie MUSS erhalten bleiben, `[]` wäre der Bug.
"""
import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import claude_cli_llm as m


# ---------------------------------------------------------------- #
# _extrahiere_json — Gutfälle
# ---------------------------------------------------------------- #
def test_extrahiere_json_plain():
    r = m._extrahiere_json('{"kategorien":[{"kategorie":"Kupfer","staerke":"stark"}]}')
    assert r == {"kategorien": [{"kategorie": "Kupfer", "staerke": "stark"}]}


def test_extrahiere_json_fenced():
    roh = '```json\n{"kategorien":[{"kategorie":"Kupfer","staerke":"mittel"}]}\n```'
    assert m._extrahiere_json(roh)["kategorien"][0]["kategorie"] == "Kupfer"


def test_extrahiere_json_prosa_umschlossen():
    roh = 'Gerne! Hier das Ergebnis:\n{"kategorien":[{"kategorie":"Lithium","staerke":"schwach"}]}\nPasst das?'
    assert m._extrahiere_json(roh)["kategorien"][0]["kategorie"] == "Lithium"


def test_extrahiere_json_leerer_text_ist_leere_liste():
    assert m._extrahiere_json("") == {"kategorien": []}
    assert m._extrahiere_json("   \n ") == {"kategorien": []}


def test_extrahiere_json_modell_sagt_legitim_leer():
    # gültiges JSON mit leerer Liste != Parse-Fehler: bleibt dict, wird NICHT zu None.
    assert m._extrahiere_json('{"kategorien":[]}') == {"kategorien": []}


def test_extrahiere_json_unparsebar_ist_None():
    # nicht-leer, aber kein kategorien-JSON -> None (fail-closed-Signal an den Aufrufer, B3).
    assert m._extrahiere_json("Ich bin mir nicht sicher, hier ist keine Struktur.") is None


# ---------------------------------------------------------------- #
# _extrahiere_json — QS-Runde-3-Regressionsfälle (B2 Gemini / B3 Claude)
# Vorher (gieriges \{.*"kategorien".*\}) lieferten A/B/C faelschlich [] -> hier POSITIV geprueft.
# ---------------------------------------------------------------- #
def test_regress_A_zwei_objekte_entwurf_und_final():
    # Entwurf ohne Schluessel + finale Antwort: das gierige Regex spannte ueber beide -> Fehler.
    roh = '{"entwurf":true}\n{"kategorien":[{"kategorie":"Kupfer","staerke":"stark"}]}'
    r = m._extrahiere_json(roh)
    assert r is not None and r["kategorien"][0]["kategorie"] == "Kupfer"


def test_regress_B_klammer_vor_dem_json():
    roh = 'Beispiel {"a":1}. Ergebnis: {"kategorien":[{"kategorie":"Lithium","staerke":"mittel"}]}'
    r = m._extrahiere_json(roh)
    assert r is not None and r["kategorien"][0]["kategorie"] == "Lithium"


def test_regress_C_klammer_notiz_hinter_gueltigem_json():
    # Der gefaehrlichste Fall: perfekt valide Extraktion, still verworfen.
    roh = '{"kategorien":[{"kategorie":"Halbleiter","staerke":"stark"}]} (Hinweis {"x":1})'
    r = m._extrahiere_json(roh)
    assert r is not None and r["kategorien"][0]["kategorie"] == "Halbleiter"


# ---------------------------------------------------------------- #
# kategorisiere — Filter/Dedup/Fail-closed/Casing (CLI gemockt)
# ---------------------------------------------------------------- #
def test_kategorisiere_filtert_und_dedupliziert(monkeypatch):
    antwort = ('{"kategorien":['
               '{"kategorie":"Transformatoren","staerke":"stark"},'
               '{"kategorie":"Transformatoren","staerke":"mittel"},'   # Duplikat -> raus
               '{"kategorie":"ErfundeneKat","staerke":"stark"},'       # nicht im Vokabular -> raus
               '{"kategorie":"Kupfer","staerke":"keine"},'            # keine -> raus
               '{"kategorie":"Kupfer","staerke":"0.9"}]}')            # Dezimal -> raus
    monkeypatch.setattr(m, "_cli_call", lambda *a, **k: antwort)
    r = m.ClaudeCliLLM().kategorisiere("egal")
    assert r == [("Transformatoren", "stark")]


def test_kategorisiere_casing_und_whitespace_normalisiert(monkeypatch):
    # B6: 'STARK'/' stark ' darf die Kategorie NICHT still verlieren.
    antwort = '{"kategorien":[{"kategorie":"Kupfer","staerke":" STARK "},{"kategorie":"Lithium","staerke":"Mittel"}]}'
    monkeypatch.setattr(m, "_cli_call", lambda *a, **k: antwort)
    r = m.ClaudeCliLLM().kategorisiere("egal")
    assert ("Kupfer", "stark") in r and ("Lithium", "mittel") in r


def test_kategorisiere_failclosed_bei_unparsebar(monkeypatch):
    # B3: unparsebare, nicht-leere Antwort -> Exception statt still-[] (kein emerging-Falsch-Positiv).
    monkeypatch.setattr(m, "_cli_call", lambda *a, **k: "Kann ich leider nicht als JSON liefern.")
    try:
        m.ClaudeCliLLM().kategorisiere("egal")
    except RuntimeError:
        return
    raise AssertionError("kategorisiere haette bei unparsebarer Antwort werfen muessen")


def test_kategorisiere_leere_liste_ist_kein_fehler(monkeypatch):
    # Modell sagt legitim 'keine Kategorie' -> [] ohne Exception.
    monkeypatch.setattr(m, "_cli_call", lambda *a, **k: '{"kategorien":[]}')
    assert m.ClaudeCliLLM().kategorisiere("egal") == []


# ---------------------------------------------------------------- #
# _cli_call — Fehlerpfade (subprocess gemockt)
# ---------------------------------------------------------------- #
class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_cli_call_erfolg(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, '{"is_error":false,"result":"OK"}'))
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    assert m._cli_call("p", "modell") == "OK"


def test_cli_call_is_error_wirft_nach_retries(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, '{"is_error":true,"error":"boom"}'))
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    try:
        m._cli_call("p", "modell", max_retries=2)
    except RuntimeError:
        return
    raise AssertionError("is_error haette werfen muessen")


def test_cli_call_returncode_wirft(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: _FakeProc(1, "", "cli kaputt"))
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    try:
        m._cli_call("p", "modell", max_retries=2)
    except RuntimeError:
        return
    raise AssertionError("returncode!=0 haette werfen muessen")


def test_cli_call_kein_json_wirft(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, "nicht json"))
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    try:
        m._cli_call("p", "modell", max_retries=2)
    except RuntimeError:
        return
    raise AssertionError("kein JSON haette werfen muessen")


# ---------------------------------------------------------------- #
# ensemble — n Stimmen, geteiltes Modell/Vokabular
# ---------------------------------------------------------------- #
def test_ensemble_n_stimmen():
    es = m.ensemble(n=3)
    assert len(es) == 3
    assert all(isinstance(e, m.ClaudeCliLLM) for e in es)
    assert len({e.voice for e in es}) == 3               # unterscheidbare voices
    assert all(e.model == es[0].model for e in es)       # gleiches Modell (Homogenitaet -> F105)


def _run_ohne_pytest():
    """Erlaubt `python3 test_claude_cli_llm.py` ohne pytest (wie die übrigen Suiten)."""
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    mp = _MP()
    n = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if "monkeypatch" in inspect.signature(fn).parameters:
            fn(mp)
        else:
            fn()
        n += 1
    print(f"test_claude_cli_llm: {n} ok")


if __name__ == "__main__":
    _run_ohne_pytest()
