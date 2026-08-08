"""
anbieter_registry.py — DATEN-Registry der Free-Tier-Anbieter + die Fähigkeits-Leiter.

Kein Kontroll-Fluss, nur beschreibende Daten (welcher Anbieter, welche Modelle, welche Familie,
welche Rollen) + reine Helfer, die daraus die Failover-Leiter ableiten. Der `ensemble_router`
liest hier — so ist ein Anbieter-/Modell-Wechsel eine Daten-Änderung, kein Code-Eingriff.

**Der „lokales LLM ohne Code-Änderung"-Gewinn:** Ollama-lokal ist genau EINE Zeile hier
(`protokoll=openai_compat`, `base_url=localhost:11434/v1`, `key_env=None`). Es erreicht denselben
`OpenAICompatLLM`-Adapter wie die Cloud-Anbieter. Umschalten Cloud↔lokal↔hybrid = die Env
`MTF_ENSEMBLE` — null Code. Bei Erreichbarkeit (`lokal_erreichbar()`) wird lokal automatisch
bevorzugt (Jens: „Arbeitsgaul lokal, Cloud nur Diversität/Schiedsrichter").

Rollen (design §Rollen-Zuordnung):
  - "initial"       : darf als abstimmende Stimme der Initialbewertung dienen.
  - "failover"      : darf eine ausgefallene Stimme ersetzen (Fähigkeits-Leiter).
  - "schiedsrichter": darf bei Uneinigkeit den Tiebreak sprechen (unbeteiligtes, starkes Modell).

Klasse = Fähigkeits-Rang (gross > mittel > klein) für das Leiter-Ranking. Live geprüft am 30.07.2026.
Nur Standardbibliothek.
"""
import os
import subprocess

# Fähigkeits-Rang (höher = fähiger) — Sortierschlüssel der Leiter.
KLASSE_RANG = {"gross": 3, "mittel": 2, "klein": 1}

# --- Anbieter (Transport) --------------------------------------------------- #
# lokal=True -> kein Key, Health-Check-fähig. base_url ohne abschließenden /.
ANBIETER = {
    "groq":       {"protokoll": "openai_compat", "base_url": "https://api.groq.com/openai/v1",
                   "key_env": "GROQ_API_KEY", "throttle_s": 2.0, "lokal": False},
    "sambanova":  {"protokoll": "openai_compat", "base_url": "https://api.sambanova.ai/v1",
                   "key_env": "SAMBA_NOVA_API_KEY", "throttle_s": 2.0, "lokal": False},
    "mistral":    {"protokoll": "openai_compat", "base_url": "https://api.mistral.ai/v1",
                   "key_env": "MISTRAL_API_KEY", "throttle_s": 1.2, "lokal": False},
    "openrouter": {"protokoll": "openai_compat", "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPEN_ROUTER_API_KEY", "throttle_s": 2.0, "lokal": False},
    "gemini":     {"protokoll": "gemini", "base_url": None,
                   "key_env": "GEMINI_API_KEY", "throttle_s": 1.5, "lokal": False},
    "ollama":     {"protokoll": "openai_compat",
                   "base_url": os.environ.get("MTF_LOKAL_URL", "http://127.0.0.1:11434/v1"),   # IPv4: kein ::1-Timeout (Windows)
                   "key_env": None, "throttle_s": 0.0, "lokal": True},
    # Cerebras: Key gültig, aber Inferenz 402 (Payment Required) am 30.07.2026 — kein Free-Kontingent.
    # Bewusst OHNE Modelle/Rollen gelistet (dokumentiert, nicht genutzt); reaktivieren, sobald frei.
    "cerebras":   {"protokoll": "openai_compat", "base_url": "https://api.cerebras.ai/v1",
                   "key_env": "CEREBRAS_API_KEY", "throttle_s": 2.0, "lokal": False,
                   "notiz": "402 Payment Required (30.07.2026) — kein Free-Inferenz-Kontingent"},
}

# --- Modelle (Fähigkeit + Rolle je Anbieter-Modell) ------------------------- #
# familie = Unabhängigkeits-Gruppe (gleiche Familie = korrelierte Fehler). Live geprüft.
MODELLE = [
    # Llama-Familie (Groq + SambaNova = DASSELBE Modell -> Failover-Redundanz, KEINE Zweitstimme)
    {"anbieter": "groq", "model": "llama-3.3-70b-versatile", "familie": "llama",
     "klasse": "gross", "rollen": ("initial", "failover")},
    {"anbieter": "groq", "model": "llama-3.1-8b-instant", "familie": "llama",
     "klasse": "klein", "rollen": ("failover",)},
    {"anbieter": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct", "familie": "llama",
     "klasse": "gross", "rollen": ("initial", "failover")},
    {"anbieter": "sambanova", "model": "Meta-Llama-3.1-8B-Instruct", "familie": "llama",
     "klasse": "klein", "rollen": ("failover",)},
    # Gemini-Familie
    {"anbieter": "gemini", "model": "gemini-flash", "familie": "gemini",
     "klasse": "gross", "rollen": ("initial", "failover", "schiedsrichter")},
    {"anbieter": "gemini", "model": "gemini-flash-lite", "familie": "gemini",
     "klasse": "mittel", "rollen": ("failover",)},
    # Mistral-Familie
    {"anbieter": "mistral", "model": "mistral-small-latest", "familie": "mistral",
     "klasse": "mittel", "rollen": ("initial", "failover")},
    {"anbieter": "mistral", "model": "ministral-8b-latest", "familie": "mistral",
     "klasse": "klein", "rollen": ("failover",)},
    # OpenRouter :free — NICHT-Llama, rate-limitiert -> Schiedsrichter (seltene Aufrufe passen zum Limit)
    {"anbieter": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "familie": "nemotron",
     "klasse": "gross", "rollen": ("schiedsrichter", "failover")},
    {"anbieter": "openrouter", "model": "openai/gpt-oss-20b:free", "familie": "gpt_oss",
     "klasse": "mittel", "rollen": ("schiedsrichter", "failover")},
    {"anbieter": "openrouter", "model": "google/gemma-4-31b-it:free", "familie": "gemma",
     "klasse": "mittel", "rollen": ("failover",)},
    # Lokal (Home): der Arbeitsgaul. Modell via MTF_OLLAMA_MODEL überschreibbar. Klasse `mittel` (ehrlich:
    # ein 30B-Lokalmodell liegt UNTER 70B/Frontier) -> die Cloud-`gross`-Modelle sind strikt höherwertig,
    # sodass die Pseudolabel-Korrektur (aufzeichnung.py) real feuern kann (Claude-QS B1). MTF_LOKAL_KLASSE
    # überschreibt (falls jemand ein stärkeres Lokalmodell fährt).
    {"anbieter": "ollama", "model": os.environ.get("MTF_OLLAMA_MODEL", "qwen3:30b"), "familie": "qwen",
     "klasse": os.environ.get("MTF_LOKAL_KLASSE", "mittel"), "rollen": ("initial", "failover")},
]

# --- Standard-Belegung der Rollen (Cloud-only-Profil; Env überschreibt) ------ #
# Initialbewertung = 3 heterogene Familien (unkorrelierte Fehler).
DEFAULT_ENSEMBLE = [
    ("groq", "llama-3.3-70b-versatile"),          # Llama-Stimme
    ("gemini", "gemini-flash"),                    # Gemini-Stimme
    ("mistral", "mistral-small-latest"),           # Mistral-Stimme
]
# Schiedsrichter = starkes, unbeteiligtes Modell mit seltenem Aufruf (passt zum engen Free-Limit).
DEFAULT_SCHIEDSRICHTER = ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")


def alle_modelle():
    """Nur Modelle, deren Anbieter Modelle+Rollen trägt (schließt cerebras/ohne-Rollen aus)."""
    return [m for m in MODELLE if m["anbieter"] in ANBIETER]


def finde_modell(anbieter, model):
    for m in MODELLE:
        if m["anbieter"] == anbieter and m["model"] == model:
            return m
    return None


def _basis_modellname(model):
    """Roh-Familienname eines Modells zum Erkennen von „gleiches Modell, anderer Anbieter".
    z. B. llama-3.3-70b-versatile / Meta-Llama-3.3-70B-Instruct -> 'llama-3.3-70b'."""
    s = model.lower().replace("meta-", "").replace("_", "-")
    import re
    m = re.search(r"(llama|mistral|ministral|gemma|nemotron|gemini|qwen|gpt-oss)[-\d.]*", s)
    return m.group(0).rstrip("-.") if m else s


def leiter(preferred, rolle="initial", ausgeschlossene_anbieter=()):
    """Fähigkeits-Leiter für einen Stimmen-Slot, ausgehend vom Wunsch-(anbieter,model).

    Reihenfolge (Jens-Refinement 30.07.: **Fähigkeit zuerst**, ein schlechteres Modell NUR als letzter
    Ausweg, wenn KEIN gleichwertiges — weder beim selben noch bei einem anderen Anbieter — verfügbar ist):
      0. exakt das Wunschmodell
      1. GLEICHWERTIG-oder-besser (Klasse ≥ Wunsch), egal welche Familie/Anbieter — darin: fähigstes zuerst,
         dann Tiebreak gleiches Modell → gleiche Familie → selber Anbieter (Kontinuität/Unabhängigkeit)
      2. SCHLECHTER (Klasse < Wunsch) — letzter Ausweg, darin am wenigsten degradiert zuerst
    Rollen-Eignung: `schiedsrichter` verlangt die Schiedsrichter-Rolle (ein degradiertes Fallback taugt nicht
    als Schiedsrichter); ein Stimmen-Slot nimmt Modelle mit der verlangten Rolle ODER `failover` (= als
    Notnagel nutzbar). `ausgeschlossene_anbieter` (z. B. tages-erschöpft) fallen raus.
    -> Liste [{anbieter, model, familie, klasse, rollen}, …]."""
    p_anb, p_mod = preferred
    p_eintrag = finde_modell(p_anb, p_mod)
    p_fam = p_eintrag["familie"] if p_eintrag else None
    p_basis = _basis_modellname(p_mod)
    p_rank = KLASSE_RANG.get(p_eintrag["klasse"], 2) if p_eintrag else 2

    def eignet(m):
        if m["anbieter"] in ausgeschlossene_anbieter:
            return False
        if rolle == "schiedsrichter":
            return "schiedsrichter" in m["rollen"]
        return rolle in m["rollen"] or "failover" in m["rollen"]   # Stimme: primär-Rolle ODER Notnagel

    def rang(m):
        c_rank = KLASSE_RANG.get(m["klasse"], 0)
        return (
            0 if (m["anbieter"] == p_anb and m["model"] == p_mod) else 1,   # exakt zuerst
            1 if c_rank < p_rank else 0,                # gleichwertig-oder-besser VOR schlechter (Jens)
            -c_rank,                                    # fähigstes zuerst (am wenigsten degradiert)
            0 if _basis_modellname(m["model"]) == p_basis else 1,          # Tiebreak: gleiches Modell
            0 if m["familie"] == p_fam else 1,          # gleiche Familie
            0 if m["anbieter"] == p_anb else 1,         # selber (bekannt-guter) Anbieter
            m["anbieter"], m["model"],
        )

    return sorted([m for m in alle_modelle() if eignet(m)], key=rang)


def lokal_erreichbar(timeout=1.5):
    """Health-Check des lokalen Ollama (OpenAI-kompatibler /models-Endpunkt). True -> lokal auto-bevorzugen."""
    base = ANBIETER["ollama"]["base_url"]
    try:
        out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), f"{base}/models"],
                             capture_output=True, text=True, timeout=timeout + 2)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def ensemble_aus_env():
    """Belegung des Initial-Ensembles aus $MTF_ENSEMBLE (kommagetrennt "anbieter:model" oder
    "anbieter"-Kurzform -> dessen erstes initial-fähiges Modell). Leer -> DEFAULT_ENSEMBLE, aber
    lokal wird vorangestellt, wenn erreichbar (Home-Hybrid: Arbeitsgaul zuerst)."""
    roh = os.environ.get("MTF_ENSEMBLE", "").strip()
    if roh:
        slots = []
        for tok in roh.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" in tok:
                a, m = tok.split(":", 1)
                slots.append((a.strip(), m.strip()))
            else:
                kand = [m for m in alle_modelle() if m["anbieter"] == tok and "initial" in m["rollen"]]
                if kand:
                    slots.append((kand[0]["anbieter"], kand[0]["model"]))
        return slots or list(DEFAULT_ENSEMBLE)
    slots = list(DEFAULT_ENSEMBLE)
    if lokal_erreichbar():
        lok = [m for m in alle_modelle() if m["anbieter"] == "ollama" and "initial" in m["rollen"]]
        if lok:
            slots = [(lok[0]["anbieter"], lok[0]["model"])] + slots
    return slots
