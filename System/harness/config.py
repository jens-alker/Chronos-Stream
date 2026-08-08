"""
config.py — Injektion von LLM und Store (Cloud-Fakes vs. lokaler Deploy).

Ein Ort, der entscheidet, WELCHE Implementierungen die Pipeline bekommt — gesteuert
über Umgebungsvariablen. So läuft **derselbe Code**:
  - in der Cloud (dieses Repo):   Mock-LLM + SQLite-Test-DB   (gratis, ephemer, phone-steuerbar)
  - beim lokalen Deploy (später): echtes Ollama-LLM + echte DB (nur ENV umstellen)

ENV:
  MTF_LLM      = mock (default) | gemini | claude | ollama | router
  MTF_ENSEMBLE = (nur MTF_LLM=router) kommagetrennt "anbieter:model" oder "anbieter" — überschreibt
                 die Default-Belegung; leer -> anbieter_registry.DEFAULT_ENSEMBLE (lokal auto-bevorzugt).
  MTF_NLI      = aus (default) | mdeberta   (V2 Relations-/Neuheits-NLI; home-gebunden, transformers)
  MTF_STORE    = mem  (default) | sqlite
  MTF_DB       = Pfad der SQLite-DB (default: System/harness/test.db)

Semantik-Kanal 2 (Frontier-Ensemble): `get_ensemble()` ist die EINE Quelle der Wahrheit für den
mehrstimmigen Extraktor (Router über die Free-Tier-Anbieter). `MTF_LLM=router` -> heterogenes
Ensemble + MIN-Konfidenz + Dritt-KI-Schiedsrichter + quota-bewusster Failover; jeder andere Wert ->
Einzel-LLM (Rückwärtskompat). Lokales Ollama wird ohne Code-Änderung eingehängt (nur MTF_ENSEMBLE
bzw. Erreichbarkeit) — Home-Hybrid: Arbeitsgaul lokal, Cloud Diversität/Schiedsrichter.

Die Embedding-Stimme (V1) wird auf PIPELINE-Ebene gebaut (sie braucht das `kategorie_version`-Vokabular,
das hier nicht vorliegt) — `embedding_llm.baue_embedding_stimme(kategorie_version_rows, schwellen=…)`.
Die `schwellen` sind PFLICHT (Fable-QS B2, fail-closed: der echte nomic braucht kalibrierte, verteilungs-
relative Cutoffs; live_lauf lädt sie gepinnt aus $MTF_EMBEDDING_SCHWELLEN, sonst wird die Stimme
übersprungen). Das heterogene Ensemble ist dann `[get_llm(), baue_embedding_stimme(...)]`; die NLI reicht
man an `make_inferenz(nli=…)`.

Nur Standardbibliothek (der Kern; die Home-Modelle Ollama/transformers werden lazy geladen).
"""
import os
from mock_llm import MockLLM
from store import MemStore, SqliteStore

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test.db")


def get_llm():
    which = os.environ.get("MTF_LLM", "mock").lower()
    if which == "mock":
        return MockLLM()
    if which == "gemini":
        # Echte FREIE Fremd-Stufe (Modul 2d): Gemini Free-Tier über den Proxy. 0 Cash
        # (Zielfunktion-konform); gleiche Schnittstelle wie MockLLM.
        from gemini_llm import GeminiLLM
        return GeminiLLM()
    if which == "claude":
        # Echte Extraktion über Claude Haiku (billigstes Modell). KOSTENPFLICHTIG — nur mit
        # hinterlegtem $ANTHROPIC_API_KEY (oder ~/.config/mtf-qs/anthropic.key). Von Jens autorisiert.
        from claude_llm import ClaudeLLM
        return ClaudeLLM()
    if which == "ollama":
        # Lokaler Deploy (Jens 07.08.): das echte lokale, freie Extraktions-LLM (Modul 2d) über den EINEN
        # OpenAI-kompatiblen Adapter + die Ollama-Registry-Zeile (KEINE INSEL — derselbe Adapter/dieselbe
        # base_url wie im Router). Modell via $MTF_OLLAMA_MODEL, Endpunkt via $MTF_LOKAL_URL. Kein Key (lokal).
        # Cloud-Container: Ollama läuft nicht auf :11434 -> der erste Aufruf wirft TransienterFehler (ehrlich),
        # aber die KONSTRUKTION ist code-frei möglich = „lokaler Deploy = nur ENV-Umstellung".
        from openai_compat_llm import OpenAICompatLLM
        import anbieter_registry as R
        cfg = R.ANBIETER["ollama"]
        model = os.environ.get("MTF_OLLAMA_MODEL", "qwen3:30b")
        return OpenAICompatLLM(anbieter="ollama", model=model, base_url=cfg["base_url"],
                               key_env=cfg["key_env"],
                               familie=(R.finde_modell("ollama", model) or {}).get("familie", "qwen"))
    raise ValueError(f"unbekanntes MTF_LLM={which!r}")


def get_ensemble(vokabular=None, modus="vorwaerts"):
    """EINE Quelle der Wahrheit für den Extraktions-Ensemble-Slot (Semantik-Kanal 2, keine Insel).

    -> dict {llms, aggregation, schiedsrichter, router}:
      - MTF_LLM=router: heterogenes Free-Tier-Ensemble (anbieter_registry), MIN-Konfidenz,
        Dritt-KI-Schiedsrichter, quota-/health-bewusster Failover; im Retro-Modus greift der
        Vorwärts/Retro-Riegel (Frontier gesperrt).
      - sonst: das Einzel-LLM aus get_llm() als eine Stimme, aggregation=max, kein Schiedsrichter
        (byte-identisch zum bisherigen Verhalten).

    `vokabular`: geschlossenes Kategorie-Vokabular für die Anbieter-Adapter (z. B. die kat_ids aus
    `kategorie_version`); None -> Adapter-Default. `modus`: vorwaerts|retro (Riegel)."""
    if os.environ.get("MTF_LLM", "mock").lower() == "router":
        from ensemble_router import EnsembleRouter
        r = EnsembleRouter(vokabular=vokabular, modus=modus)
        return {"llms": r.stimmen(), "aggregation": "min", "schiedsrichter": r.schlichte, "router": r}
    return {"llms": [get_llm()], "aggregation": "max", "schiedsrichter": None, "router": None}


def get_nli():
    """V2-NLI-Backend für den Relations-/Neuheits-Fallback. Default `aus` -> None (reiner Keyword-Pfad,
    byte-identisch; cloud-sicher). `MTF_NLI=mdeberta` lädt lokal `mDeBERTa-v3-xnli` (home, transformers)
    — im Container nicht verfügbar (bewusst NotImplemented ohne torch). An `make_inferenz(nli=…)` reichen."""
    which = os.environ.get("MTF_NLI", "aus").lower()
    if which in ("aus", "", "none"):
        return None
    if which == "mdeberta":
        raise NotImplementedError(
            "Die semantische NLI-Schicht ist nicht Teil der offenen Dateninfrastruktur.")
    raise ValueError(f"unbekanntes MTF_NLI={which!r}")


def get_store():
    which = os.environ.get("MTF_STORE", "mem").lower()
    if which == "mem":
        return MemStore()
    if which == "sqlite":
        return SqliteStore(os.environ.get("MTF_DB", DEFAULT_DB))
    raise ValueError(f"unbekanntes MTF_STORE={which!r}")
