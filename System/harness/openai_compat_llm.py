"""
openai_compat_llm.py — EIN generischer Extraktions-Adapter für alle OpenAI-kompatiblen Anbieter.

Groq, SambaNova, Mistral, OpenRouter **und das lokale Ollama** sprechen alle dasselbe Protokoll
(`POST {base_url}/chat/completions`, Bearer-Key, `choices[0].message.content`). Statt eines Adapters
je Anbieter (Insel-Wildwuchs) trägt EIN parametrisierter Adapter alle — der Unterschied ist reine
Konfiguration (base_url · key_env · model · familie), keine Code-Zweige. Genau das macht das lokale
LLM „ohne Code-Änderung einhängbar": Ollama-lokal ist derselbe Adapter mit `base_url=localhost:11434`
und ohne Key (siehe `anbieter_registry`).

Schnittstelle wie MockLLM/GeminiLLM (Drop-in für Modul 2):
  - `kategorisiere(text) -> list[(kategorie, staerke_ordinal)]`   (geschlossenes Vokabular, 3.12)
  - `.model`          Provenienz-String  "anbieter:modell"
  - `.voice`          Stimmen-Label
  - `.pin()`          Reproduzierbarkeits-Signatur (Modul-2 `_stimme_id` liest das)

Konventionen (3.12): ORDINALE Stärke (keine|schwach|mittel|stark), KEINE Dezimalkonfidenz;
strukturiertes JSON (`response_format=json_object`, temperature 0). HTTPS via `curl` durch den
vorkonfigurierten Proxy (CA schon gesetzt) — identisch zu gemini_llm/qs_extern.

Reasoning-Modell-tolerant: gpt-oss/Nemotron schreiben teils in `message.reasoning` und lassen
`content=None`/`finish_reason=length`. Der Parser scannt das erste JSON-Objekt aus content ODER
reasoning (raw_decode-Scan, kein gieriges Regex) und ist fail-closed (kein Treffer -> leere Liste
NUR wenn die Antwort sauber leer war; ein FEHLER wirft, damit der Router failovern kann).

Nur Standardbibliothek.
"""
import json
import os
import re
import subprocess
import tempfile
import time

_STAERKE = {"keine", "schwach", "mittel", "stark"}

# Geschlossenes Kategorie-Vokabular (Sektor/Technologie/Material) — 3.12; identisch zu gemini_llm,
# damit die Stimmen dasselbe Vokabular voten. Wird i. d. R. vom Aufrufer (kategorie_version) überschrieben.
DEFAULT_VOKABULAR = [
    "Transformatoren", "Stromnetz", "Kupfer", "Rechenzentrum", "Halbleiter",
    "Festkoerperbatterie", "Lithium", "Photovoltaik", "Windkraft", "Wasserstoff",
]


class QuotaFehler(RuntimeError):
    """Anbieter-Quota/Rate erschöpft (429 / 'quota' / 'rate' / 'resource_exhausted').
    Der Router schaltet daraufhin auf die nächste Sprosse der Fähigkeits-Leiter."""


class HarterFehler(RuntimeError):
    """Anbieter dauerhaft nicht nutzbar (401/402/403/400 — Auth/Payment/Bad-Request).
    Der Router deaktiviert den Anbieter (kein Retry)."""


class TransienterFehler(RuntimeError):
    """Vorübergehender Netz-/Server-Blip (Timeout/5xx/Verbindungsabbruch) — der Router darf
    beim selben Anbieter mit Backoff erneut versuchen."""


class EnthaltungsFehler(RuntimeError):
    """Eine ROUTER-Stimme konnte GAR NICHT antworten (Leiter erschöpft oder alle Kandidaten
    bereits von einer anderen Stimme dieses Dokuments belegt). UNTERSCHEIDBAR von einer leeren
    Antwort (Modell lief, fand keine Kategorie): `ensemble_extrakt` zählt eine Enthaltung NICHT
    als abgegebene Stimme (Claude-QS M7), damit degradierte Anbieter keine einige Zweitmeinung
    fälschlich in Uneinigkeit kippen."""


def env_case_tolerant(name):
    """Env-Var case-tolerant lesen: erst exakt, dann case-insensitiver Treffer. Jens' Keys kamen
    in abweichender Syntax (`Gemini_API_Key`, `OPEN_ROUTER_API_KEY`) — sonst bricht der Lauf still
    an der Groß-/Kleinschreibung ab (dieselbe Falle wie in gemini_llm/eodhd_prices)."""
    if os.environ.get(name):
        return os.environ[name]
    ziel = name.lower()
    for k, v in os.environ.items():
        if k.lower() == ziel and v:
            return v
    return None


def _klassifiziere_fehler(status, body):
    """HTTP-Status + Body -> Fehlerklasse (Quota|Hart|Transient). Eine Quelle der Wahrheit für den
    Failover-Router. Body wird mitgelesen, weil manche Anbieter 200 mit {'error':...} liefern."""
    s = (body or "").lower()
    quota_wort = any(w in s for w in ("quota", "rate limit", "rate_limit", "ratelimit",
                                      "resource_exhausted", "too many requests"))
    if status == 429 or quota_wort:
        return QuotaFehler
    if status in (400, 401, 402, 403, 404):
        # 400/404 = falsches Modell/Request bei DIESEM Anbieter -> für die Leiter wie „nicht nutzbar"
        return HarterFehler
    if status == 0 or status >= 500:
        return TransienterFehler
    return TransienterFehler


def _extrahiere_json_objekt(text, bevorzugt_schluessel=None):
    """Valides JSON-Objekt aus freiem Text ziehen (raw_decode-Scan, kein gieriges Regex). Robust
    gegen ```json-Fences, Prosa, Reasoning-Vorspann (claude_cli_llm-Muster). `bevorzugt_schluessel`
    (Claude-QS m1): bevorzugt das ERSTE Objekt, das diesen Schlüssel trägt (sonst zieht ein
    Reasoning-Vorspann-Objekt wie {"thought":…} die echte Antwort ab -> stille Leerextraktion).
    None wenn keins."""
    if not text:
        return None
    dec = json.JSONDecoder()
    erster = None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if bevorzugt_schluessel is None or bevorzugt_schluessel in obj:
                return obj
            if erster is None:
                erster = obj
    return erster


class OpenAICompatLLM:
    """Ein OpenAI-kompatibler freier Extraktor (Groq/SambaNova/Mistral/OpenRouter/Ollama-lokal).

    Wirft bei Fehlern die typisierten QuotaFehler/HarterFehler/TransienterFehler, damit der
    ensemble_router die Fähigkeits-Leiter deterministisch abarbeiten kann. Ein direkter Einsatz
    (ohne Router) ist möglich — dann muss der Aufrufer die Fehler behandeln."""

    def __init__(self, *, anbieter, model, base_url, key_env=None, familie=None,
                 vokabular=None, voice=None, throttle_s=1.0, timeout=90, max_tokens=1024,
                 key=None):
        self.anbieter = anbieter
        self.model = f"{anbieter}:{model}"       # Provenienz-String (kollisionsfrei über Anbieter)
        self._model_raw = model
        self.base_url = base_url.rstrip("/")
        self.familie = familie or anbieter
        self.vokabular = vokabular or DEFAULT_VOKABULAR
        self.voice = voice or f"{anbieter}:{model}"
        self.throttle_s = throttle_s
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.key = key or (env_case_tolerant(key_env) if key_env else None)
        self.key_env = key_env
        self._letzter_aufruf = 0.0

    def pin(self):
        """Reproduzierbarkeits-Signatur: Anbieter:Modell (die ECHTE Identität dieser Stimme erreicht
        so `fact_kategorie.modell_vintage`, auch nach einem Failover-Wechsel)."""
        return self.model

    @property
    def hat_key(self):
        return bool(self.key) or self.key_env is None    # lokal (key_env None) braucht keinen Key

    def _prompt(self, text):
        vok = ", ".join(self.vokabular)
        return (
            "Du bist ein präziser Extraktor. Ordne das folgende Dokument den zutreffenden "
            "Kategorien aus DIESER geschlossenen Liste zu (nur diese, keine erfundenen):\n"
            f"[{vok}]\n\n"
            "Für JEDE zutreffende Kategorie gib eine ordinale Stärke: keine|schwach|mittel|stark "
            "(wie zentral ist die Kategorie im Dokument). KEINE Dezimalzahlen. Passt keine "
            "Kategorie, gib eine leere Liste. Antworte AUSSCHLIESSLICH als JSON-Objekt der Form "
            '{"kategorien":[{"kategorie":"...","staerke":"..."}]}.\n\n'
            f"DOKUMENT:\n{text}"
        )

    def _body(self, text):
        return {
            "model": self._model_raw,
            "messages": [{"role": "user", "content": self._prompt(text)}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

    def _curl(self, body):
        """POST an {base_url}/chat/completions durch den Proxy. Gibt (status, text) zurück;
        rc!=0 -> status 0 (transient). Kein Schlucken echter Fehler."""
        url = f"{self.base_url}/chat/completions"
        cmd = ["curl", "-sS", "--max-time", str(self.timeout),
               "-w", "\n__HTTP__%{http_code}", "-X", "POST", url,
               "-H", "Content-Type: application/json"]
        if self.key:
            cmd += ["-H", f"Authorization: Bearer {self.key}"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(body, tmp)
            tmp.close()
            cmd += ["--data", "@" + tmp.name]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 15)
        finally:
            os.unlink(tmp.name)
        if out.returncode != 0:
            return 0, out.stderr[:300]            # Netz/Timeout -> transient
        raw = out.stdout
        m = re.search(r"__HTTP__(\d+)\s*$", raw)
        status = int(m.group(1)) if m else 0
        text = raw[:m.start()] if m else raw
        return status, text

    def kategorisiere(self, text):
        """text -> list[(kategorie, staerke)] aus dem geschlossenen Vokabular (dedupliziert).
        Wirft typisierte Fehler (Quota/Hart/Transient) für den Router."""
        wart = self.throttle_s - (time.time() - self._letzter_aufruf)
        if wart > 0:
            time.sleep(wart)
        self._letzter_aufruf = time.time()

        status, roh = self._curl(self._body(text))
        if status != 200:
            raise _klassifiziere_fehler(status, roh)(
                f"{self.model} HTTP={status}: {roh[:200]}")

        try:
            data = json.loads(roh)
        except json.JSONDecodeError:
            raise TransienterFehler(f"{self.model}: unparsebare Antwort: {roh[:200]}")
        if isinstance(data, dict) and data.get("error"):
            # 200 mit Fehler-Objekt (OpenRouter-Muster) -> den NUMERISCHEN Code aus dem Body ziehen
            # (Claude-QS M2: sonst rutscht {"error":{"code":402}} als status=0 -> transient = Endlos-Retry).
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = json.dumps(err)
            raise _klassifiziere_fehler(code if isinstance(code, int) else 0, msg)(
                f"{self.model}: {msg[:200]}")

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise TransienterFehler(f"{self.model}: keine choices: {roh[:200]}")
        # Claude-QS m2: JSON kann in content ODER reasoning stehen (Reasoning-Modelle) — beide scannen.
        parsed = None
        for feld in (msg.get("content"), msg.get("reasoning")):
            parsed = _extrahiere_json_objekt(feld, bevorzugt_schluessel="kategorien")
            if parsed is not None and "kategorien" in parsed:
                break
        if parsed is None:
            finish = (data["choices"][0].get("finish_reason") or "")
            if finish == "length":
                # Reasoning-Modell hat vor dem JSON die Tokens verbraucht -> transient (mehr Tokens/Retry)
                raise TransienterFehler(f"{self.model}: JSON abgeschnitten (finish=length)")
            inhalt = (msg.get("content") or "") + (msg.get("reasoning") or "")
            if inhalt.strip():
                raise TransienterFehler(f"{self.model}: kein JSON-Objekt in Antwort: {inhalt[:150]}")
            return []                              # sauber leere Antwort = keine Kategorie
        return self._normalisiere(parsed)

    def _normalisiere(self, parsed):
        seen, out = set(), []
        for eintrag in parsed.get("kategorien", []) if isinstance(parsed, dict) else []:
            if not isinstance(eintrag, dict):
                continue
            kat = eintrag.get("kategorie")
            staerke = (eintrag.get("staerke") or "").lower()      # casing-robust (QS-B6-Muster)
            if kat in self.vokabular and staerke in _STAERKE and staerke != "keine" and kat not in seen:
                seen.add(kat)
                out.append((kat, staerke))
        return out
