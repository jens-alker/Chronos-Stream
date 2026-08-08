"""
schluessel.py — EINE Quelle für alle API-Keys.

Problemlage (vor diesem Modul): der Scraper (Modul 1) las seine Keys aus
`config.txt` (`*_api_key_file`-Pfade), die Konnektoren/das Frontier-Ensemble
(Groq/SambaNova/Mistral/OpenRouter/Gemini/Cerebras), EODHD, HuggingFace und
Google Drive dagegen aus UMGEBUNGSVARIABLEN. Zwei Mechanismen, zwei Orte.

Dieses Modul macht `config.txt` zur EINZIGEN Quelle: es liest die dort
hinterlegten Keys (inline ODER als Dateipfad) und stellt sie unter dem exakten
Namen bereit, den die jeweilige Komponente erwartet. Zwei Nutzungen, dieselbe
Abbildung + dasselbe Parsing (keine zweite Definition):

  1. IN-PROCESS (der Regelfall): `lade_ins_environ(cfg)` in `scraper.py:main()`
     setzt die Env-Vars im laufenden Prozess -> das In-Process-Ensemble sieht sie.
  2. OS-WEIT (für separat gestartete Backtests): `python schluessel.py --setx`
     schreibt sie via `setx` dauerhaft in die Windows-Benutzerumgebung.

Sicherheit: bereits gesetzte Env-Vars werden NICHT überschrieben (`overwrite=False`)
— eine echte OS-Umgebungsvariable schlägt die Datei. Fehlende Dateien werden still
übersprungen (fail-soft): ein fehlender Optional-Key darf den Start nie kippen.

Die Env-Namen der Ensemble-Anbieter sind die `key_env`-Felder aus
`harness/anbieter_registry.py` — diese Tabelle spiegelt sie bewusst explizit
(stabile, anbieter-vorgegebene Konstanten wie GROQ_API_KEY); ein Test erzwingt die
Deckungsgleichheit, damit die beiden nie auseinanderlaufen.
"""
from __future__ import annotations

import os
import subprocess
import sys

# ENV-Name  ->  config.txt-Basisname (der Wert kommt aus `<basis>` inline ODER
# aus der Datei unter `<basis>_file`).
ENV_MAP = {
    # --- Frontier-Ensemble (Semantik-Kanal 2) ---------------------------------
    "GROQ_API_KEY": "groq_api_key",
    "SAMBA_NOVA_API_KEY": "samba_nova_api_key",
    "MISTRAL_API_KEY": "mistral_api_key",
    "OPEN_ROUTER_API_KEY": "open_router_api_key",
    "GEMINI_API_KEY": "gemini_api_key",
    "CEREBRAS_API_KEY": "cerebras_api_key",
    "COHERE_API_KEY": "cohere_api_key",          # derzeit ungenutzt, aber einheitlich
    # --- Frontier-Urteil (Anthropic, optional) --------------------------------
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    # --- Embeddings / Semantik ------------------------------------------------
    "HUGGING_FACE_API_KEY": "hugging_face_api_key",
    # --- Finanzdaten (Backtests) ----------------------------------------------
    "EODHD_API_KEY": "eod_api_key",              # Env heißt EODHD_, config-Basis eod_
    # --- Patente (EPO OPS) ----------------------------------------------------
    "EPO_API_CONSUMER_KEY": "epo_consumer_key",
    "EPO_API_CONSUMER_SECRET_KEY": "epo_consumer_secret",
    # --- Volltext-Aggregator (optional) ---------------------------------------
    "CORE_API_KEY": "core_api_key",
    # --- Google Drive (Sync, optional) ----------------------------------------
    "GOOGLE_OAUTH_CLIENT_ID": "google_oauth_client_id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "google_oauth_client_secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "google_oauth_refresh_token",
}


def _read_key_file(pfad):
    """Erste nicht-leere Zeile einer Key-Datei (BOM/Whitespace/`<>`-Paste-Artefakt
    tolerant). None bei Fehler/leer — fail-soft."""
    try:
        with open(pfad, "r", encoding="utf-8-sig") as f:
            for zeile in f:
                s = zeile.strip().strip("<>").strip()
                if s:
                    return s
    except Exception:
        return None
    return None


def lade_config(pfad):
    """`config.txt` (schluessel = wert) -> dict mit kleingeschriebenen Schlüsseln.
    Spiegelt das Parsing aus scraper.py (Kommentare mit #, erstes '=' trennt)."""
    cfg = {}
    try:
        with open(pfad, "r", encoding="utf-8-sig") as f:
            for zeile in f:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue
                k, v = zeile.split("=", 1)
                cfg[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return cfg


def _cfg_getter(cfg):
    """Akzeptiert entweder ein dict ODER eine cfg(key, default)-Funktion (scraper)."""
    if callable(cfg):
        return lambda k: cfg(k)
    d = cfg or {}
    return lambda k: (d.get(k) or None)


def aufloesen(cfg):
    """{ENV_NAME: wert} für alle Keys, die in `cfg` auffindbar sind (inline oder
    über `<basis>_file`). Fehlende werden ausgelassen. `cfg` = dict ODER Getter."""
    get = _cfg_getter(cfg)
    out = {}
    for env, basis in ENV_MAP.items():
        wert = get(basis)                          # inline: <basis> = KEY
        if not wert:
            pfad = get(basis + "_file")            # oder Datei: <basis>_file = ...pfad
            if pfad:
                wert = _read_key_file(pfad)
        if wert:
            out[env] = wert
    return out


def lade_ins_environ(cfg=None, config_pfad=None, overwrite=False):
    """Setzt die aufgelösten Keys als Umgebungsvariablen IM LAUFENDEN PROZESS.
    `cfg` = dict/Getter (bevorzugt), sonst wird `config_pfad` geparst. Bereits
    gesetzte Vars bleiben unangetastet (overwrite=False: OS schlägt Datei).
    -> Liste der gesetzten ENV-Namen. Fail-soft (wirft nie)."""
    try:
        if cfg is None and config_pfad:
            cfg = lade_config(config_pfad)
        gesetzt = []
        for env, wert in aufloesen(cfg).items():
            if overwrite or not os.environ.get(env):
                os.environ[env] = wert
                gesetzt.append(env)
        return gesetzt
    except Exception:
        return []


def _setx(cfg):
    """OS-weit (Windows): jede aufgelöste Var dauerhaft via setx schreiben.
    Gilt erst in NEUEN Prozessen. -> (gesetzt, fehlend)."""
    werte = aufloesen(cfg)
    gesetzt = []
    for env, wert in werte.items():
        try:
            subprocess.run(["setx", env, wert], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            gesetzt.append(env)
        except Exception as e:
            print(f"  [Fehler] {env}: {str(e)[:60]}")
    fehlend = [e for e in ENV_MAP if e not in werte]
    return gesetzt, fehlend


def finde_config():
    """config.txt an den üblichen Stellen suchen — deckungsgleich mit scraper.py:
    System/Config, System/, sowie eine Ebene höher auf der Projektwurzel
    (<Repo>/Config, <Repo>/). Erste vorhandene gewinnt; sonst System/Config-Default."""
    system = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../System
    root = os.path.dirname(system)                                         # Projektwurzel
    kandidaten = (os.path.join(system, "Config", "config.txt"),
                  os.path.join(system, "config.txt"),
                  os.path.join(root, "Config", "config.txt"),
                  os.path.join(root, "config.txt"))
    for p in kandidaten:
        if os.path.exists(p):
            return p
    return kandidaten[0]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    pfad = finde_config()
    for a in argv:
        if a.startswith("--config="):
            pfad = a.split("=", 1)[1]
    cfg = lade_config(pfad)
    if not cfg:
        print(f"Keine config.txt gefunden/leer: {pfad}")
        return 1
    werte = aufloesen(cfg)
    print(f"config.txt: {pfad}")
    print(f"Gefundene Keys ({len(werte)}): " + ", ".join(sorted(werte)) or "—")
    fehlend = [e for e in ENV_MAP if e not in werte]
    if fehlend:
        print("Nicht gesetzt (optional): " + ", ".join(sorted(fehlend)))
    if "--setx" in argv:
        if not sys.platform.startswith("win"):
            print("--setx ist nur unter Windows sinnvoll.")
            return 1
        gesetzt, _ = _setx(cfg)
        print(f"\nDauerhaft gesetzt (setx, gilt ab NEUEM Fenster): {len(gesetzt)} Vars.")
    else:
        print("\n(Nur Bericht. Mit  --setx  dauerhaft in die Windows-Umgebung schreiben.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
