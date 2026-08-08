"""
gemini_llm.py — echte, FREIE Extraktions-Stufe (Gemini) für den Harness.

Die freie Fremd-Stufe aus Modul 2d ("freies Ensemble: lokal + Gemini-Free"). Drop-in-Ersatz
für MockLLM: gleiche Schnittstelle `kategorisiere(text) -> list[(kategorie, staerke)]`, sodass
Modul 2 (make_klassifikation) ohne Codeänderung darauf läuft (nur MTF_LLM=gemini).

Konventionen gewahrt (3.12): geschlossenes Kategorie-Vokabular, ORDINALE Stärke
(keine|schwach|mittel|stark) — KEINE Dezimalkonfidenz. Strukturiertes JSON (responseSchema),
temperature 0 (reproduzierbar-nah). HTTPS läuft über `curl` durch den vorkonfigurierten Proxy
(CA bereits gesetzt), genau wie in qs_extern.py. Key: --  $GEMINI_API_KEY  ->  ~/.config/mtf-qs/gemini.key.

Kosten: Gemini Free-Tier (flash-lite bevorzugt) — 0 Cash (Zielfunktion-konform). Nur Standardbibliothek.
"""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

_BASE = "https://generativelanguage.googleapis.com/v1beta"
_KEYFILE = os.path.expanduser("~/.config/mtf-qs/gemini.key")
_STAERKE = {"keine", "schwach", "mittel", "stark"}
_MIN_INTERVALL = 1.5      # s Drosselung zwischen Aufrufen (Wall-Clock ist frei, Zielfunktion)
_letzter_aufruf = [0.0]   # modulweit: Zeitpunkt des letzten Calls

# Geschlossenes Kategorie-Vokabular (Sektor/Technologie/Material) — 3.12; erweiterbar.
DEFAULT_VOKABULAR = [
    "Transformatoren", "Stromnetz", "Kupfer", "Rechenzentrum", "Halbleiter",
    "Festkoerperbatterie", "Lithium", "Photovoltaik", "Windkraft", "Wasserstoff",
]

_SCHEMA = {
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
}


def _env_case_tolerant(name):
    """Env-Var case-tolerant lesen: erst exakt, dann case-insensitiver Treffer. Container-Secrets
    kommen mal als GEMINI_API_KEY, mal als Gemini_API_Key — sonst bricht der Lauf still an der
    Groß-/Kleinschreibung ab (dieselbe Falle wie in qs_extern.py)."""
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
    env = _env_case_tolerant("GEMINI_API_KEY")
    if env:
        return env.strip()
    if os.path.exists(_KEYFILE):
        return Path(_KEYFILE).read_text().strip()
    raise RuntimeError(f"Kein Gemini-Key ($GEMINI_API_KEY / Gemini_API_Key oder {_KEYFILE}).")


def _curl_json(url, method="GET", body=None, timeout=90):
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", method, url]
    tmp = None
    if body is not None:
        cmd += ["-H", "Content-Type: application/json"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, tmp)
        tmp.close()
        cmd += ["--data", "@" + tmp.name]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    finally:
        if tmp:
            os.unlink(tmp.name)
    if out.returncode != 0:
        raise RuntimeError(f"curl rc={out.returncode}: {out.stderr[:300]}")
    return json.loads(out.stdout)


def _generate(url, body, max_retries=6):
    """POST generateContent mit Drosselung + Retry-mit-Backoff auf Quota/Rate (Free-Tier).
    Respektiert den server-seitigen 'retry in Xs'-Hinweis. Wall-Clock ist frei (Zielfunktion)."""
    for versuch in range(max_retries):
        wartezeit = _MIN_INTERVALL - (time.time() - _letzter_aufruf[0])
        if wartezeit > 0:
            time.sleep(wartezeit)
        _letzter_aufruf[0] = time.time()
        data = _curl_json(url, method="POST", body=body)
        err = data.get("error")
        if not err:
            return data
        msg = str(err.get("message", ""))
        if err.get("code") == 429 or "quota" in msg.lower() or "rate" in msg.lower():
            if versuch >= max_retries - 1:
                raise RuntimeError("generateContent: quota exhausted")   # letzter Versuch: NICHT schlafen (fail-fast, z.B. für Fallback)
            m = re.search(r"retry in ([\d.]+)\s*s", msg)
            delay = min((float(m.group(1)) + 2) if m else 20.0, 65.0)
            time.sleep(delay)                 # Free-Tier-Fenster abwarten, dann erneut
            continue
        raise RuntimeError(f"generateContent: {err.get('message', err)}")
    raise RuntimeError("generateContent: Quota nach mehreren Retries nicht frei.")


def waehle_modell(key):
    """Free-Tier-freundliches gemini-flash(-lite)-Modell (wie qs_extern.py)."""
    data = _curl_json(f"{_BASE}/models?key={key}")
    if "error" in data:
        raise RuntimeError(f"Modell-Abruf: {data['error'].get('message', data['error'])}")
    kand = []
    for m in data.get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []) or not name.startswith("gemini"):
            continue
        if any(s in name for s in ("vision", "embedding", "aqa", "-tuning", "image", "tts")):
            continue
        score = 100 if ("flash" in name and "lite" not in name) else 70 if "flash" in name else 40 if "pro" in name else 0
        mm = re.search(r"\d+(?:\.\d+)?", name)
        score += float(mm.group()) * 5 if mm else 0
        score += 3 if ("exp" not in name and "preview" not in name) else 0
        kand.append((score, name))
    if not kand:
        raise RuntimeError("kein generateContent-fähiges gemini-Modell gefunden.")
    kand.sort(reverse=True)
    return kand[0][1]


class GeminiLLM:
    """Echter freier Extraktor/Kategorisierer (Gemini Free-Tier). Schnittstelle wie MockLLM."""

    def __init__(self, key=None, model=None, vokabular=None, voice="frei", max_retries=6):
        self.key = _finde_key(key)
        self.model = model or waehle_modell(self.key)
        self.vokabular = vokabular or DEFAULT_VOKABULAR
        self.voice = voice
        self.max_retries = max_retries          # =1 -> fail-fast auf Quota (für den Gemini->Haiku-Fallback)

    def _prompt(self, text):
        vok = ", ".join(self.vokabular)
        return (
            "Du bist ein präziser Extraktor. Ordne das folgende Dokument den zutreffenden "
            "Kategorien aus DIESER geschlossenen Liste zu (nur diese, keine erfundenen):\n"
            f"[{vok}]\n\n"
            "Für JEDE zutreffende Kategorie gib eine ordinale Stärke: keine|schwach|mittel|stark "
            "(wie zentral ist die Kategorie im Dokument). KEINE Dezimalzahlen. Passt keine "
            "Kategorie, gib eine leere Liste. Antworte ausschließlich im vorgegebenen JSON.\n\n"
            f"DOKUMENT:\n{text}"
        )

    def kategorisiere(self, text):
        """text -> list[(kategorie, staerke_ordinal)] aus dem geschlossenen Vokabular (dedupliziert)."""
        body = {
            "contents": [{"parts": [{"text": self._prompt(text)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": _SCHEMA, "temperature": 0.0},
        }
        url = f"{_BASE}/models/{self.model}:generateContent?key={self.key}"
        data = _generate(url, body, self.max_retries)   # gedrosselt + Retry auf Free-Tier-Quota
        try:
            roh = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(roh)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"unerwartete Gemini-Antwort ({e}): {str(data)[:300]}")
        seen, out = set(), []
        for eintrag in parsed.get("kategorien", []):
            kat = eintrag.get("kategorie")
            staerke = eintrag.get("staerke")
            if kat in self.vokabular and staerke in _STAERKE and staerke != "keine" and kat not in seen:
                seen.add(kat)
                out.append((kat, staerke))
        return out


def ensemble(n=2, **kw):
    """Freies Self-Consistency-Ensemble (n Gemini-Stimmen) für Modul 2d. Die Übereinstimmung ist
    ein *technisches* Extraktions-Gütesignal (`stimmen`) — sie hebt NICHT den Evidenz-Status auf
    `beobachtet` (F105; der steigt nur über 3.14). Teilt eine Modell-/Key-Auswahl."""
    basis = GeminiLLM(**kw)
    return [GeminiLLM(key=basis.key, model=basis.model, vokabular=basis.vokabular,
                      voice=f"frei{i}") for i in range(n)]
