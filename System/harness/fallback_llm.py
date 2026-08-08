"""
fallback_llm.py — Extraktions-Fallback Gemini(temp-0) → Claude Haiku (Jens, 22.07.).

Für den Modul-8-Datenlauf: Gemini-Free-Tier ist quota-gedrosselt. Regel (Jens): „Nimm Claude Haiku
per API, wenn Gemini ausgereizt ist." Dieser Adapter kapselt genau das — Drop-in wie die übrigen
LLM-Adapter (`kategorisiere(text) -> [(kat, staerke)]`, `ensemble(n)`).

Ablauf: Primär Gemini (temp-0, reproduzierbar). Wirft Gemini einen QUOTA-/Rate-Fehler, wird für den
REST des Laufs auf Claude Haiku umgeschaltet (nicht je Aufruf neu probiert — „Gemini ausgereizt" ist
ein Lauf-Zustand). Haiku-Backend:
  - `claude_llm` (Haiku über die Anthropic-API, temp-0) — WENN ein $ANTHROPIC_API_KEY vorliegt (echtes
    „per API", reproduzierbar), SONST
  - `claude_cli_llm` (Haiku über die `claude`-CLI, Session-Auth) — dasselbe Modell ohne Key.
Nicht-QUOTA-Fehler von Gemini werden NICHT geschluckt (fail-loud).

Reproduzierbarkeit (F105): Gemini + Claude-API sind temp-0; die CLI ist es nicht — für den Retro wird
die Extraktion daher gecacht-bei-Ingest (Re-Runs lesen den Cache, kein Modell-Vintage-Leak). Der Cache
liegt außerhalb dieses Adapters (Retro-Korpus-Ebene). `modell` trägt die Provenienz je Extraktion.
"""
import os


def _hat_anthropic_key():
    for k, v in os.environ.items():
        if k.lower() in ("anthropic_api_key",) and v:
            return True
    return bool(os.path.exists(os.path.expanduser("~/.config/mtf-qs/anthropic.key")))


def _baue_haiku(**kw):
    """Haiku-Backend: API (claude_llm) wenn Key da, sonst CLI (claude_cli_llm) — gleiches Modell."""
    if _hat_anthropic_key():
        from claude_llm import ClaudeLLM
        return ClaudeLLM(**kw)
    from claude_cli_llm import ClaudeCliLLM
    return ClaudeCliLLM(**{k: v for k, v in kw.items() if k != "key"})


class FallbackLLM:
    """Gemini(temp-0) -> Claude Haiku, sobald Gemini quota-ausgereizt ist. Schnittstelle wie MockLLM."""

    def __init__(self, gemini, haiku, zustand, voice="fallback"):
        self.gemini = gemini
        self.haiku = haiku
        self._z = zustand                       # geteilt über die Stimmen EINES Laufs: {"gemini_aus": bool}
        self.voice = voice

    @property
    def model(self):
        return f"{self.haiku.model}[fallback]" if self._z.get("gemini_aus") else self.gemini.model

    @staticmethod
    def _ist_quota(e):
        s = str(e).lower()
        return any(w in s for w in ("quota", "rate", "retries", "429", "resource_exhausted"))

    def kategorisiere(self, text):
        if not self._z.get("gemini_aus"):
            try:
                return self.gemini.kategorisiere(text)
            except Exception as e:                       # noqa: BLE001
                if not self._ist_quota(e):
                    raise                                # echter Fehler -> fail-loud, kein stiller Fallback
                self._z["gemini_aus"] = True             # Gemini ausgereizt -> Rest des Laufs über Haiku
        return self.haiku.kategorisiere(text)


def ensemble(n=1, **kw):
    """n Fallback-Stimmen mit GETEILTEM Umschalt-Zustand (Gemini-Erschöpfung gilt laufweit).
    Baut ein Gemini- und ein Haiku-Backend, die sich die Stimmen teilen."""
    from gemini_llm import GeminiLLM
    gem = GeminiLLM(max_retries=1, **kw)        # fail-fast: auf Quota sofort an Haiku, kein langer Backoff
    haiku = _baue_haiku()
    zustand = {"gemini_aus": False}
    return [FallbackLLM(gem, haiku, zustand, voice=f"fallback{i}") for i in range(n)]
