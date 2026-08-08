"""
claude_llm.py — echte Extraktions-Stufe über die Anthropic-API (Claude Haiku, billigstes Modell).

Drop-in für MockLLM/GeminiLLM: gleiche Schnittstelle `kategorisiere(text) -> list[(kategorie,
staerke)]`, sodass Modul 2 (make_klassifikation) ohne Codeänderung darauf läuft (MTF_LLM=claude).

Konventionen gewahrt (3.12): geschlossenes Kategorie-Vokabular, ORDINALE Stärke
(keine|schwach|mittel|stark) — KEINE Dezimalkonfidenz. Strukturierte Ausgabe über ERZWUNGENEN
Tool-Use (tool_choice) statt Freitext-Parsing. HTTPS über `curl` durch den vorkonfigurierten
Proxy (wie gemini_llm.py / qs_extern.py).

**Kostenpflichtig** (Anthropic-Guthaben) — nur mit explizit hinterlegtem Key. Key-Suche:
  $ANTHROPIC_API_KEY  ->  ~/.config/mtf-qs/anthropic.key
Modell-Default: claude-haiku-4-5-20251001 (billigstes Claude). Nur Standardbibliothek.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

_URL = "https://api.anthropic.com/v1/messages"
_KEYFILE = os.path.expanduser("~/.config/mtf-qs/anthropic.key")
_VERSION = "2023-06-01"
_MODELL_DEFAULT = "claude-haiku-4-5-20251001"     # billigstes Claude-Modell
_STAERKE = {"keine", "schwach", "mittel", "stark"}
_MIN_INTERVALL = 0.3

from gemini_llm import DEFAULT_VOKABULAR          # gemeinsames geschlossenes Vokabular

_letzter_aufruf = [0.0]

_TOOL = {
    "name": "kategorisieren",
    "description": "Ordnet ein Dokument den zutreffenden Kategorien mit ordinaler Stärke zu.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kategorien": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kategorie": {"type": "string"},
                        "staerke": {"type": "string", "enum": ["keine", "schwach", "mittel", "stark"]},
                    },
                    "required": ["kategorie", "staerke"],
                },
            }
        },
        "required": ["kategorien"],
    },
}


def _env_case_tolerant(name):
    """Env-Var case-tolerant lesen: erst exakt, dann case-insensitiver Treffer. Container-Secrets
    kommen mal als ANTHROPIC_API_KEY, mal als Anthropic_API_Key (wie der Gemini-Key als
    Gemini_API_Key) — sonst bricht der Lauf still an der Groß-/Kleinschreibung ab."""
    if os.environ.get(name):
        return os.environ[name]
    ziel = name.lower()
    for k, v in os.environ.items():
        if k.lower() == ziel and v:
            return v
    return None


def _finde_key(key=None):
    if key:
        return key.strip()
    env = _env_case_tolerant("ANTHROPIC_API_KEY")
    if env:
        return env.strip()
    if os.path.exists(_KEYFILE):
        return Path(_KEYFILE).read_text().strip()
    raise RuntimeError(
        "Kein Anthropic-Key. Hinterlege $ANTHROPIC_API_KEY / Anthropic_API_Key (Env-Secret der "
        f"Umgebung, empfohlen) oder lege {_KEYFILE} an (chmod 600).")


def _messages(key, body, max_retries=6):
    """POST /v1/messages mit Drosselung + Retry auf 429/Overload (respektiert retry-after)."""
    for _ in range(max_retries):
        wart = _MIN_INTERVALL - (time.time() - _letzter_aufruf[0])
        if wart > 0:
            time.sleep(wart)
        _letzter_aufruf[0] = time.time()
        cmd = ["curl", "-sS", "--max-time", "90", "-X", "POST", _URL,
               "-H", f"x-api-key: {key}", "-H", f"anthropic-version: {_VERSION}",
               "-H", "content-type: application/json"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, tmp); tmp.close()
        cmd += ["--data", "@" + tmp.name]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=100)
        finally:
            os.unlink(tmp.name)
        if out.returncode != 0:
            raise RuntimeError(f"curl rc={out.returncode}: {out.stderr[:300]}")
        data = json.loads(out.stdout)
        err = data.get("error") or (data if data.get("type") == "error" else None)
        if err:
            typ = (err.get("error") or err).get("type", "")
            if typ in ("rate_limit_error", "overloaded_error"):
                time.sleep(20)
                continue
            raise RuntimeError(f"Anthropic: {(err.get('error') or err).get('message', err)}")
        return data
    raise RuntimeError("Anthropic: Rate-Limit nach mehreren Retries.")


class ClaudeLLM:
    """Echter Extraktor/Kategorisierer über Claude Haiku. Schnittstelle wie MockLLM/GeminiLLM."""

    def __init__(self, key=None, model=None, vokabular=None, voice="claude"):
        self.key = _finde_key(key)
        self.model = model or os.environ.get("MTF_CLAUDE_MODELL", _MODELL_DEFAULT)
        self.vokabular = vokabular or DEFAULT_VOKABULAR
        self.voice = voice

    def _prompt(self, text):
        vok = ", ".join(self.vokabular)
        return (
            "Ordne das folgende Dokument den zutreffenden Kategorien aus DIESER geschlossenen "
            f"Liste zu (nur diese, keine erfundenen): [{vok}]. Für jede zutreffende Kategorie eine "
            "ordinale Stärke (keine|schwach|mittel|stark), wie zentral sie im Dokument ist. KEINE "
            "Dezimalzahlen. Passt keine, leere Liste. Nutze das Werkzeug 'kategorisieren'.\n\n"
            f"DOKUMENT:\n{text}"
        )

    def kategorisiere(self, text):
        """text -> list[(kategorie, staerke_ordinal)] aus dem geschlossenen Vokabular (dedupliziert)."""
        body = {
            "model": self.model, "max_tokens": 1024, "temperature": 0.0,
            "tools": [_TOOL], "tool_choice": {"type": "tool", "name": "kategorisieren"},
            "messages": [{"role": "user", "content": self._prompt(text)}],
        }
        data = _messages(self.key, body)
        eintraege = []
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "kategorisieren":
                eintraege = block.get("input", {}).get("kategorien", [])
                break
        seen, out = set(), []
        for e in eintraege:
            kat, staerke = e.get("kategorie"), e.get("staerke")
            if kat in self.vokabular and staerke in _STAERKE and staerke != "keine" and kat not in seen:
                seen.add(kat)
                out.append((kat, staerke))
        return out


def ensemble(n=2, **kw):
    """Self-Consistency-Ensemble (n Claude-Stimmen) für Modul 2d. Die Übereinstimmung ist ein
    *technisches* Extraktions-Gütesignal (`stimmen`) — sie hebt NICHT den Evidenz-Status auf
    `beobachtet` (F105; der steigt nur über 3.14)."""
    basis = ClaudeLLM(**kw)
    return [ClaudeLLM(key=basis.key, model=basis.model, vokabular=basis.vokabular,
                      voice=f"claude{i}") for i in range(n)]
