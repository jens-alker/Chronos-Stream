#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODUL 1 · AUTONOMER SAMMLER  (Scraper + Relevanz + Ontologie)
==============================================================
Start:   python3 scraper.py            ->  http://localhost:8000
Reset:   python3 scraper.py --reset

WAS ES TUT (autonom, ohne menschlichen Input):
  * scannt in festem Takt fuenf Quelltypen BREIT (nicht nur bekannte
    Stichworte):  Wirtschaftsnews (GDELT) · Patente (PatentsView) ·
    Ad-hoc-Mitteilungen (RSS/EQS) · Seed-/Fruehfinanzierung (EDGAR Form D) ·
    wissenschaftliche Artikel (OpenAlex + arXiv)
  * bewertet jede Fundstelle auf oekonomische Relevanz (LLM; ohne Key: Mock)
  * ordnet relevante Dokumente Themen zu und LAESST DIE ONTOLOGIE WACHSEN:
    unzuordenbare Dokumente erzeugen neue Themen (LLM oder Bigram-Mock)
  * das Ergebnis ist das ERWARTUNGSBILD: je Thema Belegmenge, Stufenmix
    (Wissenschaft->Patent->Funding->News), Momentum, Narrativ-Gap

DEINE ROLLE (bewusst nur Kuratieren):
  Quellen ausschalten/testen/Endpoint anpassen · Dokumente loeschen ·
  Relevanz-Bewertungen aendern · Themen ausschliessen/loeschen

LLM:   export ANTHROPIC_API_KEY=...   (sonst regelbasierter Mock-Modus)
Netz:  echte Quellen brauchen Internet. Der Demo-Ernter simuliert ohne Netz,
       damit der Kreislauf immer sichtbar ist (in der Oberflaeche abschaltbar).
SEC:   EDGAR verlangt einen User-Agent mit Kontakt -- unten UA_CONTACT setzen!
"""

from __future__ import annotations
import html as _html
import json, math, os, re, sqlite3, subprocess, sys, threading, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
# Geteilte Like-Dedup-Definition (Jens 30.07.: „zu EINER Definition zusammenführen", keine Insel). Heim und
# Cloud-Sammlung (sammler_db) rufen denselben Kern. Pfad-Insert, damit scraper.py auch standalone (Heim) importiert.
# QS-#6 (Claude): der Import ist gekapselt — fehlt `connectors/dedup_kern.py` (z. B. Einzeldatei ohne Repo-
# Kontext kopiert), degradiert das Dedup sauber AUS (No-Op), statt den einzigen 500k-Doc-Produzenten am Booten
# zu hindern. KEINE zweite Definition (die Stubs sind ein Aus-Schalter, kein Insel-Nachbau).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectors"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness"))
try:
    from dedup_kern import kosinus as _dedup_kosinus, beste_uebereinstimmung  # noqa: E402
except ImportError:
    _dedup_kosinus, beste_uebereinstimmung = None, None
try:
    import schluessel as _schluessel                                          # noqa: E402
except ImportError:
    _schluessel = None       # Key-Bruecke fehlt -> Scraper bootet trotzdem (fail-soft)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "scraper.db")
PORT = 8000
TODAY = lambda: date.today().isoformat()

# ==================================================================
#  KONFIGURATION  (Config/config.txt — vom Nutzer editierbar)
# ==================================================================
CONFIG = {}

def load_config():
    """Liest  config.txt  (schluessel = wert). Gesucht wird — in dieser Reihenfolge —
    neben scraper.py (`System\\Config\\`, `System\\`) UND eine Ebene darüber auf der
    Projektwurzel (`<Repo>\\Config\\`, `<Repo>\\`), weil Config/ + API Keys/ dort
    natürlich liegen. Fehlende Datei/Werte -> eingebaute Defaults. Bricht nie ab."""
    CONFIG.clear()
    _root = os.path.dirname(HERE)                          # Projektwurzel (über System/)
    for path in (os.path.join(HERE, "Config", "config.txt"),
                 os.path.join(HERE, "config.txt"),         # Fallback ohne Unterordner
                 os.path.join(_root, "Config", "config.txt"),   # <Repo>\Config\config.txt
                 os.path.join(_root, "config.txt")):       # <Repo>\config.txt
        if os.path.exists(path):
            try:
                for line in open(path, encoding="utf-8-sig"):
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    CONFIG[k.strip().lower()] = v.strip()
                CONFIG["_loaded_from"] = path
                break
            except Exception:
                pass
    return CONFIG

def cfg(key, default=None):
    v = CONFIG.get(key.lower())
    return v if v not in (None, "") else default

def cfg_bool(key, default=False):
    v = cfg(key)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "ja", "on")

def cfg_float(key, default):
    try:
        return float(cfg(key)) if cfg(key) is not None else default
    except Exception:
        return default

def cfg_int(key, default):
    try:
        return int(float(cfg(key))) if cfg(key) is not None else default
    except Exception:
        return default

load_config()

# Kontakt-Mail fuer User-Agent (EDGAR/SEC verlangen das). Aus Config, sonst Default.
UA_CONTACT = cfg("contact_email", "thesen-fabrik/3.0 (bitte-eigene-mail@einsetzen.de)")

def _read_key_file(path):
    try:
        k = open(path, encoding="utf-8-sig").read().strip()
        return k or None
    except Exception:
        return None

def _api_key():
    """Anthropic-Key: 1) direkt aus Config, 2) Config-Dateipfad, 3) Standard-
    Fundorte, 4) Umgebungsvariable. Erster Treffer gewinnt."""
    direct = cfg("anthropic_api_key")
    if direct:
        KEY_SOURCE["where"] = "config.txt"
        return direct
    cfg_path = cfg("anthropic_api_key_file")
    if cfg_path and os.path.exists(cfg_path):
        k = _read_key_file(cfg_path)
        if k:
            KEY_SOURCE["where"] = "config: " + os.path.basename(cfg_path)
            return k
    candidates = [
        os.path.join(HERE, "Anthropic API Key", "Anthropic API Key.txt"),
        os.path.join(HERE, "Anthropic API Key.txt"),
        os.path.join(HERE, "api_key", "api_key.txt"),
        os.path.join(HERE, "api_key.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            k = _read_key_file(p)
            if k:
                KEY_SOURCE["where"] = os.path.basename(p)
                return k
    if os.environ.get("ANTHROPIC_API_KEY"):
        KEY_SOURCE["where"] = "Umgebungsvariable"
        return os.environ["ANTHROPIC_API_KEY"]
    KEY_SOURCE["where"] = None
    return None

KEY_SOURCE = {"where": None}

# Live-Status: was tut der Scraper gerade? (fuer die GUI)
STATUS = {"phase": "bereit", "detail": "warte auf ersten Scan",
          "busy": False, "since": None, "scans_done": 0,
          "seen": 0, "kept": 0, "traced": 0, "dropped": 0,
          "woken": 0, "decayed": 0, "deduped": 0}

def set_status(phase, detail="", busy=None):
    STATUS["phase"] = phase
    STATUS["detail"] = detail
    if busy is not None:
        STATUS["busy"] = busy
    STATUS["since"] = datetime.now().strftime("%H:%M:%S")

# ==================================================================
#  SCHEMA
# ==================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, source_type TEXT,
  endpoint TEXT, enabled INTEGER DEFAULT 1, trust_prior REAL DEFAULT 0.5,
  last_crawl TEXT, last_found INTEGER DEFAULT 0, last_kept INTEGER DEFAULT 0,
  last_error TEXT, fail_count INTEGER DEFAULT 0, paused_until TEXT,
  cursor TEXT, rate_per_hour INTEGER DEFAULT 20, queries_win INTEGER DEFAULT 0,
  win_start TEXT, last_tick_ts TEXT, total_queries INTEGER DEFAULT 0, note TEXT);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, source_id INTEGER, source_type TEXT,
  title TEXT, text TEXT, url TEXT,
  relevance REAL, trust REAL, published_at TEXT NOT NULL,
  ingested_at TEXT DEFAULT (datetime('now')),
  dup_of INTEGER,                    -- Near-Dup: kanonisches Dokument (nicht-destruktiv)
  UNIQUE(title, published_at));

-- Semantik-Dedup: ein Embedding je KANONISCHEM Dokument (Near-Dups tragen keins).
-- vec = JSON-Float-Array. Ohne installiertes Embed-Modell bleibt die Tabelle leer
-- (der Dedup degradiert lautlos zu 'nichts markiert' — Signal bleibt korrekt, nur
-- verrauschter). Kosinus-Vergleich blockt auf source_type + jüngste Kandidaten.
CREATE TABLE IF NOT EXISTS doc_embeddings (
  doc_id INTEGER PRIMARY KEY, vec TEXT, model TEXT,
  at TEXT DEFAULT (datetime('now')));

CREATE TABLE IF NOT EXISTS themes (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, keywords TEXT,
  created_by TEXT DEFAULT 'seed', excluded INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')));

CREATE TABLE IF NOT EXISTS doc_themes (
  doc_id INTEGER, theme_id INTEGER, UNIQUE(doc_id, theme_id));

CREATE TABLE IF NOT EXISTS discarded (
  id INTEGER PRIMARY KEY, source_id INTEGER, source_type TEXT,
  title TEXT, url TEXT, relevance REAL, published_at TEXT,
  ingested_at TEXT DEFAULT (datetime('now')),
  revived INTEGER DEFAULT 0,
  UNIQUE(title, published_at));

CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY, at TEXT DEFAULT (datetime('now')),
  stage TEXT, message TEXT);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY, doc_id INTEGER, source_type TEXT,
  subjekt TEXT, beziehung TEXT, objekt TEXT,
  modus TEXT, signalart TEXT,
  reife TEXT, reife_score REAL, latenz TEXT,
  erwartungstempo REAL, konfidenz REAL,
  published_at TEXT, ingested_at TEXT DEFAULT (datetime('now')),
  UNIQUE(doc_id, subjekt, beziehung, objekt));

-- Merkt sich JEDEN geprueften Dokument-Versuch, auch wenn er 0 Fakten ergab.
-- Ohne das wuerde ein Neustart alle Null-Ergebnis-Dokumente erneut verarbeiten.
CREATE TABLE IF NOT EXISTS facts_done (
  doc_id INTEGER PRIMARY KEY,
  n_facts INTEGER DEFAULT 0,
  at TEXT DEFAULT (datetime('now')));

-- Merker fuer einmalige Migrationen (damit sie nie versehentlich erneut laufen)
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Gemessene Konsens-Naehe JE QUELLE (aus den Stufe-4-Urteilen).
-- Konsensnaehe ist keine Eigenschaft der Quelle, sondern der Aussage — aber
-- ueber viele Aussagen zeigt sich, WELCHE Quellen ueberdurchschnittlich oft
-- Nicht-Konsens liefern. Das ist gemessen, nicht angenommen.
CREATE TABLE IF NOT EXISTS quellen_profil (
  source_id INTEGER PRIMARY KEY,
  n_geprueft INTEGER DEFAULT 0,      -- durch Stufe 4 gelaufen
  n_trivial INTEGER DEFAULT 0,       -- als Allgemeinwissen verworfen
  n_einzelfall INTEGER DEFAULT 0,    -- als Einzelfall verworfen
  n_signal INTEGER DEFAULT 0);       -- durchgekommen = potenzielle Distanz

-- Relevanz-Feedback-Loop: JEDE Hand-Korrektur von Jens (GUI) wird als LABEL
-- protokolliert (alt->neu) und als Few-Shot-Anker in den Relevanz-Prompt
-- gespiegelt. Sein Urteil (die knappe Ressource) wird so nicht zweimal verbraucht.
CREATE TABLE IF NOT EXISTS relevanz_urteil (
  id INTEGER PRIMARY KEY, doc_id INTEGER, title TEXT, source_type TEXT,
  alt_score REAL, neu_score REAL, at TEXT DEFAULT (datetime('now')));
"""

DB = None
LOCK = threading.Lock()

def q(sql, args=(), fetch=True):
    with LOCK:
        cur = DB.execute(sql, args)
        if fetch:
            return [dict(r) for r in cur.fetchall()]
        DB.commit()
        return cur.lastrowid

def log(stage, msg):
    q("INSERT INTO log(stage,message) VALUES(?,?)", (stage, str(msg)[:400]), fetch=False)

# ==================================================================
#  LLM  (Anthropic-API; Mock-Fallback)
# ==================================================================
def llm_available():
    return bool(_api_key())

def llm_json(prompt, max_tokens=900):
    key = _api_key()
    if not key:
        return None
    try:
        body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
              data=body, headers={"Content-Type": "application/json",
                                  "x-api-key": key,
                                  "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.load(r)
        txt = "".join(b.get("text", "") for b in resp.get("content", []))
        return json.loads(re.sub(r"```json|```", "", txt).strip())
    except Exception as e:
        log("llm", f"LLM-Fehler: {e}")
        return None


# ==================================================================
#  LOKALES MODELL  (Ollama) + MODELL-WEICHE
# ==================================================================
# Aufgabenteilung: Volumen -> LOKAL (gratis), Urteil -> FRONTIER (bezahlt).
LOCAL = {"url": cfg("ollama_url", "http://127.0.0.1:11434"),   # 127.0.0.1: kein ::1-IPv6-Timeout (Windows, ~2s/Call)
         "model": cfg("ollama_model", "qwen3:30b")}
# Je Aufgabe: "local" | "frontier".  Hochvolumig -> local; hochwertig -> frontier.
ROUTING = {"relevance": cfg("routing_relevance", "local"),
           "ontology": cfg("routing_ontology", "local"),
           "facts": cfg("routing_facts", "local"),
           "trivial": cfg("routing_trivial", "local")}
# Frontier-LLM kostet Geld. Standardmaessig GESPERRT: der Sammler weicht NICHT
# still auf das bezahlte Modell aus, wenn Ollama mal nicht antwortet.
FRONTIER = {"allowed": cfg_bool("frontier_allowed", False)}
# Wenn Ollama ein Modell nicht kennt (404), darf das NICHT still im Mock enden.
MODEL_ALARM = {"missing": None}

_local_cache = {"at": 0, "ok": False, "models": []}

def local_available(force=False):
    """Prueft, ob Ollama laeuft, und listet installierte Modelle. Gecacht (10s),
    damit die 3s-Timeouts nicht jede Status-Abfrage blockieren."""
    now = time.time()
    if not force and now - _local_cache["at"] < 10:
        return _local_cache["ok"], _local_cache["models"]
    try:
        req = urllib.request.Request(LOCAL["url"] + "/api/tags")
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.load(r)
        models = [m.get("name", "") for m in data.get("models", [])]
        _local_cache.update(at=now, ok=True, models=models)
        return True, models
    except Exception:
        _local_cache.update(at=now, ok=False, models=[])
        return False, []

# ==================================================================
#  OLLAMA-ABSICHERUNG  (der wichtigste Schutz im Dauerbetrieb)
# ==================================================================
# Lehre aus der Datenanalyse: 768x HTTP 404 -> stiller Mock-Betrieb -> wochenlang
# WERTLOSE Relevanzen. Im Urlaub waere das fatal: der Sammler liefe zwei Wochen
# weiter und vergiftete die Datenbank mit Mock-Bewertungen.
# Grundsatz: LIEBER STILLSTAND ALS DATENVERGIFTUNG.
OLLAMA = {"ok": True, "down_since": None, "restarts": 0, "paused_collector": False,
          "last_try": 0, "note": "", "lage": "ok", "fehler": 0}

def _ollama_probe():
    """Dreistufige Diagnose — die Ursache entscheidet ueber die Konsequenz:
      'weg'      Dienst nicht erreichbar        -> Neustart sinnvoll
      'fehlt'    Dienst da, Modell nicht da     -> Neustart SINNLOS ('ollama pull')
      'haengt'   Modell gelistet, antwortet aber nicht -> Neustart sinnvoll
      'ok'       antwortet
    Die dritte Stufe ist wichtig: Ollama kann lebendig WIRKEN (Liste antwortet)
    und trotzdem beim Rechnen festhaengen — etwa nach einem GPU-Absturz."""
    ok, models = local_available(force=True)
    if not ok:
        return "weg", "Ollama nicht erreichbar"
    ziel = (cfg("facts_model") or LOCAL["model"] or "").strip()
    if ziel and models:
        basis = ziel.split(":")[0]
        if not any(m == ziel or m.split(":")[0] == basis for m in models):
            return "fehlt", (f"Modell '{ziel}' nicht installiert — "
                             f"holen mit:  ollama pull {ziel}")
    # Antwortet das Modell wirklich? Winziger Generierungstest.
    # WICHTIG: durch die Schleuse! Ein ungetakteter Ping neben einem laufenden
    # 1c-Aufruf laesst Ollama einen ZWEITEN Parallel-Slot anlegen — jeder mit
    # eigenem KV-Cache. Das sprengt bei einem 19-GB-Modell die 20 GB VRAM,
    # Windows lagert in den gemeinsamen Speicher (RAM) aus, und der Rechner
    # stirbt. Ist die Schleuse belegt, rechnet das Modell ohnehin gerade =
    # es lebt: dann gar nicht erst pingen.
    if not LLM_GATE.acquire(blocking=False):
        return "ok", ""                     # arbeitet gerade -> gesund
    # Fable-M3: auch die PROZESS-GRENZE prüfen — nutzt gerade ein ANDERER Prozess (Pipeline/nomic) die GPU,
    # NICHT den 30b-Ping daneben laden (OOM). Cross-Sperre non-blocking (timeout=0): belegt -> fremder
    # Modell-Nutzer aktiv = gesund, nicht pingen. Sonst HALTEN wir sie während des Pings (finally gibt frei).
    mdl = ziel or LOCAL["model"]
    _cross = _gpu_lock(mdl, timeout=0)
    try:
        _cross.__enter__()
    except _GpuBelegt:
        LLM_GATE.release()
        return "ok", ""                     # fremder Prozess rechnet -> gesund
    except Exception:                       # noqa: BLE001 — nullcontext/Modul weg: kein Cross-Lock, normal weiter
        _cross = None
    try:
        payload = {"model": mdl,
                   "messages": [{"role": "user", "content": "ping"}],
                   "stream": False,
                   # num_predict klein, aber NICHT 1: ein Thinking-Modell kappt
                   # bei 1 Token vor jeder Ausgabe -> Ollama 500.
                   "options": {"num_predict": 16}}
        if "qwen3" in mdl.lower():
            # MUSS den echten Extraktions-Aufruf (_ollama_chat) spiegeln: Thinking
            # AUS. Sonst denkt qwen3, der num_predict-Deckel kappt vor der Antwort
            # -> Ollama 500 -> der Ping meldet faelschlich 'haengt' und pausiert
            # Sammler + 1c, obwohl das Modell mit think=False sauber laeuft.
            payload["think"] = False
        body = json.dumps(payload).encode()
        req = urllib.request.Request(LOCAL["url"] + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=cfg_int("ollama_ping_timeout", 90)):
            pass
        return "ok", ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "fehlt", f"Modell '{ziel}' unbekannt (404) — ollama pull {ziel}"
        return "haengt", f"Modell antwortet mit Fehler {e.code}"
    except Exception as e:
        return "haengt", f"Modell antwortet nicht ({type(e).__name__})"
    finally:
        if _cross is not None:
            try:
                _cross.__exit__(None, None, None)   # Cross-Sperre freigeben (falls gehalten)
            except Exception:                       # noqa: BLE001
                pass
        LLM_GATE.release()      # MUSS frei werden, sonst steht alles still

def _pfad_tokens(cmd):
    """Alle Tokens eines Befehls, quote-aware (Anfuehrungszeichen halten Leerzeichen zusammen). OS-unabhaengig
    (kein `shlex`, das im posix-Modus Windows-Backslashes fressen wuerde)."""
    toks = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', cmd or "")
    return [t.strip('"').strip("'") for t in toks]


def _ist_pfad_token(t):
    """Sieht ein Token wie ein Dateipfad aus? (Separator / Laufwerksbuchstabe / Datei-Endung — aber NICHT die
    cmd-Schalter `/c`/`/k`, die sonst faelschlich als Pfad gaelten)."""
    if t in ("/c", "/k", "/C", "/K"):
        return False
    return ("/" in t) or ("\\" in t) or bool(re.match(r"^[A-Za-z]:", t)) or bool(os.path.splitext(t)[1])


def _fehlender_pfad(cmd):
    """Erstes PFAD-artige Token des Befehls, das NICHT existiert (Fable-M1: ein Wrapper wie
    `start "" "A:\\Alt\\Ollama Start.bat"` verbirgt den toten Pfad HINTER `start` — nur das erste Token zu
    pruefen wuerde ihn durchlassen und das Windows-'nicht gefunden'-Popup ausloesen). Gibt None, wenn kein
    path-artiges Token fehlt (blosses Kommando ODER alle Pfade existieren)."""
    for t in _pfad_tokens(cmd):
        if _ist_pfad_token(t) and not os.path.isfile(t):
            return t
    return None


def _ollama_start_command():
    """Robuster Ollama-Start-Befehl (Jens 08.08.). Ein in `config.txt` gesetztes `ollama_restart_cmd` aus einer
    ALTINSTALLATION zeigte auf einen nicht mehr existierenden Pfad (`...\\Macro Research\\Ollama Start.bat`) und
    loeste beim Feuern ein Windows-'nicht gefunden'-Popup aus (via `start`/ShellExecute). Deshalb: das
    konfigurierte Kommando wird NUR genutzt, wenn KEIN path-artiges Token darin fehlt (auch hinter `start`/`cmd`);
    sonst fail-closed Fallback auf die MITGELIEFERTE `Ollama_Start.bat` neben scraper.py (nur auf Windows —
    m4), zuletzt `ollama serve`.
    -> (cmd, shell, note): cmd = String (shell=True) ODER Liste (shell=False); note = ehrlicher Hinweis (Stille≠Grün)."""
    cmd = (cfg("ollama_restart_cmd", "") or "").strip()
    note = ""
    if cmd:
        if os.path.isfile(cmd.strip('"').strip("'")):            # ganzer Befehl = existierende Datei (Pfad mit Leerzeichen, unquotiert)
            return cmd, True, ""
        fehlt = _fehlender_pfad(cmd)
        if fehlt is None:
            return cmd, True, ""                                 # blosses Kommando ODER alle enthaltenen Pfade existieren
        note = (f"ollama_restart_cmd verweist auf eine fehlende Datei ({fehlt}) — ignoriert (Altpfad?), "
                f"nutze die mitgelieferte Ollama_Start.bat.")
    lokal = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ollama_Start.bat")
    if os.name == "nt" and os.path.isfile(lokal):                # m4: die .bat nur auf Windows starten (posix: ENOEXEC)
        return [lokal], False, note
    return ["ollama", "serve"], False, (note or "nutze 'ollama serve'.")


def _ollama_spawn(cmd, shell):
    """Startet den aufgeloesten Ollama-Befehl detached (eigene Session/Prozessgruppe), stumm. `stdin=DEVNULL`
    (Fable-m2): ohne Konsole/stdin bräche ein `timeout` in der .bat sofort ab — DEVNULL haelt den Pfad robust."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, shell=shell, **kw)


def _ollama_erreichbar():
    """Billiger Erreichbarkeits-Check (kein Generierungs-Ping): antwortet /api/tags?"""
    try:
        with urllib.request.urlopen(LOCAL["url"] + "/api/tags", timeout=2) as r:
            return getattr(r, "status", 200) == 200
    except Exception:                                            # noqa: BLE001
        return False


def _ollama_restart():
    """Versucht Ollama neu zu starten. Der Befehl wird ueber `_ollama_start_command` robust aufgeloest
    (kein Feuern eines veralteten/fehlenden Pfads -> kein Windows-Popup). Meldet JEDE Entscheidung."""
    if OLLAMA.get("manuell_gestoppt"):               # Fable-m1: bewusst per Knopf gestoppt -> NICHT auto-neustarten
        return False
    cmd, shell, note = _ollama_start_command()
    if note:
        log("ollama", note)
    try:
        _ollama_spawn(cmd, shell)
        OLLAMA["restarts"] += 1
        gz = cmd if isinstance(cmd, str) else " ".join(cmd)
        log("ollama", f"Neustart ausgeloest ({OLLAMA['restarts']}. Mal): {gz[:70]}")
        return True
    except Exception as e:                                        # noqa: BLE001
        log("ollama", f"Neustart FEHLGESCHLAGEN: {str(e)[:100]}")
        return False


def _ollama_manuell_start():
    """Ollama-Dienst manuell starten (Jens 08.08., Start-Knopf). Fable-m7: laeuft Ollama schon (API erreichbar),
    wird NICHTS angefasst (der Kill-und-Kaltstart in der .bat wuerde sonst ein geladenes Modell erschiessen).
    Fable-m1: der `manuell_gestoppt`-Latch wird VOR dem Spawn aufgehoben (die ABSICHT zu starten zaehlt, auch
    wenn der Spawn scheitert). -> (ok, msg)."""
    if _ollama_erreichbar():
        OLLAMA["manuell_gestoppt"] = False
        return True, "Ollama laeuft bereits (nicht angefasst)."
    OLLAMA["manuell_gestoppt"] = False               # m1: Latch VOR dem Spawn loesen (Absicht zaehlt)
    cmd, shell, note = _ollama_start_command()
    try:
        _ollama_spawn(cmd, shell)
        log("ollama", "Manueller Start ausgeloest." + (f" {note}" if note else ""))
        msg = "Ollama-Start ausgeloest (Status aktualisiert sich in Kuerze)."
        return True, (msg + " " + note if note else msg)
    except Exception as e:                                        # noqa: BLE001
        return False, f"Start fehlgeschlagen: {str(e)[:100]}"


def _ollama_manuell_stop():
    """Ollama-Dienst manuell stoppen (Jens 08.08., Stop-Knopf): Windows `taskkill`, sonst `pkill -x`. -> (ok, msg).
    Setzt den `manuell_gestoppt`-Latch (Fable-m1): der 60-s-Guard startet Ollama dann NICHT automatisch wieder."""
    try:
        OLLAMA["manuell_gestoppt"] = True            # bewusst gestoppt -> Guard-Auto-Neustart aussetzen
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True)
            subprocess.run(["taskkill", "/F", "/IM", "ollama app.exe"], capture_output=True)
        else:
            subprocess.run(["pkill", "-x", "ollama"], capture_output=True)   # -x: exakt 'ollama', nicht jede Cmdline
        log("ollama", "Manueller Stopp ausgeloest.")
        return True, "Ollama-Stopp ausgeloest (Auto-Neustart ausgesetzt bis 'Ollama starten')."
    except Exception as e:                                        # noqa: BLE001
        return False, f"Stopp fehlgeschlagen: {str(e)[:100]}"


def _ollama_status_kurz():
    """CHEAPer Status fuer die UI (kein Generierungs-Ping!): Dienst per /api/tags erreichbar + welches Modell die
    prozess-uebergreifende GPU-Sperre haelt. -> dict."""
    erreichbar = _ollama_erreichbar()
    gpu = {}
    try:
        from modell_gate import status as _mstatus
        gpu = _mstatus()
    except Exception:                                            # noqa: BLE001
        gpu = {}
    return {"erreichbar": erreichbar, "note": OLLAMA.get("note", ""),
            "gpu_sperre": gpu, "model": cfg("facts_model") or LOCAL["model"],
            "embed_model": cfg("embed_model", "nomic-embed-text")}


def ollama_guard():
    """Wacht ueber Ollama. Ist es weg:
      1) Sammler + 1c PAUSIEREN (keine Mock-Bewertungen in die DB!)
      2) Neustart NUR, wenn er helfen kann (Dienst tot / Modell haengt) —
         NICHT bei fehlendem Modell: da hilft nur 'ollama pull'.
      3) laufend erneut pruefen; sobald Ollama zurueck ist -> weitermachen"""
    lage, grund = _ollama_probe()
    if lage == "ok":
        if not OLLAMA["ok"]:                       # war weg, ist zurueck
            weg = ""
            if OLLAMA["down_since"]:
                mins = int((time.time() - OLLAMA["down_since"]) / 60)
                weg = f" (war {mins} min weg)"
            log("ollama", f"Ollama antwortet wieder{weg} — Betrieb wird fortgesetzt.")
            OLLAMA.update(ok=True, down_since=None, note="", lage="ok", fehler=0)
            if OLLAMA["paused_collector"]:         # nur das zuruecknehmen, was
                OLLAMA["paused_collector"] = False # WIR pausiert haben
                start_collector()
            if OLLAMA.get("paused_facts"):         # Fable-B2: 1c war von UNS pausiert (Ausfall) ODER als Absicht
                OLLAMA["paused_facts"] = False     # gemerkt (1c-Start bei down) -> jetzt starten, wo Ollama da ist
                start_extraction(None)
                log("1c", "Ollama zurueck — 1c wird (wieder) gestartet.")
        return True
    # --- Ollama nicht einsatzbereit ---
    OLLAMA["fehler"] = OLLAMA.get("fehler", 0) + 1
    if OLLAMA["ok"]:                               # erster Ausfall
        OLLAMA.update(ok=False, down_since=time.time())
        log("OLLAMA", f"{grund}. Sammler wird PAUSIERT — sonst landen wertlose "
                      f"Mock-Bewertungen in der Datenbank.")
    OLLAMA["lage"] = lage
    mins = int((time.time() - (OLLAMA["down_since"] or time.time())) / 60)
    OLLAMA["note"] = f"{grund} — seit {mins} min. Sammler pausiert."
    if COLLECTOR["running"]:
        OLLAMA["paused_collector"] = True
        stop_collector()
    if FACTS.get("running"):                       # Fable-B2: merken, dass WIR 1c pausiert haben ->
        OLLAMA["paused_facts"] = True              # bei Ollama-Rueckkehr wird 1c wieder gestartet (nicht nur der Sammler)
    FACTS["running"] = False
    # Neustart NUR wenn er etwas bewirken kann, nicht bedingungslos:
    #  - 'fehlt'  -> ein Neustart installiert kein Modell. Nur melden.
    #  - 'weg'    -> Dienst tot: Neustart hilft, nach 2 Fehlversuchen.
    #  - 'haengt' -> VORSICHT: ein 19-GB-Modell braucht beim ersten Laden
    #    Minuten. In der Zeit antwortet der Server, aber die Generierung
    #    laeuft in den Timeout — das sieht aus wie 'haengt'. Ein Neustart
    #    wuerde das LADENDE Modell erschiessen -> Endlosschleife.
    #    Deshalb: erst nach vielen Fehlversuchen in Folge (= es laedt wirklich
    #    nicht mehr, sondern haengt).
    if lage == "fehlt":
        OLLAMA["note"] += " (Neustart wuerde nicht helfen — Modell fehlt.)"
        return False
    noetig = 2 if lage == "weg" else cfg_int("ollama_haengt_geduld", 6)
    if OLLAMA["fehler"] < noetig:
        if lage == "haengt":
            OLLAMA["note"] += (f" (laedt evtl. noch — Geduld {OLLAMA['fehler']}"
                               f"/{noetig})")
        return False
    if time.time() - OLLAMA["last_try"] > 300:
        OLLAMA["last_try"] = time.time()
        if _ollama_restart():
            OLLAMA["note"] += " Neustart ausgeloest."
            OLLAMA["fehler"] = 0        # nach dem Neustart wieder Geduld haben
    return False

def _salvage_facts(txt):
    """Rettet vollstaendige Objekte aus abgeschnittenem JSON und ordnet sie der
    richtigen Aufgabe zu. Statt das ganze Ergebnis zu verlieren (und im Mock zu
    landen), werden die bereits komplett gelieferten Objekte einzeln geparst.
    Erkennt Fakten (subjekt), Ontologie-Themen (name+keywords) und
    Relevanz-Urteile (relevance)."""
    objs = []
    for m in re.finditer(r"\{[^{}]*\}", txt or ""):
        try:
            o = json.loads(m.group(0))
            if isinstance(o, dict):
                objs.append(o)
        except Exception:
            pass
    if not objs:
        return None
    fakten = [o for o in objs if o.get("subjekt")]
    if fakten:
        return {"fakten": fakten}
    themen = [o for o in objs if o.get("name") and o.get("keywords")]
    if themen:
        return {"themes": themen}
    urteile = [o for o in objs if "relevance" in o or "relevanz" in o]
    if urteile:
        return {"ratings": urteile}
    return None

# ==================================================================
#  GPU-SCHUTZ + MODELL-SCHLEUSE
# ==================================================================
# Die Grafikkarte betreibt auch die Bildschirme. Sie darf nicht so belastet
# werden, dass der Rechner haengt. Zwei Sicherungen:
#  1) LLM_GATE: nur EIN Modellaufruf gleichzeitig (Sammler und 1c teilen sich
#     die GPU -> sie alternieren statt sich zu ueberlagern).
#  2) gpu_guard(): fragt nvidia-smi nach Temperatur; wird es zu warm, legt der
#     Prozess eine Pause ein, bis die Karte abgekuehlt ist.
LLM_GATE = threading.Semaphore(1)


# Prozess-ÜBERGREIFENDE GPU-Sperre (Jens 08.08.): LLM_GATE serialisiert nur die THREADS DIESES Prozesses.
# Läuft aber ein Pipeline-Prozess mit `nomic` (Embeddings/V1) parallel zum Sammler-`qwen3:30b`, lädt Ollama
# beide Modelle -> OOM. `modell_gate.gpu_lock` (Lockfile) serialisiert prozess-übergreifend. Fällt das Modul
# (Alt-Stand), degradiert es sauber auf einen No-Op (nur In-Process-LLM_GATE, wie bisher).
try:
    from modell_gate import gpu_lock as _gpu_lock, GpuBelegt as _GpuBelegt
except Exception:                                  # noqa: BLE001
    import contextlib as _contextlib

    def _gpu_lock(*_a, **_k):
        return _contextlib.nullcontext()

    class _GpuBelegt(Exception):                    # Fallback-Typ (Modul weg -> nie geworfen)
        pass


class _GpuGate:
    """`with _gpu_gate(model):` = In-Process-LLM_GATE UND prozess-übergreifende Modell-Sperre in EINEM."""
    def __init__(self, modell="?"):
        self.modell = modell
        self._cross = None

    def __enter__(self):
        LLM_GATE.acquire()                          # erst in-process (billig, immer), dann cross-process
        try:
            self._cross = _gpu_lock(self.modell)
            self._cross.__enter__()
        except _GpuBelegt:
            # Fable-M2: die GPU ist NACHWEISLICH von einem anderen Prozess belegt (> Timeout). Dann NICHT den
            # 19-GB-Call feuern (das waere genau der OOM) -> LLM_GATE freigeben (kein __exit__ nach __enter__-Wurf!)
            # und propagieren; der Aufrufer (ask_json/local_json) pausiert statt zu vergiften.
            LLM_GATE.release()
            raise
        except Exception:                           # noqa: BLE001 — Modul weg/OSError: auf In-Process-Gate degradieren
            self._cross = None
        return self

    def __exit__(self, *a):
        try:
            if self._cross is not None:
                self._cross.__exit__(*a)
        finally:
            LLM_GATE.release()
        return False


def _gpu_gate(modell="?"):
    return _GpuGate(modell)


GPU = {"enabled": cfg_bool("gpu_guard", True),
       "max_temp": cfg_int("gpu_max_temp", 80),
       "cooldown_ms": cfg_int("gpu_cooldown_ms", 150),
       "temp": None, "paused": 0, "last_check": 0}

def _gpu_temp():
    """Temperatur der GPU via nvidia-smi. None, wenn nicht verfuegbar."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return int(out.stdout.strip().split("\n")[0])
    except Exception:
        pass
    return None

def gpu_guard():
    """Vor jedem Modellaufruf: kurze Verschnaufpause, und bei Ueberhitzung
    warten, bis die Karte wieder kuehl ist. Schuetzt Desktop/Bildschirme."""
    if not GPU["enabled"]:
        return
    if GPU["cooldown_ms"]:
        time.sleep(GPU["cooldown_ms"] / 1000.0)      # GPU Luft zum Atmen lassen
    now = time.time()
    if now - GPU["last_check"] < 10:                 # nicht bei jedem Aufruf messen
        return
    GPU["last_check"] = now
    t = _gpu_temp()
    GPU["temp"] = t
    if t is None:
        return
    if t >= GPU["max_temp"]:
        GPU["paused"] += 1
        log("gpu", f"GPU bei {t}°C (Grenze {GPU['max_temp']}°C) — pausiere zum Abkuehlen.")
        for _ in range(60):                          # max 60s warten
            time.sleep(1)
            t = _gpu_temp()
            if t is None or t < GPU["max_temp"] - 5:
                break
        GPU["temp"] = t
        log("gpu", f"GPU wieder bei {t}°C — weiter.")

def _ollama_chat(mdl, prompt, max_tokens, fmt):
    """Ein Ollama-/api/chat-Aufruf durch die GPU-Schleuse. fmt = 'json' ODER ein
    JSON-Schema-dict (schema-constrained decoding). Gibt den Roh-Text zurueck."""
    is_qwen3 = "qwen3" in mdl.lower()
    payload = {"model": mdl,
               "messages": [{"role": "user", "content": prompt}],
               "stream": False, "format": fmt,
               "options": {"num_predict": max_tokens,
                           # Qwen3 non-thinking: temp 0.7 / top_p 0.8 / top_k 20;
                           # sonst konservativ 0.2 fuer konsistente Extraktion.
                           "temperature": 0.7 if is_qwen3 else 0.2,
                           "top_p": 0.8, "top_k": 20}}
    if is_qwen3:
        payload["think"] = False               # Thinking-Modus aus -> saubere JSON
    body = json.dumps(payload).encode()
    req = urllib.request.Request(LOCAL["url"] + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    # Schleuse: nur ein Modellaufruf gleichzeitig (in-process UND prozess-übergreifend) + GPU-Schutz davor.
    with _gpu_gate(mdl):
        gpu_guard()
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.load(r)
    return (resp.get("message") or {}).get("content", "")

class UnlesbareModellantwort(Exception):
    """Das Modell HAT geantwortet (HTTP 200), aber die Ausgabe war auch nach
    _salvage_facts nicht als JSON lesbar (abgeschnitten/Muell). Das ist ein
    UNBRAUCHBARES ERGEBNIS, NICHT der Ausfall des Modells (ModellWeg) — die zwei
    nie verwechseln. Eigener Typ (statt roher JSONDecodeError), damit ein
    kaputter API-Umschlag von _ollama_chat NICHT faelschlich als 'unbrauchbar'
    zaehlt, sondern als echter Ausfall behandelt wird."""


def local_json(prompt, max_tokens=900, model=None, schema=None):
    """Ruft das lokale Ollama-Modell und erzwingt JSON.
    Mit `schema` (JSON-Schema-dict) laeuft SCHEMA-CONSTRAINED decoding (Ollama
    >=0.5): das Modell KANN grammatikalisch kein invalides/abgeschnittenes JSON
    mehr erzeugen — das war die Hauptfehlerquelle (abgeschnittene Fakten -> Mock).
    Kennt eine aeltere Ollama-Version das dict-`format` nicht (400/422/500), wird
    transparent auf format='json' zurueckgeschaltet; die Salvage-Absicherung bleibt
    als letztes Netz. Bei Qwen3 ist Thinking aus (think=False)."""
    mdl = model or LOCAL["model"]
    fmt = schema if (schema and cfg_bool("strukturiertes_json", True)) else "json"
    try:
        txt = _ollama_chat(mdl, prompt, max_tokens, fmt)
    except urllib.error.HTTPError as e:
        if fmt != "json" and e.code in (400, 422, 500):
            log("local", f"Schema-format abgelehnt ({e.code}) — ohne Schema weiter "
                         f"(Ollama aktualisieren fuer strukturiertes JSON).")
            txt = _ollama_chat(mdl, prompt, max_tokens, "json")
        else:
            raise
    clean = re.sub(r"```json|```", "", txt).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        # Antwort abgeschnitten (Token-Limit) -> vollstaendige Objekte retten,
        # statt das ganze Ergebnis zu verlieren (sonst: Mock = wertlose Daten).
        sal = _salvage_facts(clean)
        if sal:
            n = len(next(iter(sal.values())))
            log("local", f"JSON abgeschnitten — {n} Objekt(e) gerettet.")
            return sal
        # Auch die Salvage rettet nichts. Das Modell hat geantwortet, aber
        # unbrauchbar -> eigener Fehlertyp (kein roher JSONDecodeError), damit die
        # Weiche das sauber von einem echten Ausfall trennt.
        raise UnlesbareModellantwort(str(e)[:100]) from None

def local_test():
    ok, models = local_available()
    if not ok:
        return {"ok": False, "message": f"Ollama nicht erreichbar unter {LOCAL['url']} "
                "— laeuft der Dienst? (ollama serve)"}
    if LOCAL["model"] not in models and not any(
            m.split(":")[0] == LOCAL["model"].split(":")[0] for m in models):
        return {"ok": False, "message": f"Ollama laeuft, aber Modell "
                f"'{LOCAL['model']}' fehlt. Installiert: {', '.join(models) or '—'}. "
                f"Holen mit:  ollama pull {LOCAL['model']}"}
    try:
        res = local_json('Antworte NUR mit JSON: {"ok": true}', 50)
        return ({"ok": True, "message": f"Lokales Modell '{LOCAL['model']}' antwortet."}
                if isinstance(res, dict) and res.get("ok")
                else {"ok": False, "message": "Modell antwortete, aber kein sauberes JSON."})
    except Exception as e:
        return {"ok": False, "message": f"Aufruf fehlgeschlagen: {str(e)[:120]}"}

def ask_json(task, prompt, max_tokens=900, model=None, schema=None):
    """Modell-Weiche: schickt die Aufgabe an das konfigurierte Ziel (lokal/
    frontier). Frontier NUR, wenn ausdruecklich freigegeben (FRONTIER.allowed) —
    sonst keine stillen Kosten. `schema` (JSON-Schema) greift nur auf dem lokalen
    Pfad (schema-constrained decoding).

    RUECKGABE-KONTRAKT (wichtig — der Unterschied hat den 1c-Rueckstand nie leeren
    lassen): None = KEIN Modell hat geantwortet (Ausfall) -> Aufrufer nutzt Mock,
    facts loest ModellWeg aus (pausieren, Dok NICHT erledigt). {} = das Modell HAT
    geantwortet, aber die Ausgabe war unbrauchbar (JSON auch nach Salvage unlesbar)
    -> das ist ein ERGEBNIS, kein Ausfall; facts markiert das Dok als erledigt
    (0 Fakten) und geht weiter, statt es als 'Modell weg' ewig neu zu versuchen."""
    target = ROUTING.get(task, "frontier")
    lokal_unbrauchbar = False        # HTTP 200, aber JSON unlesbar -> Ergebnis, kein Ausfall
    if target == "local":
        ok, _ = local_available()
        if ok:
            try:
                return local_json(prompt, max_tokens, model=model, schema=schema)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # Modell fehlt/kaputt = Konfigurationsfehler, KEIN transienter
                    # Aussetzer. Laut melden, sonst bewertet der Sammler still per
                    # Mock weiter und erzeugt wertlose Relevanzen.
                    mdl = model or LOCAL["model"]
                    MODEL_ALARM["missing"] = mdl
                    log("MODELL FEHLT", f"Ollama kennt '{mdl}' nicht (404). "
                        f"Holen mit:  ollama pull {mdl}  — bis dahin wird NICHT "
                        f"echt bewertet ({task} laeuft im Mock)!")
                else:
                    log("local", f"Lokal-Fehler ({task}): {str(e)[:120]}")
            except UnlesbareModellantwort as e:
                # Das Modell HAT geantwortet (HTTP 200), aber die Ausgabe war auch
                # nach _salvage_facts nicht lesbar. Das ist ein UNBRAUCHBARES
                # ERGEBNIS, KEIN Modell-Ausfall — die zwei NIE verwechseln: sonst
                # behandelt 1c ein Gift-Dokument als 'Modell weg', pausiert und
                # markiert es nicht erledigt -> ewig neu versucht, der Rueckstand
                # leert nie (der Befund aus scraper.db: 40k offen, facts eingefroren).
                log("local", f"Lokal-Fehler ({task}): Ausgabe unlesbar trotz Salvage "
                             f"— {str(e)[:80]}")
                lokal_unbrauchbar = True
            except Exception as e:
                log("local", f"Lokal-Fehler ({task}): {str(e)[:120]}")
        # Lokal nicht verfuegbar/unbrauchbar: NUR mit Frontier-Freigabe zahlen
        if not FRONTIER["allowed"]:
            log("weiche", f"Lokal aus, Frontier gesperrt ({task}) -> Mock/uebersprungen")
            # 'geantwortet, unbrauchbar' (facts) -> {} statt None: kein ModellWeg.
            return {} if (task == "facts" and lokal_unbrauchbar) else None
    # Ziel frontier ODER Fallback bei freigegebenem Frontier
    if FRONTIER["allowed"] and llm_available():
        return llm_json(prompt, max_tokens)
    if target == "frontier" and not FRONTIER["allowed"]:
        log("weiche", f"Frontier gesperrt ({task}) -> Mock/uebersprungen")
    return {} if (task == "facts" and lokal_unbrauchbar) else None

def any_model_active():
    """True, wenn irgendein echtes Modell (lokal oder frontier) nutzbar ist."""
    ok, _ = local_available()
    return ok or llm_available()

# --- JSON-Schemata fuer schema-constrained decoding (lokaler Ollama-Pfad) ------
# Erzwingen die Struktur BEI der Generierung — kein abgeschnittenes/ungueltiges
# JSON mehr. Enums spiegeln exakt das geschlossene Vokabular (3.12-konform).
# maxItems/maxLength BEGRENZEN die Grammatik des schema-constrained decoding.
# OHNE sie durfte das Modell eine beliebig lange Fakten-Liste + beliebig lange
# Strings erzeugen und lief dabei in den num_predict-Deckel (3000 Tokens) ->
# abgeschnittenes JSON. Das war die HAUPT-Truncation-Quelle (scraper.db: 1254x
# "abgeschnitten", 34x exakt bei char 6000 gekappt). extract_facts nimmt ohnehin
# nur [:3] und kappt die Strings auf 200/120/200 — die Grenzen kosten also nichts.
FACTS_SCHEMA = {
    "type": "object",
    "properties": {"fakten": {"type": "array", "maxItems": 3, "items": {
        "type": "object",
        "properties": {
            "subjekt": {"type": "string", "maxLength": 200},
            "beziehung": {"type": "string", "maxLength": 120},
            "objekt": {"type": "string", "maxLength": 200},
            "modus": {"type": "string", "enum": ["ist", "wird"]},
            "signalart": {"type": "string", "enum": ["technologie", "ereignis"]},
            "reife_score": {"type": "number"},
            "latenz": {"type": "string", "enum": ["kurz", "mittel", "lang"]},
            "erwartungstempo": {"type": "number"}, "konfidenz": {"type": "number"}},
        "required": ["subjekt", "beziehung", "objekt", "modus", "signalart", "latenz"]}}},
    "required": ["fakten"]}

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {"ratings": {"type": "array", "items": {"type": "number"}}},
    "required": ["ratings"]}

ONTOLOGY_SCHEMA = {
    "type": "object",
    # Prompt fordert "1-3 NEUE Themen", Namen/Schlagworte KURZ -> begrenzte
    # Grammatik verhindert die 17x beobachtete ontology-Truncation (-> Mock).
    "properties": {"themes": {"type": "array", "maxItems": 3, "items": {
        "type": "object",
        "properties": {"name": {"type": "string", "maxLength": 80},
                       "keywords": {"type": "array", "maxItems": 6,
                                    "items": {"type": "string", "maxLength": 40}}},
        "required": ["name", "keywords"]}}},
    "required": ["themes"]}

TRIVIAL_SCHEMA = {
    "type": "object",
    "properties": {"allgemeinwissen": {"type": "boolean"},
                   "einzelfall": {"type": "boolean"},
                   "begruendung": {"type": "string"}},
    "required": ["allgemeinwissen", "einzelfall"]}

# ==================================================================
#  ERNTER  (Hybrid: Entdeckung "was gibt es?" + gezielte Klaerung)
# ==================================================================
# Breite, THEMENNEUTRALE Anker fuer den Entdeckungs-Strom. Bewusst NICHT die
# gewachsene Ontologie — sonst bestaetigt der Sammler nur Bekanntes.
BROAD = ["economy", "technology", "industry", "energy", "manufacturing",
         "supply chain", "investment", "materials", "infrastructure", "trade"]


def broad_anker():
    """Die breiten, themenneutralen Such-Anker — **GUI/DB-editierbar** (Jens 29.07.): gelesen aus
    `meta['such_anker']` (JSON-Liste); die `BROAD`-Konstante ist nur noch der **Seed-Default**. So steuert Jens
    die breite Suche ohne Code-Edit — symmetrisch zu den eingeschränkten `gnews_topic`-Begriffen in
    `sources.endpoint`. Fehlt/kaputt/leer -> `BROAD`-Fallback (fail-closed, nie leerer Anker-Satz)."""
    rows = None
    try:
        rows = q("SELECT value FROM meta WHERE key='such_anker'")
    except Exception:
        return list(BROAD)                               # DB-Problem -> stiller Seed-Fallback (kein Log-Rekursions-Risiko)
    if rows and rows[0]["value"]:
        try:
            terms = json.loads(rows[0]["value"])
            sauber = [str(t).strip() for t in terms if str(t).strip()] if isinstance(terms, list) else []
            if sauber:
                return sauber
            raise ValueError("leere/ungültige Anker-Liste")
        except Exception as e:                           # meta VORHANDEN aber korrupt -> auditierbar loggen (QS-B3), dann Fallback
            try:
                log("broad_anker", f"korruptes meta['such_anker'] -> Fallback BROAD: {str(e)[:80]}")
            except Exception:
                pass
    return list(BROAD)                                   # fehlend (normal) ODER korrupt (geloggt)


def set_broad_anker(terms):
    """Die breiten Such-Anker setzen (GUI/DB). Leere/ungültige Eingabe -> Reset auf die `BROAD`-Seed-Defaults
    (nie ein leerer Anker-Satz). -> die effektiv gesetzte Liste."""
    sauber = [str(t).strip() for t in (terms or []) if str(t).strip()]
    effektiv = sauber if sauber else list(BROAD)
    q("INSERT OR REPLACE INTO meta(key,value) VALUES('such_anker',?)",
      (json.dumps(effektiv, ensure_ascii=False),), fetch=False)
    return effektiv


def seed_broad_anker():
    """Beim Seed die Anker EINMAL nach `meta` schreiben (falls nicht gesetzt) → in GUI/DB sichtbar + editierbar."""
    if not q("SELECT value FROM meta WHERE key='such_anker'"):
        set_broad_anker(list(BROAD))

def _uncertain_terms():
    """Begriffe aus UNSICHEREN Themen: mittlere Relevanz UND duenne Beleglage.
    Genau die Grenzfaelle, deren Relevanz sich durch mehr Evidenz klaeren soll."""
    terms = []
    try:
        rows = q("""SELECT th.keywords, COUNT(dt.doc_id) n,
                           AVG(COALESCE(d.relevance,0.5)) avgrel
                    FROM themes th
                    LEFT JOIN doc_themes dt ON dt.theme_id=th.id
                    LEFT JOIN documents d ON d.id=dt.doc_id
                    WHERE th.excluded=0
                    GROUP BY th.id
                    HAVING n BETWEEN 1 AND 5 AND avgrel BETWEEN 0.4 AND 0.6
                    ORDER BY RANDOM() LIMIT 20""")
        for r in rows:
            kws = json.loads(r["keywords"])
            if kws and kws[0] not in terms:
                terms.append(kws[0])
    except Exception:
        pass
    return terms

def _terms_for(src):
    """Liefert die Suchbegriffe passend zum Modus des aktuellen Ticks.
    discovery -> breite neutrale Anker (was gibt es?);
    clarify   -> Begriffe aus unsicheren Themen (was davon klaert sich?)."""
    if src.get("_mode") == "clarify":
        t = _uncertain_terms()
        if t:
            return t
    return broad_anker()

# Rueckwaertskompatibler Name (einige Ernter rufen _search_terms ohne src).
def _search_terms():
    return broad_anker()

def _get(url, timeout=25, headers=None):
    h = {"User-Agent": UA_CONTACT}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")

def _get_json(url, timeout=25):
    return json.loads(_get(url, timeout))

def _friendly_err(e):
    """Uebersetzt haeufige Netzfehler in verstaendliche Hinweise."""
    s = str(e)
    if "429" in s:
        return ("429 Rate-Limit: Diese Quelle ist gerade gedrosselt. Beim manuellen "
                "Test wird das Budget umgangen — im Dauerbetrieb verhindert das "
                "Rate-Budget das. Kurz warten und erneut testen.")
    if "404" in s:
        return "404 Nicht gefunden: URL/Endpoint stimmt nicht (mehr). Endpoint prüfen."
    if "403" in s or "401" in s:
        return "403/401 Zugriff verweigert: evtl. Key noetig oder User-Agent-Kontakt setzen."
    if "timed out" in s or "timeout" in s.lower() or "10060" in s:
        return "Zeitueberschreitung: Quelle antwortet nicht — spaeter erneut versuchen."
    return s[:300]

# ==================================================================
#  FEED-PARSING  (robust: xml.etree statt Regex; Regex nur als Fallback)
# ==================================================================
# Lehre aus dem Best-in-Class-Abgleich: Regex-XML/RSS-Parsing verliert Eintraege
# STILL (CDATA, Namespaces, Entities) — ein malformter Eintrag matcht einfach
# nicht und verschwindet ohne Spur. ElementTree ist stdlib (keine neue
# Abhaengigkeit) und namespace-robust. Faellt XML unparsebar aus, greift der alte
# Regex-Pfad — die Umstellung kann also nur MEHR finden, nie weniger.

def _clean(s):
    """Whitespace kollabieren + HTML-Entities aufloesen."""
    return re.sub(r"\s+", " ", _html.unescape(s or "")).strip()

def _strip_html(s):
    """Tags entfernen, Entities aufloesen, Whitespace kollabieren."""
    return _clean(re.sub(r"<[^>]+>", " ", s or ""))

def _localtag(el):
    """Tag-Name ohne Namespace ({ns}tag -> tag), klein."""
    t = el.tag
    return (t.rsplit("}", 1)[-1] if isinstance(t, str) and "}" in t else t or "").lower()

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

def _norm_date(s):
    """Beliebiges Feed-Datum -> YYYY-MM-DD oder None. ISO / YYYYMMDD / RFC822."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    # RFC822: 'Mon, 02 Jan 2006 15:04:05 GMT'
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", s)
    if m:
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1))).isoformat()
            except ValueError:
                return None
    return None

def _child_text(el, *names):
    """Text des ERSTEN direkten Kindes mit passendem local-name (namespace-egal).
    itertext() zieht auch CDATA/verschachtelten Text sauber heraus."""
    want = {n.lower() for n in names}
    for c in list(el):
        if _localtag(c) in want:
            t = "".join(c.itertext()).strip()
            if t:
                return t
    return ""

def _child_link(el):
    """RSS <link>text</link> ODER Atom <link href=...>. Bevorzugt eine echte URL."""
    fallback = None
    for c in list(el):
        if _localtag(c) == "link":
            href = c.get("href")
            if href and href.strip():
                rel = (c.get("rel") or "alternate").lower()
                if rel == "alternate":
                    return href.strip()
                fallback = fallback or href.strip()
            elif (c.text or "").strip():
                fallback = fallback or c.text.strip()
    return fallback

def _feed_items(xml_text):
    """RSS-<item> UND Atom-<entry> aus beliebiger Feed-Struktur. None, wenn das
    XML gar nicht parsebar ist (dann Regex-Fallback beim Aufrufer)."""
    root = None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # Haeufigster Defekt: nicht deklarierte '&'-Entities -> maskieren + retry.
        try:
            fixed = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)",
                           "&amp;", xml_text)
            root = ET.fromstring(fixed)
        except ET.ParseError:
            return None
    return [el for el in root.iter() if _localtag(el) in ("item", "entry")]

def _parse_feed(xml_text, default_type="news", limit=30, text_cap=600):
    """RSS-2.0 ODER Atom robust -> [dict(source_type,title,text,url,published_at)].
    Bei unparsebarem XML: toleranter Regex-Fallback (Nicht-Regression)."""
    items = _feed_items(xml_text)
    if items is None:
        return _parse_feed_regex(xml_text, default_type, limit, text_cap)
    out = []
    for it in items:
        title = _clean(_child_text(it, "title"))
        if not title:
            continue
        desc = _strip_html(_child_text(it, "description", "summary", "content",
                                       "encoded", "subtitle"))
        pub = _norm_date(_child_text(it, "pubdate", "published", "updated",
                                     "date", "issued")) or TODAY()
        text = (title + " — " + desc) if desc and desc != title else title
        out.append(dict(source_type=default_type, title=title[:300],
                        text=text[:text_cap], url=_child_link(it), published_at=pub))
        if len(out) >= limit:
            break
    return out

def _edgar_context(s, kopf=""):
    """Baut aus den bereits vorhandenen EDGAR-FTS-`_source`-Feldern einen
    kontextreichen Text — statt nur des nackten Firmennamens. Kostet KEINE
    zusaetzliche Anfrage (alles ist schon in der Treffer-Antwort). Das war der
    Grund, warum 1c bei Funding/Events fast leer lief: reiner Name -> keine Fakten."""
    parts = list(s.get("display_names") or [])
    if s.get("file_description"):
        parts.append(str(s["file_description"]))
    sics = s.get("sics") or []
    if sics:
        parts.append("SIC " + ", ".join(str(x) for x in sics[:3]))
    staaten = s.get("inc_states") or s.get("biz_states") or []
    if staaten:
        parts.append("Sitz " + ", ".join(str(x) for x in staaten[:2]))
    body = _clean(" — ".join(str(p) for p in parts if p))
    return (kopf + " " + body).strip() if kopf else body

def _openalex_abstract(inv):
    """OpenAlex liefert den Abstract als inverted-index {wort: [positionen]} —
    zurueckbauen in Fliesstext. Wieder eine Gratis-Anreicherung (in der Antwort)."""
    if not isinstance(inv, dict) or not inv:
        return ""
    positions = []
    for word, idxs in inv.items():
        if isinstance(idxs, list):
            for i in idxs:
                if isinstance(i, int):
                    positions.append((i, word))
    positions.sort()
    return _clean(" ".join(w for _, w in positions))[:1200]

def _parse_feed_regex(xml_text, default_type="news", limit=30, text_cap=600):
    """Toleranter Fallback (nur wenn ElementTree scheitert). Bewusst grob."""
    out = []
    for m in re.finditer(r"<item>(.*?)</item>|<entry>(.*?)</entry>", xml_text, re.S):
        e = m.group(1) or m.group(2) or ""
        t = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", e, re.S)
        title = _clean(t.group(1)) if t else ""
        if not title:
            continue
        ds = re.search(r"<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>"
                       r"|<summary[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</summary>", e, re.S)
        desc = _strip_html(ds.group(1) or ds.group(2) or "") if ds else ""
        lk = re.search(r"<link[^>]*href=\"(.*?)\"|<link>(.*?)</link>", e, re.S)
        url = ((lk.group(1) or lk.group(2)).strip() if lk else None)
        d = re.search(r"<pubDate>(.*?)</pubDate>|<published>(.*?)</published>"
                      r"|<updated>(.*?)</updated>", e, re.S)
        pub = _norm_date(d.group(1) or d.group(2) or d.group(3)) if d else None
        text = (title + " — " + desc) if desc and desc != title else title
        out.append(dict(source_type=default_type, title=title[:300],
                        text=text[:text_cap], url=url, published_at=pub or TODAY()))
        if len(out) >= limit:
            break
    return out

# ------------------------------------------------------------------
# HARVESTER-VERTRAG:  fn(src, cursor) -> (docs, new_cursor)
#   cursor = None beim ersten Aufruf; danach das, was der Ernter zurueckgibt.
#   Paginierende Quellen (Wissenschaft) blaettern ueber viele Ticks tief in die
#   Historie; wenn eine Seite/ein Begriff erschoepft ist, springt der Cursor
#   zum naechsten Begriff. Nicht-paginierende (News) holen frisch (cursor=None).
# ------------------------------------------------------------------

# ---- Nicht-paginierend: holen frisch, Rate-Budget steuert die Frequenz ----
def h_gnews_topic(src, cursor):
    """Themenfeed: Google News mit FESTEM Suchbegriff (im Endpoint-Feld).
    Fuer Finanzthemen, die keinen freien RSS-Feed haben — Private Credit, M&A,
    Unternehmensfinanzierung, Zentralbank-Debatte. Der Begriff ist bewusst fix
    (nicht ontologiegesteuert): das Thema selbst IST der Auftrag."""
    kw = (src.get("endpoint") or "").strip()
    if not kw:
        raise RuntimeError("Themenfeed ohne Suchbegriff. Begriff ins Endpoint-Feld "
                           "eintragen, z.B.:  private credit OR direct lending")
    # englisch+global suchen (Finanznachrichten erscheinen zuerst auf Englisch)
    u = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(kw)
         + "&hl=en-US&gl=US&ceid=US:en")
    return _parse_feed(_get(u), "news", limit=20), {"t": TODAY()}

def h_gdelt(src, cursor):
    """Wirtschaftsnews. Enge, wirtschaftsspezifische Abfrage (weniger 429-Druck).
    Rate-Budget bewusst klein (siehe RATE_DEFAULTS)."""
    since = (date.today() - timedelta(days=14)).isoformat()
    kw = _terms_for(src)[0]
    # theme:ECON_* schraenkt GDELT auf wirtschaftlich getaggte Artikel ein.
    query = f'"{kw}" (theme:ECON_STOCKMARKET OR theme:ECON_SUBSIDIES OR ' \
            'theme:ECON_TRADE OR theme:MANUFACTURING OR theme:ECON_EARNINGSREPORT)'
    u = ("https://api.gdeltproject.org/api/v2/doc/doc?query="
         + urllib.parse.quote(query) + "&mode=artlist&maxrecords=50&format=json"
         + "&startdatetime=" + since.replace("-", "") + "000000")
    out = []
    for a in _get_json(u).get("articles", []):
        sd = a.get("seendate", "")
        pub = f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) >= 8 else TODAY()
        out.append(dict(source_type="news", title=(a.get("title") or "")[:300],
                        text=a.get("title"), url=a.get("url"), published_at=pub))
    return out, None

def _concept_terms(limit=12):
    """Schlagworte der Themen mit juengster Paper-Aktivitaet — damit die News-Suche
    gezielt fragt, ob Forschungssignale die Reifegrad-Leiter hochklettern."""
    d30 = (date.today() - timedelta(days=30)).isoformat()
    terms = []
    try:
        rows = q("""SELECT th.keywords, COUNT(*) recent FROM themes th
                    JOIN doc_themes dt ON dt.theme_id=th.id
                    JOIN documents d ON d.id=dt.doc_id
                    WHERE th.excluded=0 AND d.source_type='paper'
                      AND d.published_at > ?
                    GROUP BY th.id ORDER BY recent DESC LIMIT ?""", (d30, limit))
        for r in rows:
            kws = json.loads(r["keywords"])
            if kws and kws[0] not in terms:
                terms.append(kws[0])
    except Exception:
        pass
    return terms

def h_gnews(src, cursor):
    """Google News: suchgesteuert. Im Entdeckungs-Modus speist sich die Suche aus
    breiten Ankern UND aktuellen Forschungs-Konzepten (Reifegrad-Detektor:
    'wird dieses Paper-Thema gerade zum Ereignis?'). Ein Begriff pro Tick."""
    if src.get("_mode") == "clarify":
        terms = _terms_for(src)
    else:
        terms = broad_anker() + _concept_terms()     # Anker (GUI/DB-editierbar) + Forschungs-Konzepte
    if not terms:
        terms = broad_anker()
    ti = (cursor or {}).get("ti", 0) % len(terms)
    kw = terms[ti]
    u = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(kw)
         + "&hl=de&gl=DE&ceid=DE:de")
    return _parse_feed(_get(u), "news", limit=12), {"ti": ti + 1}

def h_guardian(src, cursor):
    """The Guardian Open Platform — nur wirtschaftsnahe Sektionen (weniger
    Kultur/Sport-Rauschen). Paginiert ueber Seiten (page im Cursor)."""
    page = (cursor or {}).get("page", 1)
    since = (date.today() - timedelta(days=365)).isoformat()
    # Nur relevante Ressorts: Wirtschaft, Technik, Umwelt, Wissenschaft, Geld.
    sections = "business|technology|environment|science|money|world"
    u = ("https://content.guardianapis.com/search?order-by=newest&page-size=50"
         + f"&page={page}&from-date=" + since
         + "&section=" + urllib.parse.quote(sections)
         + "&show-fields=trailText&api-key=test")
    resp = _get_json(u).get("response", {})
    out = []
    for r in resp.get("results", []):
        f = r.get("fields", {}) or {}
        trail = re.sub(r"<[^>]+>", " ", f.get("trailText", "") or "")
        out.append(dict(source_type="news",
                        title=(r.get("webTitle") or "")[:300],
                        text=(r.get("webTitle", "") + " — " + trail).strip(),
                        url=r.get("webUrl"),
                        published_at=(r.get("webPublicationDate") or TODAY())[:10]))
    pages = resp.get("pages", 1)
    new = {"page": page + 1} if page < min(pages, 40) else {"page": 1}
    return out, new

def h_edgar(src, cursor):
    """Form-D via EDGAR-Volltextsuche. Paginiert ueber 'from' (Cursor)."""
    frm = (cursor or {}).get("from", 0)
    since = (date.today() - timedelta(days=365)).isoformat()
    u = ("https://efts.sec.gov/LATEST/search-index?q=%22equity%22&forms=D"
         + f"&from={frm}&startdt=" + since + "&enddt=" + TODAY())
    out = []
    hits = _get_json(u).get("hits", {}).get("hits", [])
    for h in hits:
        s = h.get("_source", {})
        names = s.get("display_names") or []
        name = names[0] if names else (s.get("entity") or "Form D")
        text = _edgar_context(
            s, "Form D — Regulation-D-Frühfinanzierung (befreites Wertpapierangebot):")
        out.append(dict(source_type="funding", title=f"Form D: {name}"[:300],
                        text=text or name, url=None,
                        published_at=(s.get("file_date") or TODAY())))
    new = {"from": frm + len(hits)} if len(hits) >= 10 and frm < 300 else {"from": 0}
    return out, new

def h_edgar8k(src, cursor):
    """8-K via EDGAR-Volltextsuche — das US-Aequivalent zur Ad-hoc-Publizitaet
    (Pflichtmeldung boersennotierter US-Firmen bei wesentlichen Ereignissen:
    Uebernahmen, Fuehrungswechsel, Grossvertraege, Delisting, Insolvenz).
    Ereignis-Signal. Paginiert ueber 'from'. Neueste zuerst."""
    frm = (cursor or {}).get("from", 0)
    since = (date.today() - timedelta(days=30)).isoformat()   # Ereignisse = frisch
    # Wirtschaftlich gehaltvolle 8-K ueber Suchbegriff eingrenzen.
    kw = _terms_for(src)[0]
    u = ("https://efts.sec.gov/LATEST/search-index?q="
         + urllib.parse.quote(f'"{kw}"') + "&forms=8-K"
         + f"&from={frm}&startdt=" + since + "&enddt=" + TODAY())
    out = []
    hits = _get_json(u).get("hits", {}).get("hits", [])
    for h in hits:
        s = h.get("_source", {})
        names = s.get("display_names") or []
        name = names[0] if names else (s.get("entity") or "8-K")
        # Link auf das Filing zusammenbauen (falls Kennungen vorhanden)
        cik = (s.get("cik") or [None])[0] if isinstance(s.get("cik"), list) else s.get("cik")
        adsh = h.get("_id", "").split(":")[0].replace("-", "")
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
               f"&type=8-K" if cik else None)
        text = _edgar_context(
            s, "8-K — US-Pflichtmeldung eines wesentlichen Unternehmensereignisses:")
        out.append(dict(source_type="news",             # Ereignis-Signal
                        title=f"8-K: {name}"[:300], text=text or name, url=url,
                        published_at=(s.get("file_date") or TODAY())))
    new = {"from": frm + len(hits)} if len(hits) >= 10 and frm < 200 else {"from": 0}
    return out, new

def h_rss(src, cursor):
    """Generischer RSS/Atom-Ernter. Feeds tragen nur Frisches -> kein Cursor.
    Robustes ElementTree-Parsing (Regex nur als Fallback bei kaputtem XML)."""
    st = src.get("source_type") or "news"
    return _parse_feed(_get(src["endpoint"]), st, limit=30), None

# ---- Paginierend: blaettern tief in die Historie (Cursor je Quelle) ----
def h_openalex(src, cursor):
    """OpenAlex, echte Cursor-Paginierung. Nur Papers im Zeitfenster
    (from_publication_date) — kein Abtauchen in die Vergangenheit."""
    terms = _terms_for(src)
    cur = cursor or {"ti": 0, "c": "*"}
    ti = cur.get("ti", 0) % len(terms); kw = terms[ti]
    u = ("https://api.openalex.org/works?search=" + urllib.parse.quote(kw)
         + "&per-page=50&sort=publication_date:desc&cursor="
         + urllib.parse.quote(cur.get("c", "*")))
    ws = _window_start()
    if ws:
        u += "&filter=from_publication_date:" + ws
    data = _get_json(u)
    out = []
    for w in data.get("results", []):
        title = w.get("display_name")
        if not title:
            continue
        abstract = _openalex_abstract(w.get("abstract_inverted_index"))
        text = (title + " — " + abstract) if abstract else title
        out.append(dict(source_type="paper", title=title[:300], text=text[:1500],
                        url=w.get("id"),
                        published_at=w.get("publication_date") or TODAY()))
    nxt = (data.get("meta") or {}).get("next_cursor")
    new = {"ti": ti, "c": nxt} if nxt else {"ti": ti + 1, "c": "*"}
    return out, new

def h_semanticscholar(src, cursor):
    """Semantic Scholar Bulk-Suche mit Fortsetzungs-Token. Blaettert je Begriff."""
    terms = _terms_for(src)
    cur = cursor or {"ti": 0, "tok": None}
    ti = cur.get("ti", 0) % len(terms); kw = terms[ti]
    u = ("https://api.semanticscholar.org/graph/v1/paper/search/bulk?query="
         + urllib.parse.quote(kw) + "&fields=title,abstract,year,publicationDate,"
         "externalIds&sort=publicationDate:desc")
    ws = _window_start()
    if ws:
        # Datumsfenster: nur frische Papers (war die Hauptquelle der Flut)
        u += "&publicationDateOrYear=" + ws + ":"
    if cur.get("tok"):
        u += "&token=" + urllib.parse.quote(cur["tok"])
    data = _get_json(u)
    out = []
    for p in data.get("data", []) or []:
        title = p.get("title") or ""
        if not title:
            continue
        pub = p.get("publicationDate") or (f"{p['year']}-01-01" if p.get("year") else TODAY())
        doi = (p.get("externalIds") or {}).get("DOI")
        abstr = (p.get("abstract") or "")[:400]
        out.append(dict(source_type="paper", title=title[:300],
                        text=(title + " — " + abstr) if abstr else title,
                        url=f"https://doi.org/{doi}" if doi else None,
                        published_at=str(pub)[:10]))
    tok = data.get("token")
    new = {"ti": ti, "tok": tok} if tok else {"ti": ti + 1, "tok": None}
    return out, new

def h_europepmc(src, cursor):
    """Europe PMC mit cursorMark-Paginierung. Blaettert je Begriff tief."""
    terms = _terms_for(src)
    cur = cursor or {"ti": 0, "m": "*"}
    ti = cur.get("ti", 0) % len(terms); kw = terms[ti]
    ws = _window_start()
    query = kw + (f' AND (FIRST_PDATE:[{ws} TO {TODAY()}])' if ws else "")
    u = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="
         + urllib.parse.quote(query) + "&format=json&pageSize=50&sort=P_PDATE_D%20desc"
         + "&cursorMark=" + urllib.parse.quote(cur.get("m", "*")))
    data = _get_json(u)
    out = []
    for r in data.get("resultList", {}).get("result", []):
        title = r.get("title") or ""
        if not title:
            continue
        pub = (r.get("firstPublicationDate") or r.get("pubYear") or TODAY())
        if len(str(pub)) == 4:
            pub = f"{pub}-01-01"
        out.append(dict(source_type="paper", title=title[:300], text=title,
                        url=(f"https://europepmc.org/article/"
                             f"{r.get('source','MED')}/{r.get('id','')}")
                            if r.get("id") else None,
                        published_at=str(pub)[:10]))
    nm = data.get("nextCursorMark")
    new = ({"ti": ti, "m": nm} if nm and nm != cur.get("m")
           else {"ti": ti + 1, "m": "*"})
    return out, new

def h_arxiv(src, cursor):
    """arXiv, Offset-Paginierung (start). Nur frische Einreichungen im Fenster.
    WICHTIG (Signal-Anreicherung): das <summary> = ABSTRACT wird mit-geerntet und
    an den Titel gehaengt. Vorher war der Text nur der Titel — 1c hatte fast nichts
    zu extrahieren. Der Abstract kommt in DERSELBEN Antwort, kostet also nichts."""
    start = (cursor or {}).get("start", 0)
    cats = "cat:econ.GN OR cat:q-fin.GN OR cat:cs.AI OR cat:eess.SY"
    ws = _window_start()
    if ws:
        # arXiv erwartet submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]
        a = ws.replace("-", "") + "0000"
        b = TODAY().replace("-", "") + "2359"
        cats = f"({cats}) AND submittedDate:[{a} TO {b}]"
    u = ("http://export.arxiv.org/api/query?search_query="
         + urllib.parse.quote(cats)
         + f"&start={start}&max_results=50&sortBy=submittedDate&sortOrder=descending")
    out = []
    for it in (_feed_items(_get(u)) or []):
        title = _clean(_child_text(it, "title"))
        if not title or title.lower().startswith("arxiv query"):
            continue
        abstract = _clean(_child_text(it, "summary"))       # <- der Abstract
        pub = _norm_date(_child_text(it, "published")) or TODAY()
        link = _child_link(it)
        text = (title + " — " + abstract) if abstract else title
        out.append(dict(source_type="paper", title=title[:300],
                        text=text[:1500], url=link, published_at=pub))
    new = {"start": start + 50} if len(out) >= 40 and start < 3000 else {"start": 0}
    return out, new

def h_core(src, cursor):
    """CORE Volltext-Aggregator (Key noetig). Offset-Paginierung je Begriff."""
    ck = _core_key()
    if not ck:
        raise RuntimeError("Kein CORE-Key. In  core_key.txt  ablegen "
                           "(kostenlos: core.ac.uk/services/api).")
    terms = _terms_for(src)
    cur = cursor or {"ti": 0, "off": 0}
    ti = cur.get("ti", 0) % len(terms); kw = terms[ti]; off = cur.get("off", 0)
    u = ("https://api.core.ac.uk/v3/search/works?q=" + urllib.parse.quote(kw)
         + f"&limit=25&offset={off}&sort=publishedDate:desc")
    req = urllib.request.Request(u, headers={"Authorization": f"Bearer {ck}",
                                             "User-Agent": UA_CONTACT})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    out = []
    for w in data.get("results", []):
        title = w.get("title") or ""
        if not title:
            continue
        pub = (w.get("publishedDate") or w.get("createdDate") or TODAY())[:10]
        out.append(dict(source_type="paper", title=title[:300],
                        text=(title + " — " + (w.get("abstract") or "")[:300]).strip(),
                        url=w.get("downloadUrl") or (w.get("links") or [{}])[0].get("url"),
                        published_at=pub))
    new = ({"ti": ti, "off": off + 25} if len(out) >= 25 and off < 500
           else {"ti": ti + 1, "off": 0})
    return out, new

def _patents_key():
    direct = cfg("patents_api_key")
    if direct:
        return direct
    cfg_path = cfg("patents_api_key_file")
    if cfg_path and os.path.exists(cfg_path):
        k = _read_key_file(cfg_path)
        if k:
            return k
    for p in (os.path.join(HERE, "patents_key.txt"),
              os.path.join(HERE, "PatentsView Key", "PatentsView Key.txt")):
        if os.path.exists(p):
            k = _read_key_file(p)
            if k:
                return k
    return None

def h_patents(src, cursor):
    """PatentsView (USPTO) NEUE API (search.patentsview.org/api/v1). Braucht
    einen API-Key (X-Api-Key), anzufordern auf search.patentsview.org.
    Die Reifestufe Paper->[PATENT]->Funding->News. Paginiert je Begriff mit
    'after' (Cursor auf patent_id). Endpoint in GUI anpassbar."""
    import json as _json
    key = _patents_key()
    if not key:
        raise RuntimeError("Kein PatentsView-API-Key. Anfordern auf "
                           "search.patentsview.org ('Request an API Key'), dann in "
                           "config.txt als  patents_api_key = ...  eintragen.")
    terms = _terms_for(src)
    cur = cursor or {"ti": 0, "after": None}
    ti = cur.get("ti", 0) % len(terms); kw = terms[ti]
    base = (src.get("endpoint") or "https://search.patentsview.org/api/v1/patent/")
    q_obj = {"_and": [
        {"_text_any": {"patent_title": kw}},
        {"_gte": {"patent_date": (date.today() - timedelta(days=1095)).isoformat()}}]}
    opts = {"size": 25}
    if cur.get("after"):
        opts["after"] = cur["after"]
    params = ("?q=" + urllib.parse.quote(_json.dumps(q_obj))
              + "&f=" + urllib.parse.quote(_json.dumps(
                  ["patent_id", "patent_title", "patent_date", "patent_abstract"]))
              + "&o=" + urllib.parse.quote(_json.dumps(opts))
              + "&s=" + urllib.parse.quote(_json.dumps([{"patent_id": "asc"}])))
    req = urllib.request.Request(base + params,
              headers={"X-Api-Key": key, "User-Agent": UA_CONTACT})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = _json.load(r)
    out = []
    last_id = None
    for p in data.get("patents", []) or []:
        title = p.get("patent_title") or ""
        last_id = p.get("patent_id") or last_id
        if not title:
            continue
        abstr = (p.get("patent_abstract") or "")[:300]
        num = p.get("patent_id")
        out.append(dict(source_type="patent", title=title[:300],
                        text=(title + " — " + abstr) if abstr else title,
                        url=f"https://patents.google.com/patent/US{num}" if num else None,
                        published_at=(p.get("patent_date") or TODAY())[:10]))
    # 'after' = letzte patent_id dieser Seite; leer/erschoepft -> naechster Begriff
    new = ({"ti": ti, "after": last_id} if len(out) >= 25 and last_id
           else {"ti": ti + 1, "after": None})
    return out, new

def _core_key():
    direct = cfg("core_api_key")
    if direct:
        return direct
    cfg_path = cfg("core_api_key_file")
    if cfg_path and os.path.exists(cfg_path):
        k = _read_key_file(cfg_path)
        if k:
            return k
    for p in (os.path.join(HERE, "core_key.txt"),
              os.path.join(HERE, "CORE Key", "CORE Key.txt")):
        if os.path.exists(p):
            k = _read_key_file(p)
            if k:
                return k
    return None

# ---- EPO OPS: europaeische/internationale Patente (OAuth2, XML) ----
_EPO_TOKEN = {"value": None, "expires": 0}

def _epo_creds():
    """Consumer-Key/Secret aus config.txt (direkt oder ueber Dateipfade).
    Gibt (key, secret, grund) — grund erklaert praezise, WAS fehlt, statt
    pauschal 'registrier dich' zu melden (irrefuehrend, wenn nur der Pfad
    einen Tippfehler hat)."""
    key = cfg("epo_consumer_key")
    sec = cfg("epo_consumer_secret")
    if key and sec:
        return key, sec, ""
    kp, sp = cfg("epo_consumer_key_file"), cfg("epo_consumer_secret_file")
    if not kp and not sp:
        return None, None, ("keine EPO-Zugangsdaten in config.txt "
                            "(epo_consumer_key/-secret oder die _file-Pfade). "
                            "Kostenlos: developers.epo.org")
    if not kp or not sp:
        fehlt = "epo_consumer_key_file" if not kp else "epo_consumer_secret_file"
        return None, None, f"{fehlt} fehlt in config.txt — es braucht BEIDE Pfade"
    for p, nm in ((kp, "Key"), (sp, "Secret")):
        if not os.path.exists(p):
            return None, None, f"{nm}-Datei nicht gefunden: {p}  (Pfad pruefen!)"
    k, s = _read_key_file(kp), _read_key_file(sp)
    if not k or not s:
        leer = kp if not k else sp
        return None, None, f"Datei ist leer: {leer}"
    return k, s, ""

def _epo_token():
    """OAuth2-Token holen/cachen (gilt ~20 min). Base64(key:secret) -> Bearer."""
    now = time.time()
    if _EPO_TOKEN["value"] and now < _EPO_TOKEN["expires"] - 30:
        return _EPO_TOKEN["value"]
    key, sec, grund = _epo_creds()
    if not key or not sec:
        raise RuntimeError("EPO: " + grund)
    import base64
    basic = base64.b64encode(f"{key}:{sec}".encode()).decode()
    body = b"grant_type=client_credentials"
    req = urllib.request.Request(
        "https://ops.epo.org/3.2/auth/accesstoken", data=body,
        headers={"Authorization": "Basic " + basic,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    _EPO_TOKEN["value"] = data.get("access_token")
    _EPO_TOKEN["expires"] = now + int(data.get("expires_in", 1200))
    return _EPO_TOKEN["value"]

def h_epo(src, cursor):
    """EPO Open Patent Services: internationale Patente (EPO/WIPO/national via
    INPADOC). CQL-Suche ueber Titel, neueste zuerst. Paginiert je Begriff ueber
    'Range' (25er-Fenster). XML-Antwort. Braucht kostenlose EPO-Zugangsdaten."""
    terms = _terms_for(src)
    cur = cursor or {"ti": 0, "start": 1}
    ti = cur.get("ti", 0) % len(terms); kw = terms[ti]; start = cur.get("start", 1)
    token = _epo_token()
    # CQL: Titel-Volltext. Range=start-ende (max 25 pro Aufruf, max 2000 gesamt).
    end = start + 24
    cql = urllib.parse.quote(f'ti="{kw}"')
    url = (f"https://ops.epo.org/3.2/rest-services/published-data/search/biblio"
           f"?q={cql}&Range={start}-{end}")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                               "Accept": "application/xml",
                                               "User-Agent": UA_CONTACT})
    with urllib.request.urlopen(req, timeout=30) as r:
        xml = r.read().decode("utf-8", "ignore")
    out = []
    # Jedes Patent ist ein <exchange-document> mit country/doc-number/kind + Titel.
    for m in re.finditer(r"<exchange-document\b[^>]*"
                         r'country="([^"]*)"[^>]*doc-number="([^"]*)"[^>]*'
                         r'kind="([^"]*)"(.*?)</exchange-document>', xml, re.S):
        country, num, kind, block = m.groups()
        # Titel: bevorzugt Englisch, sonst der erste vorhandene.
        t = (re.search(r'<invention-title[^>]*lang="en"[^>]*>(.*?)</invention-title>',
                       block, re.S)
             or re.search(r"<invention-title[^>]*>(.*?)</invention-title>", block, re.S))
        title = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
        if not title:
            continue
        d = re.search(r"<date>(\d{8})</date>", block)
        pub = f"{d.group(1)[:4]}-{d.group(1)[4:6]}-{d.group(1)[6:8]}" if d else TODAY()
        pnum = f"{country}{num}{kind}"
        out.append(dict(source_type="patent", title=title[:300], text=title,
                        url=f"https://worldwide.espacenet.com/patent/search?q={pnum}",
                        published_at=_valid_date(pub)))
    # total-result-count steuert, ob weiterblaettern lohnt
    tot = re.search(r'total-result-count="(\d+)"', xml)
    total = int(tot.group(1)) if tot else 0
    new = ({"ti": ti, "start": end + 1} if end < min(total, 500) and out
           else {"ti": ti + 1, "start": 1})
    return out, new

_DEMO = [
 ("news", "Chip suppliers warn of substrate bottleneck into 2027",
  "semiconductor substrate shortage supply"),
 ("patent", "Method for high-density copper interconnect recycling",
  "copper recycling interconnect patent"),
 ("funding", "Form D: VoltGrid raises $120M for grid-scale storage",
  "grid storage battery funding"),
 ("paper", "Forecasting electricity demand under datacenter expansion",
  "datacenter electricity demand model"),
 ("news", "Adhoc: Maschinenbauer meldet Grossauftrag fuer Verteidigungselektronik",
  "defense electronics order adhoc"),
 ("paper", "Perovskite tandem stability under field conditions",
  "perovskite tandem solar stability"),
 ("funding", "Form D: HelioTandem raises $75M for perovskite pilot line",
  "perovskite tandem manufacturing"),
 ("news", "Perovskite tandem panels win first utility tender",
  "perovskite tandem solar utility"),
 ("patent", "Encapsulation stack for perovskite tandem modules",
  "perovskite tandem encapsulation patent"),
 ("news", "Freight rates spike as canal transits drop",
  "shipping logistics freight rates"),
]
_demo_i = {"i": 0}

def h_demo(src, cursor):
    out = []
    for _ in range(3):
        st, title, text = _DEMO[_demo_i["i"] % len(_DEMO)]
        _demo_i["i"] += 1
        out.append(dict(source_type=st, title=f"{title} [{_demo_i['i']}]",
                        text=text, url=None, published_at=TODAY()))
    return out, None

HARVESTERS = {"gdelt": h_gdelt, "gnews": h_gnews, "guardian": h_guardian,
              "gnews_topic": h_gnews_topic,
              "openalex": h_openalex, "arxiv": h_arxiv, "europepmc": h_europepmc,
              "semanticscholar": h_semanticscholar, "core": h_core,
              "patents": h_patents, "epo": h_epo,
              "edgar": h_edgar, "edgar8k": h_edgar8k, "rss": h_rss, "demo": h_demo}

# Rate-Budget-Vorgaben je Art (Abfragen pro Stunde) — Basiswert. Der
# Aufmerksamkeits-Regler (siehe unten) skaliert Paper- und News-Seite dynamisch.
RATE_DEFAULTS = {"gdelt": 2, "gnews": 15, "guardian": 12, "edgar": 8, "edgar8k": 10,
                 "rss": 8, "openalex": 30, "semanticscholar": 30, "europepmc": 20,
                 "arxiv": 20, "core": 20, "patents": 20, "epo": 20, "demo": 60,
                 "gnews_topic": 4}

# Signalseiten fuer den Regler: Forschungsseite (frueh) vs. Ereignisseite (spaet).
PAPER_KINDS = {"openalex", "arxiv", "europepmc", "semanticscholar", "core"}
NEWS_KINDS = {"gnews", "guardian", "gdelt", "rss", "edgar8k", "gnews_topic"}

# ==================================================================
#  RELEVANZ  (LLM in kleinen Batches; Mock: Wirtschafts-Heuristik)
# ==================================================================
TRUST_PRIOR = {"paper": 0.8, "patent": 0.85, "funding": 0.9, "news": 0.5}
# Breite Wirtschafts-/Technik-Heuristik (DE+EN) fuer den Mock-Modus.
_ECON = re.compile(
    r"invest|fund|market|price|demand|supply|capacit|energy|power|grid|chip|"
    r"semiconductor|substrate|battery|storage|solar|nuclear|reactor|hydrogen|"
    r"defen[sc]e|drone|robot|automat|manufactur|factory|production|shortage|"
    r"bottleneck|tariff|export|import|raise|series [a-e]|venture|patent|"
    r"electric|vehicle|copper|lithium|uranium|datacenter|data center|ai |"
    r"artificial intelligence|quantum|biotech|pharma|logistics|freight|"
    r"zoll|auftrag|umsatz|finanz|nachfrage|kapazit|engpass|lieferkette|"
    r"technolog|forschung|studie|markt|preis|energie|wasserstoff", re.I)

RELEVANCE_PROMPT = (
 "Du bist Analyst fuer ein OEKONOMISCHES Erwartungsmodell. Bewerte je Eintrag, "
 "wie relevant er fuer INVESTITIONEN, MAERKTE und WIRTSCHAFTLICHE Erwartungen ist "
 "(0.0 bis 1.0).\n"
 "HOCH (0.7-1.0): Kapazitaeten, Capex/Investitionen, Nachfrage/Angebot, Preise, "
 "Lieferketten/Engpaesse, Finanzierungsrunden, Regulierung mit Marktwirkung, "
 "Technologie kurz vor kommerzieller Reife, Unternehmens-/Branchenereignisse.\n"
 "MITTEL (0.4-0.6): Technologie-Grundlagen mit erkennbarem spaeteren Marktbezug.\n"
 "NIEDRIG (0.0-0.3): reine Grundlagenforschung OHNE Marktbezug (z.B. Molekular-"
 "biologie, Medizin-Studien, Materialphysik ohne Anwendung), Kultur, Sport, "
 "Lifestyle, Meinung, Sport, Unterhaltung, lokale Vermischtes.\n"
 "FRUEHSIGNAL-AXIOM (nicht abwerten!): Forschung/Paper mit PLAUSIBLEM spaeteren "
 "Wirtschaftspfad ist die FRUEHESTE Sprosse des Alpha-Signals — mindestens MITTEL "
 "(nicht niedrig). Nur Forschung OHNE jeden denkbaren Marktpfad faellt auf niedrig. "
 "Alpha entsteht VOR dem Konsens; ein zu scharfes Relevanz-Gate unterdrueckt genau "
 "das Fruehsignal.\n"
 "Beispiele: 'Autophagy in NK cells' -> 0.1 (Grundlagenbiologie). "
 "'NBA free agency' -> 0.0 (Sport). 'Chip substrate shortage' -> 0.9. "
 "'Startup raises $95M for battery pilot line' -> 0.9.\n"
 "Antworte NUR als JSON {\"ratings\":[0.x, ...]} mit genau so vielen Zahlen wie "
 "Eintraege. Eintraege: ")

def _relevanz_anker(max_n=None):
    """Jüngste, nach Score balancierte Hand-Korrekturen als (title, neu_score) —
    Few-Shot-Anker für den Relevanz-Prompt. Balance über niedrig/mittel/hoch, damit
    die Beispiele die ganze Skala aufspannen (nicht nur Jens' letzte drei Löschungen)."""
    if not cfg_bool("relevanz_lernen", True):
        return []
    max_n = max_n or cfg_int("relevanz_anker_n", 8)
    try:
        rows = q("""SELECT title, neu_score FROM relevanz_urteil
                    WHERE title IS NOT NULL AND TRIM(title) != ''
                    GROUP BY doc_id ORDER BY MAX(id) DESC LIMIT 60""")
    except Exception:
        return []
    eimer = {"hi": [], "mid": [], "lo": []}
    for r in rows:
        sc = r["neu_score"]
        if sc is None:
            continue
        key = "lo" if sc < 0.4 else "hi" if sc >= 0.7 else "mid"
        eimer[key].append((r["title"], round(float(sc), 2)))
    out = []
    while len(out) < max_n and any(eimer.values()):        # Round-robin = balanciert
        for key in ("hi", "lo", "mid"):
            if eimer[key] and len(out) < max_n:
                out.append(eimer[key].pop(0))
    return out

def lerne_relevanz(doc_id, neu):
    """Setzt die (Hand-)Relevanz eines Dokuments UND protokolliert die Korrektur als
    Label (nur bei echter Änderung >= 0.01). Rückgabe (alt, neu). Der Kern des
    Feedback-Loops — testbar getrennt von der GUI/HTTP-Schicht."""
    neu = max(0.0, min(1.0, float(neu)))
    cur = q("SELECT relevance, title, source_type FROM documents WHERE id=?", (doc_id,))
    alt = cur[0]["relevance"] if cur else None
    q("UPDATE documents SET relevance=? WHERE id=?", (neu, doc_id), fetch=False)
    if (cfg_bool("relevanz_lernen", True) and cur
            and (alt is None or abs((alt or 0.0) - neu) >= 0.01)):
        q("INSERT INTO relevanz_urteil(doc_id,title,source_type,alt_score,neu_score) "
          "VALUES(?,?,?,?,?)", (doc_id, cur[0]["title"], cur[0]["source_type"], alt, neu),
          fetch=False)
    return alt, neu

def _relevanz_prompt():
    """RELEVANCE_PROMPT + (falls vorhanden) Jens' Kalibrier-Anker. Kalibriert das
    Relevanz-GATE an sein tatsächliches Urteil — kein Konsens-/Dichtefilter."""
    anker = _relevanz_anker()
    if not anker:
        return RELEVANCE_PROMPT
    zeilen = "\n".join(f'- "{(t or "")[:120]}" -> {sc}' for t, sc in anker)
    return (RELEVANCE_PROMPT
            + "\nKALIBRIERUNG — so hat der Kurator vergleichbare Einträge per Hand "
              "bewertet; richte deine Skala danach aus:\n" + zeilen + "\n")

def evaluate(docs):
    # SCHUTZ VOR DATENVERGIFTUNG: Ist das Modell nicht erreichbar, wird NICHT
    # per Mock bewertet (das erzeugte in der Vergangenheit wochenlang wertlose
    # Relevanzen). Stattdessen: Abbruch — der Aufrufer sammelt dann nichts.
    if ROUTING.get("relevance") == "local":
        ok, _ = local_available()
        if not ok and not FRONTIER["allowed"]:
            raise RuntimeError("Lokales Modell nicht erreichbar — es wird nicht "
                               "per Mock bewertet (schuetzt die Datenbank).")
    rel_prompt = _relevanz_prompt()          # einmal je Lauf (Anker aus Jens' Urteilen)
    for i in range(0, len(docs), 10):
        batch = docs[i:i+10]
        # Titel + Kurztext geben dem Modell mehr Kontext als der Titel allein.
        items = [((d["title"] or "") + (" — " + d["text"][:160]
                  if d.get("text") and d["text"] != d["title"] else ""))
                 for d in batch]
        res = ask_json("relevance", rel_prompt
                       + json.dumps(items, ensure_ascii=False), max_tokens=1500,
                       schema=RELEVANCE_SCHEMA)
        ratings = None
        if isinstance(res, dict) and isinstance(res.get("ratings"), list):
            ratings = res["ratings"]
        elif isinstance(res, list):
            ratings = [x.get("relevance") if isinstance(x, dict) else x for x in res]
        if ratings and len(ratings) == len(batch):
            for d, r in zip(batch, ratings):
                try: d["relevance"] = max(0.0, min(1.0, float(r)))
                except Exception: d["relevance"] = 0.5
        else:
            # Mock: Treffer -> 0.65; sonst 0.45 (knapp ueber Schwelle 0.4).
            for d in batch:
                blob = ((d["title"] or "") + " " + (d.get("text") or "")).lower()
                d["relevance"] = 0.65 if _ECON.search(blob) else 0.45
    for d in docs:
        d["trust"] = TRUST_PRIOR.get(d["source_type"], 0.5)
    return docs

# ==================================================================
#  ONTOLOGIE  (zuordnen + wachsen lassen)
# ==================================================================
def _lexicon():
    return {t["id"]: (t["name"], json.loads(t["keywords"]))
            for t in q("SELECT id,name,keywords FROM themes WHERE excluded=0")}

def _assign(doc_id, text):
    t = " " + (text or "").lower() + " "
    hit = False
    for tid, (_, kws) in _lexicon().items():
        if any(k.lower() in t for k in kws):
            q("INSERT OR IGNORE INTO doc_themes(doc_id,theme_id) VALUES(?,?)",
              (doc_id, tid), fetch=False)
            hit = True
    return hit

def grow_ontology(unassigned):
    """[(doc_id, titel+text)] -> neue Themen (Modell oder Bigram-Mock), zuordnen."""
    if len(unassigned) < 2:
        return 0
    texts = [t for _, t in unassigned][:15]
    new = None
    res = ask_json("ontology",
        "Diese Dokumente passen zu keinem bekannten Wirtschaftsthema. "
        "Erkenne 1-3 NEUE Themen fuer ein oekonomisches Erwartungsmodell. "
        "Antworte NUR als JSON der Form {\"themes\":[{\"name\":\"...\","
        "\"keywords\":[\"kw1\",\"kw2\",\"kw3\"]}]}. Halte Namen und Schlagworte KURZ. "
        "Dokumente: " + json.dumps(texts, ensure_ascii=False),
        max_tokens=2000, schema=ONTOLOGY_SCHEMA)   # Schema statt reiner Token-Puffer
    if isinstance(res, dict) and isinstance(res.get("themes"), list):
        new = res["themes"]
    elif isinstance(res, list):
        new = res
    if new is None:
        pairs = defaultdict(int)
        for t in texts:
            w = re.findall(r"[a-z][a-z\-]{3,}", t.lower())
            for a, b in zip(w, w[1:]):
                pairs[f"{a} {b}"] += 1
        new = [{"name": bg.title(), "keywords": [bg]}
               for bg, c in sorted(pairs.items(), key=lambda x: -x[1])[:1] if c >= 2]
    n = 0
    for t in new:
        try:
            q("INSERT INTO themes(name,keywords,created_by) VALUES(?,?,'machine')",
              (t["name"][:80], json.dumps(t["keywords"][:6])), fetch=False)
            log("ontologie", f"Neues Thema: {t['name']} {t['keywords'][:3]}")
            n += 1
        except Exception:
            pass
    if n:
        for doc_id, text in unassigned:
            _assign(doc_id, text)
    return n

# ==================================================================
#  SEMANTIK-DEDUP  (lokale Embeddings; NICHT-destruktiv)
# ==================================================================
# Best-in-Class-Nachrüstung #3: Exakt-Dedup (title+datum) verfehlt Wire-Republishes
# mit leicht variiertem Titel -> dieselbe Story zählt mehrfach und bläht Momentum/
# Konvergenz auf (genau das Rauschen, an dem die Retro-Empirie hängen blieb).
# Kosinus über ein kleines lokales Embed-Modell (freie Maschinenzeit). LÖSCHT NICHTS:
# ein Near-Dup wird nur mit dup_of=<kanonisch> markiert; das Zählen filtert
# dup_of IS NULL. Kein Embed-Modell -> lautloser No-Op (Signal bleibt korrekt).
_EMBED_ALARM = {"missing": False}

def _cosine(a, b):
    """Kosinus-Ähnlichkeit — delegiert an die geteilte Definition (`dedup_kern.kosinus`), byte-identisch
    zum Alt-Verhalten (Null-Norm/Dim-Mismatch -> 0). Dünne Hülle, damit bestehende Aufrufer weiterlaufen.
    Fehlt der geteilte Kern (Degradations-Pfad, QS-#6) -> 0.0 (Dedup aus, kein Absturz)."""
    return _dedup_kosinus(a, b) if _dedup_kosinus else 0.0

def embed_text(text):
    """Vektor via Ollama /api/embeddings. None, wenn Modell/Dienst weg —
    konsistent mit 'Stillstand statt Vergiftung' (kein Fantasie-Vektor)."""
    text = (text or "").strip()
    if not text:
        return None
    model = cfg("embed_model", "nomic-embed-text")
    try:
        body = json.dumps({"model": model, "prompt": text[:2000]}).encode()
        req = urllib.request.Request(LOCAL["url"] + "/api/embeddings", data=body,
                                     headers={"Content-Type": "application/json"})
        with _gpu_gate(model):                          # nomic — prozess-übergreifend gegen den 30b-Sammler gesperrt
            gpu_guard()
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
        vec = data.get("embedding")
        if isinstance(vec, list) and vec:
            _EMBED_ALARM["missing"] = False
            return [float(x) for x in vec]
    except urllib.error.HTTPError as e:
        if e.code == 404 and not _EMBED_ALARM["missing"]:
            _EMBED_ALARM["missing"] = True
            log("EMBED FEHLT", f"Ollama kennt Embed-Modell '{model}' nicht (404). "
                f"Holen mit:  ollama pull {model}  — bis dahin KEIN Semantik-Dedup.")
    except Exception as e:
        log("embed", f"Embedding-Fehler: {str(e)[:100]}")
    return None

def _near_dup_of(vec, source_type, published_at=None):
    """Kanonisches Dokument, zu dem `vec` ein Near-Dup ist (Kosinus >= Schwelle),
    sonst None. Blocking gegen O(N²): DESSELBEN source_type. QS-B4: zusätzlich ein
    ZEITfenster (dedup_fenster_tage) um published_at — bei hochfrequenten Quellen
    sind 400 Docs nur Stunden; so werden auch Republikationen 2-3 Tage später noch
    gefangen (statt nur die letzten N). published_at=None -> reines Mengen-Blocking."""
    if not vec or beste_uebereinstimmung is None:      # QS-#6: fehlt der geteilte Kern -> Dedup aus (No-Op)
        return None
    schwelle = cfg_float("dedup_schwelle", 0.92)
    limit = cfg_int("dedup_kandidaten", 500)
    fenster = cfg_int("dedup_fenster_tage", 21)
    sql = ("SELECT e.doc_id, e.vec FROM doc_embeddings e "
           "JOIN documents d ON d.id=e.doc_id "
           "WHERE d.source_type=? AND d.dup_of IS NULL")
    args = [source_type]
    if published_at and fenster > 0:
        try:
            mitte = date.fromisoformat(_valid_date(published_at))
            von = (mitte - timedelta(days=fenster)).isoformat()
            bis = (mitte + timedelta(days=fenster)).isoformat()
            sql += " AND d.published_at BETWEEN ? AND ?"; args += [von, bis]
        except Exception:
            pass
    sql += " ORDER BY e.doc_id DESC LIMIT ?"; args.append(limit)
    cand = q(sql, tuple(args))
    # Best-Match über die geteilte Definition (dedup_kern) — byte-identisch (Kandidaten in doc_id-DESC-
    # Reihenfolge, wie zuvor). Blocking (source_type + Zeitfenster + dup_of IS NULL) ist oben im SQL.
    kandidaten = []
    for c in cand:
        try:
            kandidaten.append((c["doc_id"], json.loads(c["vec"])))
        except Exception:
            continue
    return beste_uebereinstimmung(vec, kandidaten, schwelle)

def _store_embedding(doc_id, vec, model=None):
    """Speichert ein Embedding (leeres vec [] = 'kein Inhalt, geprüft'-Marker,
    matcht nie). Rückgabe True bei Erfolg — der Backfill braucht das, um einen
    stillen Endloslauf bei INSERT-Fehlern (Lock/Platte) zu erkennen."""
    try:
        q("INSERT OR REPLACE INTO doc_embeddings(doc_id,vec,model) VALUES(?,?,?)",
          (doc_id, json.dumps(vec), model or cfg("embed_model", "nomic-embed-text")),
          fetch=False)
        return True
    except Exception as e:
        log("embed", f"Embedding-Speichern fehlgeschlagen (doc {doc_id}): {str(e)[:80]}")
        return False

def dedup_dokument(doc_id, source_type, text, published_at=None):
    """Ingestions-Kern: embedde ein frisch gespeichertes Dokument; ist es einem
    kanonischen zu ähnlich -> dup_of setzen (Near-Dup), sonst sein Embedding als
    neuen Kanon speichern. Gibt die dup_of-id oder None. Nicht-destruktiv."""
    if not cfg_bool("dedup_aktiv", True):
        return None
    vec = embed_text(text)
    if not vec:
        return None
    dup = _near_dup_of(vec, source_type, published_at)
    if dup is not None:
        q("UPDATE documents SET dup_of=? WHERE id=?", (dup, doc_id), fetch=False)
        STATUS["deduped"] = STATUS.get("deduped", 0) + 1
        return dup
    _store_embedding(doc_id, vec)          # neuer Kanon
    return None

# --- Rückwärts-Dedup über den Bestand (resumierbar, nicht-destruktiv) ----------
DEDUP = {"running": False, "done": 0, "total": 0, "marked": 0,
         "phase": "bereit", "thread": None}

def _offene_embeddings():
    return q("""SELECT COUNT(*) c FROM documents d
                LEFT JOIN doc_embeddings e ON e.doc_id=d.id
                WHERE e.doc_id IS NULL AND d.dup_of IS NULL""")[0]["c"]

def _run_dedup_backfill(limit=None):
    """Bestands-Dedup: ÄLTESTE zuerst (stabiler Kanon), embedde Dokumente ohne
    Embedding, markiere Near-Dups. Resumierbar (Embeddings persistieren). Bricht
    sauber ab, wenn das Embed-Modell fehlt (kein Endloslauf, keine Vergiftung)."""
    if not any_model_active():
        DEDUP.update(running=False, phase="kein Modell aktiv")
        return
    DEDUP.update(running=True, done=0, marked=0, phase="läuft")
    DEDUP["total"] = min(_offene_embeddings(), limit) if limit else _offene_embeddings()
    log("dedup", f"Bestands-Dedup gestartet ({DEDUP['total']} Dokumente ohne Embedding).")
    done = 0
    kopf_vorher = None                         # Fortschritts-Wächter (QS Major 3)
    while DEDUP["running"]:
        batch = q("""SELECT d.id, d.source_type, d.title, d.text, d.published_at FROM documents d
                     LEFT JOIN doc_embeddings e ON e.doc_id=d.id
                     WHERE e.doc_id IS NULL AND d.dup_of IS NULL
                     ORDER BY d.id ASC LIMIT 50""")
        if not batch:
            break
        # Kein Fortschritt zwischen zwei Runden (INSERT-Lock/Platte voll) -> Abbruch
        # statt Endlos-Neu-Embedden desselben Dokuments.
        if batch[0]["id"] == kopf_vorher:
            DEDUP.update(running=False, phase="kein Fortschritt — gestoppt")
            log("dedup", "Backfill kommt nicht voran (Speicher/Lock?) — gestoppt.")
            break
        kopf_vorher = batch[0]["id"]
        for r in batch:
            if not DEDUP["running"] or (limit and done >= limit):
                break
            blob = ((r["title"] or "") + " " + (r["text"] or "")).strip()
            # QS Major 1: LEERES Dokument ist KEIN 'Modell weg' — als geprüft
            # markieren (leerer Vektor, matcht nie) und weiter, nicht anhalten.
            if not blob:
                if not _store_embedding(r["id"], []):
                    DEDUP.update(running=False, phase="Speichern fehlgeschlagen")
                    break
                done += 1; DEDUP["done"] = done
                continue
            vec = embed_text(blob)
            if vec is None:
                # QS Major 2: nur ein echtes Modell-Fehlen (404-Alarm) stoppt sauber;
                # ein transienter Blip pausiert ebenfalls (Resume beim nächsten Lauf) —
                # aber NICHT wegen leerer Dokumente (die sind oben abgefangen).
                grund = ("Embed-Modell fehlt" if _EMBED_ALARM["missing"]
                         else "Embed-Modell antwortet nicht (transient)")
                DEDUP.update(running=False, phase=f"{grund} — pausiert")
                log("dedup", f"{grund} — Backfill pausiert (kein Dokument fälschlich markiert).")
                break
            dup = _near_dup_of(vec, r["source_type"], r["published_at"])
            if dup is not None:
                q("UPDATE documents SET dup_of=? WHERE id=?", (dup, r["id"]), fetch=False)
                DEDUP["marked"] += 1
            elif not _store_embedding(r["id"], vec):
                DEDUP.update(running=False, phase="Speichern fehlgeschlagen")
                break
            done += 1
            DEDUP["done"] = done
            if done % 20 == 0:
                set_status("dedup", f"{done}/{DEDUP['total']} · "
                           f"{DEDUP['marked']} Near-Dups", busy=True)
        if limit and done >= limit:
            break
    if DEDUP["phase"] == "läuft":
        DEDUP["phase"] = "fertig"
    DEDUP["running"] = False
    log("dedup", f"Bestands-Dedup beendet: {done} geprüft, {DEDUP['marked']} Near-Dups markiert.")

def start_dedup(test_n=None):
    if DEDUP["running"]:
        return False
    def _safe():
        try:
            _run_dedup_backfill(test_n)
        except Exception as e:
            DEDUP.update(running=False, phase="Fehler")
            log("dedup", f"Backfill abgebrochen: {str(e)[:180]}")
    DEDUP["thread"] = threading.Thread(target=_safe, daemon=True)
    DEDUP["thread"].start()
    return True

# ==================================================================
#  DER DAUERSAMMLER  (Fliessprozess: Cursor + Rate-Budget, stoppbar)
# ==================================================================
# ==================================================================
#  AUFMERKSAMKEITS-REGLER  (folgt der inkrementellen Relevanz der News)
# ==================================================================
# Kein starres Mengenverhaeltnis, sondern signalgetrieben:
#  - News duenn        -> Papers hoch (liefern Suchbegriffe) + News hoch
#  - News reich & inkrementelle Relevanz STEIGT -> News laufen lassen (es passiert
#    gerade etwas), Papers drosseln
#  - News reich & inkrementelle Relevanz FAELLT -> News drosseln (durchgelaufen),
#    Papers hoch (zurueck zur Fruehsignalsuche)
# Bewusst traege (EMA-Glaettung) und sichtbar — die Relevanzbewertung ist noch
# grob, der Regler darf nicht auf Rauschen zappeln.
REGULATOR = {
    "enabled": cfg_bool("regulator_enabled", True),
    "paper_factor": 1.0, "news_factor": 1.0,   # aktuelle Stellgroessen (geglaettet)
    "incr_ema": 0.0,                            # geglaettete inkrementelle Relevanz
    "state": "neutral", "news_share": 0.0, "n_recent": 0,
    "alpha": cfg_float("regulator_smoothing", 0.3),   # EMA-Traegheit
    "every": cfg_int("regulator_every_ticks", 15),
}
_reg_i = {"n": 0}

def _side_avg_rel(kinds, recent):
    """Ø-Relevanz der News-/Paper-Seite: recent=True -> juengster Zufluss
    (nach ingested_at), sonst der aeltere Bestand. Fuer inkrementelle Relevanz."""
    if not kinds:
        return None, 0
    marks = ",".join("?" * len(kinds))
    # 'recent' = die letzten 300 nach Wissenszeit; 'bestand' = die 1500 davor
    if recent:
        rows = q(f"""SELECT d.relevance r FROM documents d JOIN sources s ON s.id=d.source_id
                     WHERE s.kind IN ({marks}) ORDER BY d.id DESC LIMIT 300""", tuple(kinds))
    else:
        rows = q(f"""SELECT d.relevance r FROM documents d JOIN sources s ON s.id=d.source_id
                     WHERE s.kind IN ({marks}) ORDER BY d.id DESC LIMIT 1800""", tuple(kinds))
        rows = rows[300:]        # ueberspringe den juengsten Block -> aelterer Bestand
    vals = [x["r"] for x in rows if x["r"] is not None]
    return (sum(vals) / len(vals) if vals else None), len(vals)

def update_regulator():
    """Rechnet die inkrementelle Relevanz der News-Seite und setzt die
    Stellgroessen (traege). Wird in fester Kadenz aufgerufen."""
    if not REGULATOR["enabled"]:
        REGULATOR["paper_factor"] = REGULATOR["news_factor"] = 1.0
        return
    a = REGULATOR["alpha"]
    # News-Anteil im juengsten Zufluss (duenn vs. dominant)
    rec_news = q(f"""SELECT COUNT(*) c FROM documents d JOIN sources s ON s.id=d.source_id
                     WHERE s.kind IN ({",".join("?"*len(NEWS_KINDS))})
                     AND d.id > (SELECT COALESCE(MAX(id),0)-500 FROM documents)""",
                 tuple(NEWS_KINDS))[0]["c"]
    rec_all = q("SELECT COUNT(*) c FROM documents WHERE id > "
                "(SELECT COALESCE(MAX(id),0)-500 FROM documents)")[0]["c"] or 1
    share = rec_news / rec_all
    REGULATOR["news_share"] = round(share, 3)
    REGULATOR["n_recent"] = rec_all
    # inkrementelle Relevanz der News: juengster Zufluss vs. Bestand
    r_new, n_new = _side_avg_rel(NEWS_KINDS, True)
    r_old, n_old = _side_avg_rel(NEWS_KINDS, False)
    incr = (r_new - r_old) if (r_new is not None and r_old is not None) else 0.0
    REGULATOR["incr_ema"] = round(a * incr + (1 - a) * REGULATOR["incr_ema"], 4)
    e = REGULATOR["incr_ema"]

    # Zustandsmaschine -> Ziel-Stellgroessen
    THIN, DOM = 0.15, 0.30       # News-Anteil-Schwellen
    RISE = 0.01                  # inkrementelle-Relevanz-Schwelle (geglaettet)
    if share < THIN:
        state, tp, tn = "news duenn -> Papers laden", 1.3, 1.4
    elif share > DOM and e > RISE:
        state, tp, tn = "news steigend -> dranbleiben", 0.5, 1.5
    elif share > DOM and e < -RISE:
        state, tp, tn = "news fallend -> zurueck zu Papers", 1.3, 0.4
    else:
        state, tp, tn = "neutral", 1.0, 1.0
    REGULATOR["state"] = state
    # Stellgroessen ebenfalls traege nachfuehren (kein Sprung)
    REGULATOR["paper_factor"] = round(a * tp + (1 - a) * REGULATOR["paper_factor"], 3)
    REGULATOR["news_factor"] = round(a * tn + (1 - a) * REGULATOR["news_factor"], 3)

def _reg_factor(kind):
    if kind in PAPER_KINDS:
        return REGULATOR["paper_factor"]
    if kind in NEWS_KINDS:
        return REGULATOR["news_factor"]
    return 1.0

REL_MIN = cfg_float("relevance_min", 0.4)
# Deckel je Abfrage: begrenzt DOKUMENTE (nicht nur Abfragen). Ohne das
# ueberrollen Bulk-Quellen (Semantic Scholar: ~458 Dok/Abfrage) alle anderen.
# ==================================================================
#  FILTER-KASKADE  (Kostenleiter: gratis -> billig -> teuer)
# ==================================================================
#  Stufe 1  formal      — ist es ueberhaupt Information?      (gratis)
#  Stufe 2  negativ     — ist es SICHER Ballast?              (gratis)
#  Stufe 3  Kanten-neu  — kennen WIR die Aussage schon?       (DB-Abfrage)
#  Stufe 4  Trivialitaet— kennt die WELT sie laengst?         (Modell)
# Grundsatz: keine Stufe behauptet zu wissen, was wertvoll IST — sie schliessen
# nur nacheinander aus, was es sicher NICHT ist. (Ein Positivfilter auf
# "oekonomische Dichte" waere anti-Alpha: er bevorzugt Texte, in denen die
# Bedeutung schon ausgesprochen ist — also spaete Signale.)

# --- Stufe 2: Negativliste (in config.txt editierbar) ----------------------
NEGATIV_DEFAULT = [
 # Sport
 "spielergebnis", "bundesliga", "champions league", "transfermarkt", "tabellenplatz",
 "world cup", "olympic games", "premier league", "nba", "nfl ",
 # Prominenz / Unterhaltung
 "promi", "celebrity", "gossip", "red carpet", "box office", "filmkritik",
 "album review", "streaming-tipps", "grammy", "oscar-verleihung",
 # Lebenshilfe / Konsum
 "horoskop", "horoscope", "rezept", "recipe", "diaet-tipps", "beauty-tipps",
 "mode-trend", "wohn-deko", "geschenktipps", "die 10 besten", "kaufberatung",
 "testsieger", "im test:", "produktvergleich",
 # Tourismus-Ratgeber (NICHT Tourismuswirtschaft!)
 "reisetipps", "schoenste straende", "hotelbewertung", "urlaubstipps",
 "staedtetrip", "travel guide",
 # Persoenliches / Lokales
 "nachruf", "obituary", "hochzeit von", "vermisstenmeldung", "verkehrsunfall",
 "gemeinderat", "stadtrat", "bebauungsplan",
 # Wissenschafts-Overhead (Formalia, kein Inhalt)
 "erratum", "corrigendum", "retraction note", "retraction notice",
 "call for papers", "conference announcement", "book review",
 "editorial board", "acknowledgement to reviewers", "author correction",
 # Konsens-Aggregate: fassen zusammen, was alle wissen -> null Distanz
 "systematic review", "literature review", "a survey of", "scoping review",
 "narrative review", "review article",
 # Forschung ueber Forschung
 "bibliometric", "scientometric", "citation analysis", "h-index",
 # Einzelfall-Medizin (Muster, nicht das Feld!)
 "case report", "a case of", "case series", "patient case",
 # Paedagogik/Didaktik (NICHT Bildungstrends!)
 "lehrplan", "curriculum design", "unterrichtsmethode", "teaching method",
 "classroom", "lernmaterial", "e-learning-kurs",
 # Religion (Fachbegriffe — NICHT 'religioes', sonst faellt Konflikt raus)
 "theologie", "theology", "liturgie", "liturgy", "predigt", "sermon",
 "kirchengemeinde", "bible study",
 # Geisteswissenschaft ohne Wirtschaftsbezug
 "kunstgeschichte", "art history", "literaturwissenschaft", "literary criticism",
 "archaeological excavation", "philology",
 # Betrieb
 "stellenanzeige", "job posting", "immobilieninserat", "gewinnspiel",
]

def _negativliste():
    """Aus config: negativ_filter = a; b; c   (ergaenzt die Standardliste).
    negativ_filter_ersetzt = true  -> NUR die eigene Liste."""
    eigene = [x.strip().lower() for x in (cfg("negativ_filter", "") or "").split(";")
              if x.strip()]
    if cfg_bool("negativ_filter_ersetzt", False):
        return eigene or NEGATIV_DEFAULT
    return NEGATIV_DEFAULT + eigene

_NEG_RE = {"re": None, "src": None}

def _neg_regex():
    """Muster mit Wortgrenzen — 'im test:' darf nicht 'Belastungstest' treffen."""
    liste = _negativliste()
    if _NEG_RE["src"] != liste:
        pats = [re.escape(x) for x in liste]
        _NEG_RE["re"] = re.compile(r"(?<![\w])(" + "|".join(pats) + r")", re.I)
        _NEG_RE["src"] = liste
    return _NEG_RE["re"]

def stufe2_negativ(doc):
    """Greift NUR im Titel (ein Nebensatz ueber Fussball macht ein
    Wirtschaftspapier nicht wertlos). Gibt das Treffermuster zurueck oder None."""
    m = _neg_regex().search(doc.get("title") or "")
    return m.group(0) if m else None

# --- Stufe 1: formale Substanz -------------------------------------------
def stufe1_formal(doc):
    """Inhaltlich NEUTRAL: bevorzugt kein Thema, keine Reifestufe — prueft nur,
    ob ueberhaupt Information da ist. (Aus einem Titel ohne Abstract kann 1c
    schwer Fakten ziehen — das erklaert einen Teil der Null-Ergebnisse.)"""
    titel = (doc.get("title") or "").strip()
    text = (doc.get("text") or "").strip()
    if len(titel) < 15:
        return "Titel zu kurz"
    körper = len(text) - len(titel)
    if körper < 30 and not doc.get("url"):
        return "kein Inhalt und keine Quelle"     # weder Text noch nachladbar
    return None

# --- Stufe 3: Kanten-Neuheit gegen den Fakten-Index -----------------------
_KANTEN = {"begriffe": set(), "paare": set(), "at": 0}

def _kanten_index():
    """Bekannte Begriffe und Begriffs-PAARE aus der facts-Tabelle (Proto-Graph).
    Solange der Index duenn ist, wirkt alles neu -> Stufe 3 haelt sich zurueck."""
    now = time.time()
    if now - _KANTEN["at"] < 600 and _KANTEN["begriffe"]:
        return _KANTEN
    b, p = set(), set()
    try:
        for r in q("SELECT subjekt, objekt FROM facts LIMIT 40000"):
            s = (r["subjekt"] or "").lower().strip()
            o = (r["objekt"] or "").lower().strip()
            if s: b.add(s)
            if o: b.add(o)
            if s and o: p.add((s, o))
    except Exception:
        pass
    _KANTEN.update(begriffe=b, paare=p, at=now)
    return _KANTEN

def stufe3_kanten_neu(doc, idx):
    """0..1: Wie viel am Dokument ist uns unbekannt? Misst Aussage-Neuheit
    (nicht Themen-Zugehoerigkeit): auch bei KI kann etwas Neues passieren."""
    if len(idx["begriffe"]) < 200:
        return 0.5                 # Index zu duenn -> neutral, nicht raten
    blob = ((doc.get("title") or "") + " " + (doc.get("text") or "")).lower()
    bekannt = sum(1 for b in idx["begriffe"] if len(b) > 4 and b in blob)
    if bekannt == 0:
        return 1.0                 # voellig unbekanntes Vokabular
    return max(0.0, 1.0 - min(1.0, bekannt / 8.0))

# --- Stufe 4: Trivialitaet + Einzelfall (Modell) --------------------------
TRIVIAL_PROMPT = (
 "Beurteile eine Aussage fuer ein Frueherkennungs-System fuer wirtschaftliche "
 "Erwartungen. Zwei Fragen:\n"
 "1) allgemeinwissen: Ist das laengst bekannt/selbstverstaendlich? "
 "(Beispiel: 'Pflanzen brauchen Licht' -> ja. 'Rechenzentren brauchen Strom' -> ja.)\n"
 "2) einzelfall: Beschreibt es einen EINZELFALL/eine Instanz (ein Sturm, ein "
 "Patient, eine Firmenmeldung ohne Musterbezug) statt eines MUSTERS/Trends "
 "(Haeufigkeit steigt, Durchbruch, Strukturwandel)?\n"
 "Einzelfaelle und Allgemeinwissen sind BEIDE wertlos: sie verschieben keine "
 "Erwartung. Antworte NUR als JSON: "
 "{\"allgemeinwissen\":true|false,\"einzelfall\":true|false,\"begruendung\":\"kurz\"}\n"
 "Aussage: ")

def stufe4_trivial(doc):
    """Fragt das Modell, ob die Aussage Allgemeinwissen ODER ein Einzelfall ist.
    Das Modell ist eine eingefrorene Konsens-Momentaufnahme — was es mit
    'weiss doch jeder' quittiert, hat null Distanz zum Konsens = null Alpha.
    Grenze: der Trainings-Stichtag. Etwas, das SEITHER Konsens wurde, haelt es
    faelschlich fuer neu. Deshalb nichts loeschen — nur als Spur ablegen."""
    txt = ((doc.get("title") or "") + ". " + (doc.get("text") or ""))[:600]
    res = ask_json("trivial", TRIVIAL_PROMPT + txt, max_tokens=200,
                   model=cfg("facts_model"), schema=TRIVIAL_SCHEMA)
    if not isinstance(res, dict):
        return None, None
    return bool(res.get("allgemeinwissen")), bool(res.get("einzelfall"))

MAX_DOCS_PER_QUERY = cfg_int("max_docs_per_query", 25)
# Anteil des Deckels, der ZUFAELLIG gezogen wird (statt nach Score). Verhindert,
# dass die Vorsortierung zu einer neuen Bestaetigungsschleife wird.
EXPLORE_SHARE = cfg_float("explore_share", 0.2)

def _neue_dokumente(batch):
    """Filtert Bekanntes RAUS, BEVOR teure Stufen laufen.
    Grund: flache Feeds (RSS, News) liefern bei jeder Abfrage fast dieselben
    ~20 Eintraege — gemessen 0,21 NEUE Dokumente je Abfrage, also ~99%
    Wiederholung. Frueher liefen die trotzdem durch Stufe 4 UND die Bewertung
    (zwei Modellaufrufe je Eintrag) und scheiterten erst am INSERT.
    Der Abgleich hier ist ein Index-Zugriff (title+published_at ist UNIQUE) —
    praktisch gratis."""
    if not batch:
        return batch, 0
    frisch, bekannt = [], 0
    for d in batch:
        t = d.get("title")
        p = _valid_date(d.get("published_at"))
        if not t:
            continue
        try:
            da = q("SELECT 1 FROM documents WHERE title=? AND published_at=? LIMIT 1",
                   (t, p))
            if da:
                bekannt += 1; continue
            sp = q("SELECT 1 FROM discarded WHERE title=? AND published_at=? LIMIT 1",
                   (t, p))
            if sp:
                bekannt += 1; continue      # schon einmal aussortiert -> nicht erneut pruefen
        except Exception:
            pass
        frisch.append(d)
    return frisch, bekannt

def _auswahl(batch, cap, src_name=""):
    """Filter-Kaskade auf einen zu grossen Fund. Stufen 1-3 (gratis/billig),
    dann Auswahl. Stufe 4 laeuft spaeter nur auf den Ueberlebenden (teuer).
    Ein Teil wird ZUFAELLIG gezogen: die Vorsortierung darf keine neue
    Bestaetigungsschleife werden — Zufall ist unverzerrt."""
    import random
    idx = _kanten_index()
    ueberlebende, raus = [], []
    for d in batch:
        g = stufe1_formal(d)
        if g:
            raus.append((d, "formal: " + g)); continue
        m = stufe2_negativ(d)
        if m:
            raus.append((d, "negativ: " + m)); continue
        ueberlebende.append((stufe3_kanten_neu(d, idx), d))
    if raus and src_name:
        log("kaskade", f"{src_name}: {len(raus)} vor-aussortiert "
                       f"(z.B. {raus[0][1]}), {len(ueberlebende)} bleiben.")
    for d, grund in raus:
        _spur(d, grund)                      # nichts verschwindet: ab in die Spur
    if len(ueberlebende) <= cap:
        return [d for _, d in ueberlebende], None
    ueberlebende.sort(key=lambda x: -x[0])
    n_zufall = max(1, int(cap * EXPLORE_SHARE))
    n_top = cap - n_zufall
    gewaehlt = [d for _, d in ueberlebende[:n_top]]
    rest = [d for _, d in ueberlebende[n_top:]]
    if rest:
        gewaehlt += random.sample(rest, min(n_zufall, len(rest)))
    schnitt = sum(s for s, _ in ueberlebende[:n_top]) / max(1, n_top)
    return gewaehlt, round(schnitt, 2)

def _spur(doc, grund):
    """Aussortiertes als schlanke Spur ablegen — reversibel, nachvollziehbar."""
    try:
        q("INSERT OR IGNORE INTO discarded(source_id,source_type,title,url,"
          "relevance,published_at) VALUES(?,?,?,?,?,?)",
          (None, doc.get("source_type"), doc.get("title"), doc.get("url"),
           None, _valid_date(doc.get("published_at"))), fetch=False)
    except Exception:
        pass
# Zeitfenster fuer Wissenschaftsquellen: nur FRISCHE Papers. Ein Paper von 2019
# ist kein Fruehsignal, sondern Geschichte — und die Tiefenblaetterei in die
# Vergangenheit war die Quelle der Paper-Flut. 0 = kein Fenster (alles).
PAPER_WINDOW_DAYS = cfg_int("paper_window_days", 7)

def _window_start():
    """Aelteste erlaubte Veroeffentlichung fuer Wissenschaftsquellen."""
    if PAPER_WINDOW_DAYS <= 0:
        return None
    return (date.today() - timedelta(days=PAPER_WINDOW_DAYS)).isoformat()

def _budget_remaining(src):
    """Verbleibende Abfragen im aktuellen Stundenfenster. Der effektive Deckel
    wird vom Aufmerksamkeits-Regler je Signalseite skaliert."""
    now = datetime.now()
    base = src["rate_per_hour"] or RATE_DEFAULTS.get(src["kind"], 20)
    cap = max(1, round(base * _reg_factor(src["kind"])))   # Regler-Skalierung
    ws = src["win_start"]
    if not ws:
        q("UPDATE sources SET win_start=?, queries_win=0 WHERE id=?",
          (now.isoformat(), src["id"]), fetch=False)
        return cap
    try:
        elapsed = (now - datetime.fromisoformat(ws)).total_seconds()
    except Exception:
        elapsed = 3601
    if elapsed >= 3600:
        q("UPDATE sources SET win_start=?, queries_win=0 WHERE id=?",
          (now.isoformat(), src["id"]), fetch=False)
        return cap
    return max(0, cap - (src["queries_win"] or 0))

def _next_source():
    """Waehlt die naechste faellige Quelle: aktiv, nicht pausiert, Budget frei —
    und am laengsten nicht abgefragt (hungrige zuerst = Rundlauf)."""
    now_iso = datetime.now().isoformat()
    elig = []
    for s in q("SELECT * FROM sources WHERE enabled=1"):
        if s["kind"] not in HARVESTERS:
            continue
        if s["paused_until"] and now_iso < s["paused_until"]:
            continue
        if _budget_remaining(s) <= 0:
            continue
        elig.append(s)
    if not elig:
        return None
    elig.sort(key=lambda s: s["last_tick_ts"] or "")   # laengste Wartezeit zuerst
    return elig[0]

def _valid_date(s):
    """Prueft/normalisiert published_at: gueltiges YYYY-MM-DD, nicht in der
    Zukunft. Zukunfts-/Formatfehler -> heute (Point-in-Time bleibt sauber)."""
    today = TODAY()
    if not s or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(s)):
        return today
    return s if s <= today else today

LOW_KEEP = cfg_float("trace_min", 0.2)   # darunter: ganz verwerfen

# --- Lernoffene Aufzeichnungsschicht: scraper.py -> aufzeichnung.db (home-verdrahtet, Task 8) ---
# SEPARATE aufzeichnung.db (WAL) — beschwert NIE die Single-Copy-scraper.db (QS-B4). FAIL-SAFE: jeder
# Aufzeichnungs-Fehler wird geschluckt und deaktiviert die Schicht, kippt aber NIE den Scraper.
# THREAD-SAFE: Sammler (_store_batch) UND 1c (_store_facts) schreiben -> ein Lock serialisiert.
# KEINE INSEL: ruft aufzeichnung.schreibe_dokument_roh/schreibe_fakt_attribut (der doppelt-QS'te Kern).
_AUFZ = {"conn": None, "extraktor_id": None, "run_id": None, "aus": False}
_AUFZ_LOCK = threading.Lock()

def _aufz_conn():
    """Lazy: oeffnet aufzeichnung.db, legt das Schema an, pinnt den Extraktor + eine run_id. Nur unter
    _AUFZ_LOCK aufrufen. Ein Fehler deaktiviert die Aufzeichnung dauerhaft (fail-safe), nie den Scraper."""
    if _AUFZ["conn"] is not None or _AUFZ["aus"]:
        return _AUFZ["conn"]
    try:
        import aufzeichnung, hashlib, datetime, time as _t
        pfad = os.path.join(HERE, "aufzeichnung.db")
        conn = sqlite3.connect(pfad, timeout=30, check_same_thread=False)
        aufzeichnung.schema_anlegen(conn)
        model = cfg("facts_model") or LOCAL.get("model") or "?"
        heute = datetime.date.today().isoformat()
        schema_hash = hashlib.sha256(json.dumps(FACTS_SCHEMA, sort_keys=True).encode()).hexdigest()[:12]
        eid = "facts:" + hashlib.sha256((model + "|" + schema_hash).encode()).hexdigest()[:12]
        conn.execute("INSERT OR IGNORE INTO extraktor_version(extraktor_id,modell,pin_datum,schema_version) "
                     "VALUES(?,?,?,?)", (eid, model, heute, schema_hash))
        rid = heute + ":" + hashlib.sha256(str(_t.time()).encode()).hexdigest()[:8]
        conn.execute("INSERT OR IGNORE INTO ingest_log(run_id,t_query,status) VALUES(?,?,?)", (rid, heute, "laeuft"))
        conn.commit()
        _AUFZ.update(conn=conn, extraktor_id=eid, run_id=rid)
        log("aufz", f"Aufzeichnungsschicht aktiv ({os.path.basename(pfad)}, extraktor {eid}, run {rid[:14]}).")
    except Exception as e:
        _AUFZ["aus"] = True
        try:
            log("aufz", f"Aufzeichnungsschicht deaktiviert (fail-safe): {str(e)[:120]}")
        except Exception:
            pass
    return _AUFZ["conn"]

def _aufz_dokument(d, did):
    """Ein bewertetes Dokument aufzeichnen (auch abgelehnt = lernbarer Nenner, QS-K3). `did`=documents.id
    (angenommen) ODER None (abgelehnt -> content-hash-id). Fail-safe/thread-safe.
    NAMENSRAUM (QS-Gemini-B1, bewusst): ANGENOMMENE Docs tragen `str(documents.id)` -> joinbar mit ihren
    `attribut`-Zeilen (die `_store_facts`/`_aufz_fakten` unter demselben doc_id ablegen). ABGELEHNTE Docs
    (`h:`<hash>) sind STANDALONE — sie haben KEINE Fakten (1c extrahiert nur angenommene), der Nenner zaehlt
    sie ueber das `angenommen`-Flag, NICHT ueber einen doc_id-Join. Kein Mischmasch-Join, keine verfaelschte
    Nenner-Zaehlung."""
    try:
        import aufzeichnung, datetime
        with _AUFZ_LOCK:
            conn = _aufz_conn()
            if conn is None:
                return
            doc_id = str(did) if did is not None else "h:" + aufzeichnung._content_hash(d.get("title"), d.get("text"))
            aufzeichnung.schreibe_dokument_roh(conn, doc_id, d, datetime.date.today().isoformat(),
                                               run_id=_AUFZ["run_id"])
    except Exception:
        pass

def _aufz_fakten(doc, facts):
    """Die extrahierten Fakt-Attribute eines Dokuments aufzeichnen (gepinnter Extraktor). Fail-safe/thread-safe."""
    if not facts:
        return
    try:
        import aufzeichnung, datetime
        with _AUFZ_LOCK:
            conn = _aufz_conn()
            if conn is None:
                return
            heute = datetime.date.today().isoformat()
            for f in facts:
                aufzeichnung.schreibe_fakt_attribut(conn, doc["id"], f, _AUFZ["extraktor_id"], heute)
    except Exception:
        pass


def _store_batch(src, batch):
    """Dreiband-Ablage:
      relevance >= REL_MIN      -> echtes Dokument
      LOW_KEEP <= rel < REL_MIN -> schlanke Spur (Titel/Quelle/Datum/URL) in
                                    'discarded' — spaeter wiederauffindbar
      rel < LOW_KEEP            -> ganz verworfen (klar irrelevant)
    Gibt (behaltene (id,text), seen, traced, dropped)."""
    kept, traced, dropped = [], 0, 0
    scored = evaluate(batch)
    for d in scored:
        rel = d["relevance"]
        if rel >= REL_MIN:
            did = None
            try:
                did = q("INSERT INTO documents(source_id,source_type,title,text,"
                        "url,relevance,trust,published_at) VALUES(?,?,?,?,?,?,?,?)",
                        (src["id"], d["source_type"], d["title"], d.get("text"),
                         d.get("url"), round(rel, 2), d["trust"],
                         _valid_date(d["published_at"])), fetch=False)
                blob = (d["title"] or "") + " " + (d.get("text") or "")
                # Semantik-Dedup: Near-Dups bleiben gespeichert (dup_of), gehen aber
                # NICHT in Ontologie/1c — nur der Kanon zählt.
                if dedup_dokument(did, d["source_type"], blob,
                                  _valid_date(d["published_at"])) is None:
                    kept.append((did, blob))
            except Exception:
                pass                                    # Duplikat (title+datum)
            _aufz_dokument(d, did)                       # Aufzeichnung: angenommenes Dokument (gepinnter Nenner)
        elif rel >= LOW_KEEP:
            try:
                q("INSERT INTO discarded(source_id,source_type,title,url,"
                  "relevance,published_at) VALUES(?,?,?,?,?,?)",
                  (src["id"], d["source_type"], d["title"], d.get("url"),
                   round(rel, 2), _valid_date(d["published_at"])), fetch=False)
                traced += 1
            except Exception:
                pass
            _aufz_dokument(d, None)                      # Aufzeichnung: borderline-abgelehnt = lernbarer Nenner (QS-K3)
        else:
            dropped += 1
    return kept, len(scored), traced, dropped

DISCOVERY_RATIO = cfg_float("discovery_ratio", 0.7)  # Anteil Entdeckungs-Ticks
_tick_i = {"n": 0}

def _pick_mode():
    """Hybrid: meist Entdeckung ('was gibt es?'), ein kleinerer Teil gezielte
    Klaerung unsicherer Themen. Deterministisch gemischt ueber den Tickzaehler."""
    _tick_i["n"] += 1
    # clarify nur, wenn es ueberhaupt unsichere Themen gibt
    if DISCOVERY_RATIO >= 1.0:
        return "discovery"
    every = max(2, round(1 / max(0.01, 1 - DISCOVERY_RATIO)))
    if _tick_i["n"] % every == 0 and _uncertain_terms():
        return "clarify"
    return "discovery"

def collect_tick():
    """EIN Schritt des Dauersammlers: eine faellige Quelle abfragen, Cursor
    weiterschreiben, Rate-Fenster hochzaehlen. Hybrid: Entdeckung + Klaerung."""
    row = _next_source()
    if not row:
        set_status("wartet", "alle Quellen im Rate-Limit oder pausiert", busy=False)
        return {"queried": None}
    src = dict(row)                          # Row -> dict (fuer .get und _mode)
    src["_mode"] = _pick_mode()
    fn = HARVESTERS[src["kind"]]
    cursor = None
    try:
        cursor = json.loads(src["cursor"]) if src["cursor"] else None
    except Exception:
        cursor = None
    modelabel = "Entdeckung" if src["_mode"] == "discovery" else "Klärung"
    set_status("erntet", f"{src['name']} ({modelabel})", busy=True)
    found, kept, err, new_cursor = 0, 0, None, cursor
    seen = traced = dropped = 0
    try:
        batch, new_cursor = fn(src, cursor)
        # ZEITFENSTER-SICHERUNG fuer Wissenschaftsquellen: selbst wenn eine API
        # den serverseitigen Datumsfilter ignoriert, wird Altes hier verworfen.
        # Und wichtiger: weil absteigend sortiert wird, heisst "alles zu alt" =
        # tiefer blaettern bringt nur noch Aelteres -> Cursor auf den naechsten
        # Begriff setzen statt weiter in die Vergangenheit zu graben.
        ws = _window_start()
        if ws and src["kind"] in PAPER_KINDS and batch:
            frisch = [d for d in batch if (d.get("published_at") or "") >= ws]
            if not frisch:
                ti = (cursor or {}).get("ti", 0)
                new_cursor = {"ti": ti + 1}          # Paginierung abbrechen
                log("fenster", f"{src['name']}: nur Altes (< {ws}) — "
                               f"weiter zum naechsten Begriff.")
            batch = frisch
        # STUFE 0 — BEKANNTES RAUS, bevor irgendetwas Teures laeuft.
        # Flache Feeds liefern bei jeder Abfrage fast dasselbe (gemessen: 0,21
        # neue Dok/Abfrage = ~99% Wiederholung). Frueher kostete jede dieser
        # Wiederholungen zwei Modellaufrufe und wurde erst am INSERT verworfen.
        n_roh = len(batch)
        batch, n_bekannt = _neue_dokumente(batch)
        if n_bekannt:
            log("dedup", f"{src['name']}: {n_bekannt} von {n_roh} bereits bekannt "
                         f"— uebersprungen (kein Modellaufruf).")
        # FILTER-KASKADE (Stufen 1-3, gratis/billig). Der Deckel ist damit ein
        # Qualitaets-, kein Mengenschnitt: die Rate begrenzt nur ABFRAGEN
        # (Semantic Scholar ~458 Dok/Abfrage, News ~5).
        cap = MAX_DOCS_PER_QUERY
        n_vorher = len(batch)
        if batch:
            batch, schnitt = _auswahl(batch, cap, src["name"])
            if n_vorher > cap:
                log("auswahl", f"{src['name']}: {n_vorher} Funde -> {len(batch)} "
                    f"(Kanten-Neuheit Ø {schnitt}, {int(EXPLORE_SHARE*100)}% zufaellig).")
        # STUFE 4 (teuer, nur auf Ueberlebenden): Allgemeinwissen? Einzelfall?
        # Entscheidet, was wir BEHALTEN. Verworfenes wandert in die Spur.
        if batch and cfg_bool("stufe4_aktiv", True) and any_model_active():
            behalten = []
            n_triv = n_einz = 0
            for d in batch:
                if not COLLECTOR["running"] and not FACTS["running"]:
                    behalten.append(d); continue
                try:
                    allg, einzel = stufe4_trivial(d)
                except Exception:
                    allg = einzel = None
                if allg:
                    _spur(d, "trivial: Allgemeinwissen"); n_triv += 1; continue
                if einzel:
                    _spur(d, "trivial: Einzelfall"); n_einz += 1; continue
                behalten.append(d)
            # Quellen-Profil fortschreiben: WELCHE Quelle liefert wie oft
            # Nicht-Konsens? Gemessen statt angenommen.
            try:
                q("INSERT OR IGNORE INTO quellen_profil(source_id) VALUES(?)",
                  (src["id"],), fetch=False)
                q("UPDATE quellen_profil SET n_geprueft=n_geprueft+?, "
                  "n_trivial=n_trivial+?, n_einzelfall=n_einzelfall+?, "
                  "n_signal=n_signal+? WHERE source_id=?",
                  (len(batch), n_triv, n_einz, len(behalten), src["id"]), fetch=False)
            except Exception:
                pass
            if len(behalten) < len(batch):
                log("stufe4", f"{src['name']}: {n_triv} Allgemeinwissen, "
                              f"{n_einz} Einzelfall aussortiert.")
            batch = behalten
        found = len(batch)
        if batch:
            set_status("bewertet", f"{src['name']}: {found} Fundstellen", busy=True)
        stored, seen, traced, dropped = _store_batch(src, batch)
        kept = len(stored)
        for did, txt in stored:
            _assign(did, txt)
    except Exception as e:
        err = str(e)[:200]
        log("ernte", f"{src['name']}: {err}")

    # Zaehler fuer die Oberflaeche (Strom: gesehen / behalten / Spur / verworfen)
    for k, v in (("seen", seen), ("kept", kept), ("traced", traced), ("dropped", dropped)):
        STATUS[k] = STATUS.get(k, 0) + v

    # Ontologie-Wachstum gebuendelt (nicht jeden Tick, spart LLM-Kosten)
    grew = 0
    unassigned = [(r["id"], (r["title"] or "") + " " + (r["text"] or ""))
                  for r in q("SELECT d.id, d.title, d.text FROM documents d "
                             "LEFT JOIN doc_themes dt ON dt.doc_id=d.id "
                             "WHERE dt.doc_id IS NULL AND d.relevance>=0.5 LIMIT 40")]
    if len(unassigned) >= 6:
        set_status("ordnet zu", "Ontologie prüft neue Belege", busy=True)
        grew = grow_ontology(unassigned)

    # Rate-Fenster + Cursor + Statistik schreiben; Fehlerkette -> Auto-Pause
    now = datetime.now()
    if err:
        fails = (src["fail_count"] or 0) + 1
        pause = None
        if fails >= 3:
            pause = (now + timedelta(hours=3)).isoformat()
            log("collector", f"{src['name']}: {fails} Fehler -> 3h pausiert.")
        q("UPDATE sources SET last_error=?, fail_count=?, paused_until=?, "
          "last_tick_ts=?, queries_win=queries_win+1, total_queries=total_queries+1 "
          "WHERE id=?", (err, fails, pause, now.isoformat(), src["id"]), fetch=False)
    else:
        q("UPDATE sources SET cursor=?, last_crawl=?, last_found=?, last_kept=?, "
          "last_error=NULL, fail_count=0, paused_until=NULL, last_tick_ts=?, "
          "queries_win=queries_win+1, total_queries=total_queries+1 WHERE id=?",
          (json.dumps(new_cursor) if new_cursor else None, TODAY(), found, kept,
           now.isoformat(), src["id"]), fetch=False)
    STATUS["scans_done"] += 1
    run_maintenance()                        # Wecken + Verfall in fester Kadenz
    tail = f", {traced} Spur" if traced else ""
    set_status("bereit", f"{src['name']}: {kept} behalten{tail}"
               + (f", {grew} Themen neu" if grew else ""), busy=False)
    return {"queried": src["name"], "mode": src["_mode"], "found": found,
            "kept": kept, "traced": traced, "dropped": dropped, "themes_new": grew}

COLLECTOR = {"running": False, "thread": None,
             "tick_seconds": cfg_int("collector_tick_seconds", 30), "last": None}

def _collector_loop():
    while COLLECTOR["running"]:
        try:
            collect_tick()
            COLLECTOR["last"] = datetime.now().strftime("%H:%M:%S")
            HEARTBEAT["collector"] = time.time()
        except Exception as e:
            log("collector", f"Fehler: {str(e)[:200]}")
        except BaseException as e:                 # z.B. MemoryError/Recursion
            log("collector", f"SCHWERER Fehler: {type(e).__name__}: {str(e)[:150]}")
            time.sleep(30)                         # durchatmen statt sterben
        # in kleinen Schritten schlafen, damit Stop schnell greift
        for _ in range(max(1, COLLECTOR["tick_seconds"])):
            if not COLLECTOR["running"]:
                break
            time.sleep(1)

# ==================================================================
#  DAUERBETRIEB: WATCHDOG + AUFRAEUMEN  (fuer wochenlange Laeufe)
# ==================================================================
HEARTBEAT = {"collector": 0, "facts": 0}
WATCHDOG = {"restarts": 0, "last_prune": 0, "disk_free_mb": None, "note": ""}

def _prune_db():
    """Haelt die DB schlank. Ohne das waechst sie bei 10s-Takt ueber Wochen
    unbegrenzt: log-Zeilen und Spuren summieren sich zu hunderttausenden."""
    try:
        # Protokoll: nur die letzten N Zeilen behalten (Diagnose reicht)
        keep = cfg_int("log_keep", 5000)
        n = q("SELECT COUNT(*) c FROM log")[0]["c"]
        if n > keep * 1.5:
            q("DELETE FROM log WHERE id NOT IN "
              "(SELECT id FROM log ORDER BY id DESC LIMIT ?)", (keep,), fetch=False)
            log("wartung", f"Protokoll gekuerzt: {n} -> {keep} Zeilen.")
        # Spuren: grosszuegig deckeln. KEIN Zeitverfall (dein KI-Mathematik-Fall!),
        # nur eine ferne Obergrenze gegen unbegrenztes Wachstum. Aelteste zuerst,
        # aber bereits GEWECKTE (revived) werden zuletzt geopfert.
        smax = cfg_int("spuren_max", 200000)
        sn = q("SELECT COUNT(*) c FROM discarded")[0]["c"]
        if sn > smax:
            q("DELETE FROM discarded WHERE id IN (SELECT id FROM discarded "
              "ORDER BY revived ASC, id ASC LIMIT ?)", (sn - smax,), fetch=False)
            log("wartung", f"Spuren gedeckelt: {sn} -> {smax} (Speichergrenze).")
    except Exception as e:
        log("wartung", f"Aufraeumen fehlgeschlagen: {str(e)[:120]}")

def _disk_free_mb():
    try:
        import shutil as _sh
        return _sh.disk_usage(os.path.dirname(DB_PATH) or ".").free // (1024 * 1024)
    except Exception:
        return None

def _watchdog_loop():
    """Laeuft dauerhaft im Hintergrund und haelt den Betrieb am Leben:
      - gestorbene Threads neu starten (sonst zeigt die GUI 'laeuft', aber nichts
        passiert — der gefaehrlichste stille Ausfall)
      - DB aufraeumen
      - Plattenplatz pruefen (volle Platte = korrupte DB)"""
    while True:
        try:
            time.sleep(60)
            # 0) Ollama gesund? Wenn nicht: pausieren statt Mock-Daten erzeugen.
            #    ABER (Jens 08.08.): `ollama_guard`->`_ollama_probe` macht einen ECHTEN Generierungs-Ping
            #    (laedt qwen3:30b, ~19 GB). Im CONTROL-ONLY-Leerlauf (weder Sammler noch 1c laufen, und wir
            #    haben auch nichts pausiert) darf Ollama NICHT angefasst werden — sonst laedt der Watchdog das
            #    30B-Modell alle 60 s in eine 20-GB-Karte (OOM), obwohl die "nutzende Funktion" gar nicht laeuft.
            #    Ollama wird also nur geprueft, wenn tatsaechlich etwas rechnet (oder von uns pausiert wurde).
            _nutzt_ollama = (COLLECTOR.get("running") or FACTS.get("running")
                             or DEDUP.get("running") or REEVAL.get("running")
                             or OLLAMA.get("paused_collector") or OLLAMA.get("paused_facts"))
            if _nutzt_ollama and (ROUTING.get("relevance") == "local" or ROUTING.get("facts") == "local"):
                ollama_guard()
            # 1) Sammler-Thread tot, obwohl er laufen soll?
            t = COLLECTOR.get("thread")
            if COLLECTOR["running"] and (t is None or not t.is_alive()):
                WATCHDOG["restarts"] += 1
                log("watchdog", "Sammler-Thread war tot — starte neu.")
                COLLECTOR["thread"] = threading.Thread(target=_collector_loop,
                                                       daemon=True)
                COLLECTOR["thread"].start()
            # 2) 1c-Thread tot, obwohl er laufen soll?
            ft = FACTS.get("thread")
            if FACTS["running"] and (ft is None or not ft.is_alive()):
                WATCHDOG["restarts"] += 1
                log("watchdog", "1c-Thread war tot — setze fort.")
                FACTS["thread"] = threading.Thread(
                    target=lambda: _run_extraction(None), daemon=True)
                FACTS["thread"].start()
            # 3) Aufraeumen (stuendlich)
            if time.time() - WATCHDOG["last_prune"] > 3600:
                WATCHDOG["last_prune"] = time.time()
                _prune_db()
            # 4) Plattenplatz
            free = _disk_free_mb()
            WATCHDOG["disk_free_mb"] = free
            if free is not None and free < 500:
                WATCHDOG["note"] = f"WENIG PLATZ: {free} MB frei — Sammler gestoppt."
                if COLLECTOR["running"]:
                    log("watchdog", f"Nur {free} MB frei — Sammler gestoppt, "
                                    f"um die Datenbank zu schuetzen.")
                    stop_collector()
                FACTS["running"] = False
            elif free is not None and free < 2000:
                WATCHDOG["note"] = f"Platz knapp: {free} MB frei."
            else:
                WATCHDOG["note"] = ""
        except Exception as e:
            try: log("watchdog", f"Fehler: {str(e)[:120]}")
            except Exception: pass
        except BaseException:
            pass                                  # der Watchdog darf NIE sterben

def start_collector():
    if not COLLECTOR["running"]:
        COLLECTOR["running"] = True
        COLLECTOR["thread"] = threading.Thread(target=_collector_loop, daemon=True)
        COLLECTOR["thread"].start()
        log("collector", "Dauersammler gestartet.")

def stop_collector():
    if COLLECTOR["running"]:
        COLLECTOR["running"] = False
        set_status("gestoppt", "Dauersammler angehalten", busy=False)
        log("collector", "Dauersammler gestoppt.")

# ==================================================================
#  NEU BEWERTEN  (Bestand behalten, Relevanz+Ontologie frisch erzeugen)
# ==================================================================
REEVAL = {"running": False, "done": 0, "total": 0, "phase": "bereit"}

def _reevaluate_all(fix_dates=True, rebuild_ontology=True):
    """Dokumente BLEIBEN. Relevanz wird mit geschaerftem Prompt neu berechnet;
    Zukunftsdaten korrigiert; Ontologie frisch aus sauberem Bestand aufgebaut."""
    REEVAL.update(running=True, done=0, phase="Datum pruefen")
    log("reeval", "Neubewertung gestartet — Dokumente bleiben erhalten.")
    if fix_dates:
        n = q("SELECT COUNT(*) c FROM documents WHERE published_at > ?", (TODAY(),))[0]["c"]
        q("UPDATE documents SET published_at=? WHERE published_at > ?",
          (TODAY(), TODAY()), fetch=False)
        if n:
            log("reeval", f"{n} Zukunftsdaten auf heute korrigiert.")
    # Relevanz neu — in Bloecken, Dokument-Objekte durch evaluate() schicken
    ids = [r["id"] for r in q("SELECT id FROM documents ORDER BY id")]
    REEVAL["total"] = len(ids)
    REEVAL["phase"] = "Relevanz neu bewerten"
    B = 10
    for i in range(0, len(ids), B):
        if not REEVAL["running"]:
            break
        chunk = ids[i:i+B]
        rows = q("SELECT id,title,text,source_type FROM documents WHERE id IN (%s)"
                 % ",".join("?" * len(chunk)), tuple(chunk))
        docs = [dict(id=r["id"], title=r["title"], text=r["text"],
                     source_type=r["source_type"]) for r in rows]
        evaluate(docs)
        for d in docs:
            q("UPDATE documents SET relevance=? WHERE id=?",
              (round(d["relevance"], 2), d["id"]), fetch=False)
        REEVAL["done"] = min(i + B, len(ids))
        set_status("neu bewerten", f"{REEVAL['done']}/{len(ids)} Dokumente", busy=True)
    if rebuild_ontology and REEVAL["running"]:
        REEVAL["phase"] = "Ontologie neu aufbauen"
        set_status("neu bewerten", "Ontologie wird neu aufgebaut", busy=True)
        # Maschinen-Themen + alle Zuordnungen verwerfen; Seed-Themen behalten
        q("DELETE FROM doc_themes", fetch=False)
        q("DELETE FROM themes WHERE created_by='machine'", fetch=False)
        # Bestehende (Seed-)Themen neu zuordnen
        for r in q("SELECT id,title,text FROM documents WHERE relevance>=0.5"):
            _assign(r["id"], (r["title"] or "") + " " + (r["text"] or ""))
        # Ontologie frisch wachsen lassen — in mehreren Runden ueber Unzugeordnete
        for _ in range(30):
            if not REEVAL["running"]:
                break
            un = [(r["id"], (r["title"] or "") + " " + (r["text"] or ""))
                  for r in q("SELECT d.id,d.title,d.text FROM documents d "
                             "LEFT JOIN doc_themes dt ON dt.doc_id=d.id "
                             "WHERE dt.doc_id IS NULL AND d.relevance>=0.6 LIMIT 15")]
            if len(un) < 3:
                break
            if grow_ontology(un) == 0:
                break
    REEVAL.update(running=False, phase="fertig")
    set_status("bereit", f"Neubewertung fertig ({REEVAL['done']} Dokumente)", busy=False)
    log("reeval", f"Neubewertung fertig: {REEVAL['done']} Dokumente neu bewertet.")

def start_reeval():
    if REEVAL["running"]:
        return False
    stop_collector()                       # waehrend Neubewertung kein Sammeln
    threading.Thread(target=_reevaluate_all, daemon=True).start()
    return True

# ==================================================================
#  MODUL 1c  —  FAKTEN-EXTRAKTION  (Subjekt-Beziehung-Objekt + ist/wird + Achsen)
# ==================================================================
FACTS = {"running": False, "done": 0, "total": 0, "phase": "bereit",
         "test": False, "found": 0, "thread": None}

FACTS_PROMPT = (
 "Du extrahierst aus einem Dokument bis zu DREI oekonomisch bedeutsame Kernaussagen "
 "fuer ein Erwartungsmodell. Jede Aussage als Subjekt-Beziehung-Objekt.\n"
 "Fuer JEDE Aussage bestimme:\n"
 "- modus: 'ist' (beschreibt einen Zustand/Fakt der Welt) ODER 'wird' (eine "
 "Erwartung/Prognose/Absicht ueber die Zukunft).\n"
 "- signalart: 'technologie' (eine Faehigkeit/Methode/Erfindung, die reift) ODER "
 "'ereignis' (eine Handlung/Transaktion/Entscheidung zu einem Zeitpunkt).\n"
 "- reife_score 0..1: Reifegrad auf der Leiter Grundlagenforschung(0.1) -> "
 "angewandte Forschung(0.3) -> Patent/Prototyp(0.5) -> Finanzierung/Pilot(0.7) -> "
 "Markt(0.9) -> breit wirksam(1.0).\n"
 "- latenz: 'kurz' (<1 Jahr bis oekonomische Wirkung), 'mittel' (1-3 J), 'lang' (>3 J).\n"
 "- erwartungstempo 0..1: wie schnell sich die Erwartung dazu GERADE verschiebt "
 "(0 = laengst bekannt/statisch, 1 = kippt gerade schnell). Reife und Tempo sind "
 "UNABHAENGIG.\n"
 "- konfidenz 0..1: wie sicher die Aussage aus dem Text belegt ist.\n"
 "Wenn das Dokument keine oekonomisch verwertbare Aussage enthaelt, gib eine leere "
 "Liste. Erfinde nichts. Antworte NUR als JSON: {\"fakten\":[{\"subjekt\":\"..\","
 "\"beziehung\":\"..\",\"objekt\":\"..\",\"modus\":\"ist|wird\",\"signalart\":"
 "\"technologie|ereignis\",\"reife_score\":0.x,\"latenz\":\"kurz|mittel|lang\","
 "\"erwartungstempo\":0.x,\"konfidenz\":0.x}]}\n"
 "BEISPIEL Dokument: {\"titel\":\"TSMC startet Bau einer 2nm-Fab in Arizona, "
 "Produktion ab 2027 geplant\",\"text\":\"...\"}\n"
 "BEISPIEL Antwort: {\"fakten\":[{\"subjekt\":\"TSMC\",\"beziehung\":\"baut\","
 "\"objekt\":\"2nm-Halbleiterfabrik in Arizona\",\"modus\":\"ist\",\"signalart\":"
 "\"ereignis\",\"reife_score\":0.7,\"latenz\":\"mittel\",\"erwartungstempo\":0.5,"
 "\"konfidenz\":0.9},{\"subjekt\":\"TSMC\",\"beziehung\":\"plant Produktionsstart\","
 "\"objekt\":\"2nm-Chips ab 2027\",\"modus\":\"wird\",\"signalart\":\"ereignis\","
 "\"reife_score\":0.6,\"latenz\":\"mittel\",\"erwartungstempo\":0.6,\"konfidenz\":0.7}]}\n"
 "WICHTIG: subjekt/beziehung/objekt jeweils KURZ halten (subjekt max 60 Zeichen, "
 "beziehung max 25, objekt max 60). Keine ganzen Saetze, keine Erklaerungen, "
 "keine weiteren Felder. Nur das JSON.\n"
 "Jetzt dieses Dokument: ")

def _reife_label(score):
    s = score or 0
    return ("Grundlagenforschung" if s < 0.2 else "angewandte Forschung" if s < 0.4
            else "Patent/Prototyp" if s < 0.6 else "Finanzierung/Pilot" if s < 0.8
            else "Markt" if s < 0.95 else "breit wirksam")

class ModellWeg(Exception):
    """Das Modell hat nicht geantwortet (nicht: 'es gab keine Fakten').
    Der Unterschied ist entscheidend: 'keine Fakten' ist ein Ergebnis und wird
    vermerkt; 'kein Modell' ist ein Ausfall — das Dokument darf NICHT als
    erledigt gelten, sonst wird es nie wieder verarbeitet."""

def extract_facts(doc):
    """Zieht bis zu 3 strukturierte Fakten aus einem Dokument.
    Token-Limit grosszuegig (abgeschnittenes JSON war die Hauptfehlerquelle:
    3 ausfuehrliche Fakten sprengten die alten 1200 Tokens).
    Wirft ModellWeg, wenn gar kein Modell antwortete — NICHT als 0 Fakten
    durchgehen lassen (das hat schon 2.347 Dokumente unwiederbringlich als
    'erledigt' markiert, waehrend Ollama neu startete)."""
    payload = {"titel": doc.get("title") or "", "text": (doc.get("text") or "")[:1200]}
    res = ask_json("facts", FACTS_PROMPT + json.dumps(payload, ensure_ascii=False),
                   max_tokens=3000, model=cfg("facts_model"), schema=FACTS_SCHEMA)
    # Drei Faelle sauber trennen:
    #   None = KEIN Modell antwortete (Ausfall)           -> ModellWeg (pausieren)
    #   {}   = Modell antwortete, aber unbrauchbar/unlesbar -> UnlesbareModellantwort
    #          (der Loop entscheidet: vereinzelt ueberspringen, systemisch pausieren)
    #   sonst = echtes Ergebnis (auch {"fakten": []} = 'keine verwertbare Aussage')
    if res is None:
        raise ModellWeg("Modell hat nicht geantwortet")
    if res == {}:
        raise UnlesbareModellantwort("Modell-Ausgabe unlesbar (facts)")
    out = []
    if isinstance(res, dict) and isinstance(res.get("fakten"), list):
        for f in res["fakten"][:3]:
            if not isinstance(f, dict) or not f.get("subjekt"):
                continue
            def num(k):
                try: return max(0.0, min(1.0, float(f.get(k))))
                except Exception: return None
            out.append(dict(
                subjekt=str(f.get("subjekt", ""))[:200],
                beziehung=str(f.get("beziehung", ""))[:120],
                objekt=str(f.get("objekt", ""))[:200],
                modus=f.get("modus") if f.get("modus") in ("ist", "wird") else "ist",
                signalart=(f.get("signalart") if f.get("signalart")
                           in ("technologie", "ereignis") else "ereignis"),
                reife_score=num("reife_score"),
                latenz=f.get("latenz") if f.get("latenz") in ("kurz", "mittel", "lang")
                       else "mittel",
                erwartungstempo=num("erwartungstempo"), konfidenz=num("konfidenz")))
    return out

def _store_facts(doc, facts):
    n = 0
    for f in facts:
        try:
            q("INSERT INTO facts(doc_id,source_type,subjekt,beziehung,objekt,modus,"
              "signalart,reife,reife_score,latenz,erwartungstempo,konfidenz,"
              "published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (doc["id"], doc.get("source_type"), f["subjekt"], f["beziehung"],
               f["objekt"], f["modus"], f["signalart"], _reife_label(f["reife_score"]),
               f["reife_score"], f["latenz"], f["erwartungstempo"], f["konfidenz"],
               doc.get("published_at")), fetch=False)
            n += 1
        except Exception:
            pass
    _aufz_fakten(doc, facts)                             # Aufzeichnung: Fakt-Attribute (gepinnter Extraktor)
    return n

def _run_extraction(limit=None):
    """Verarbeitet Dokumente (neueste zuerst), zieht Fakten, legt sie ab.
    limit gesetzt = Testlauf. KEIN Relevanzfilter (Relevanz ist unzuverlaessig).
    Laeuft ueber das konfigurierte Modell (Standard: lokal -> gratis).
    FORTSETZBAR: jeder Versuch wird in facts_done vermerkt — auch wenn er 0 Fakten
    ergab. Nach einem Abbruch macht der Lauf dort weiter, statt von vorn."""
    if not any_model_active():
        FACTS.update(running=False, phase="kein Modell aktiv")
        log("1c", "Fakten-Extraktion braucht ein aktives Modell (Ollama lokal "
            "oder Frontier).")
        return
    FACTS.update(running=True, done=0, found=0,
                 phase="Testlauf" if limit else "Voller Lauf", test=bool(limit))
    # Sammler laeuft WEITER: beide teilen sich die GPU ueber LLM_GATE und
    # alternieren dadurch automatisch (statt sich zu ueberlagern).
    if cfg("parallel_mode", "alternate") == "exclusive":
        stop_collector()
    # offene Menge = noch nie versuchte Dokumente (Near-Dups überspringt 1c)
    total = q("SELECT COUNT(*) c FROM documents d LEFT JOIN facts_done fd "
              "ON fd.doc_id=d.id WHERE fd.doc_id IS NULL AND d.dup_of IS NULL")[0]["c"]
    FACTS["total"] = min(total, limit) if limit else total
    log("1c", f"Fakten-Extraktion gestartet ({FACTS['total']} offene Dok, "
        + ("Test" if limit else "voll") + ") — Ereignissignale zuerst.")
    done = 0
    unbrauchbar_streak = 0          # unlesbare Modell-Antworten in Folge (Systemik-Wache)
    unbrauchbar_grenze = cfg_int("facts_unbrauchbar_grenze", 8)
    while FACTS["running"]:
        # in Bloecken holen (nicht 70k Zeilen auf einmal in den Speicher).
        # REIHENFOLGE: Ereignis-/Reifesignale zuerst (news, funding, patent,
        # 8-K) — sie tragen ~4% der Menge, aber den Grossteil des Signalwerts.
        # Die Paper-Masse (96%, ueberwiegend Lehrbuchsaetze) kommt zuletzt.
        batch = q("""SELECT d.id,d.title,d.text,d.source_type,d.published_at
                     FROM documents d LEFT JOIN facts_done fd ON fd.doc_id=d.id
                     WHERE fd.doc_id IS NULL AND d.dup_of IS NULL
                     ORDER BY CASE d.source_type
                                WHEN 'news' THEN 0 WHEN 'funding' THEN 1
                                WHEN 'patent' THEN 2 ELSE 3 END,
                              d.id DESC LIMIT 50""")
        if not batch:
            # Rueckstand abgearbeitet. Im Testlauf: fertig.
            if limit or not cfg_bool("facts_continuous", True):
                break
            # DAUERBETRIEB: nicht aufhoeren, sondern auf Nachschub warten.
            # Sonst idlet die GPU tage- bis wochenlang, waehrend der Sammler
            # weiter Dokumente anhaeuft, die nie Fakten bekommen.
            if FACTS["phase"] != "wartet auf Nachschub":
                FACTS["phase"] = "wartet auf Nachschub"
                log("1c", f"Rueckstand abgearbeitet ({done} Dok in diesem Lauf). "
                          f"Warte auf neue Dokumente — laeuft automatisch weiter.")
                set_status("bereit", f"1c: Rueckstand fertig, wartet auf Nachschub "
                                     f"({FACTS['found']} Fakten)", busy=False)
            for _ in range(60):                  # eine Minute, stoppbar
                if not FACTS["running"]:
                    break
                time.sleep(1)
            # offene Menge neu bestimmen (der Sammler hat evtl. nachgelegt)
            FACTS["total"] = q("SELECT COUNT(*) c FROM documents d "
                               "LEFT JOIN facts_done fd ON fd.doc_id=d.id "
                               "WHERE fd.doc_id IS NULL AND d.dup_of IS NULL")[0]["c"]
            if FACTS["total"]:
                FACTS["phase"] = "Voller Lauf"
                log("1c", f"{FACTS['total']} neue Dokumente — mache weiter.")
            continue
        FACTS["phase"] = "Voller Lauf" if not limit else "Testlauf"
        for r in batch:
            if not FACTS["running"]:
                break
            if limit and done >= limit:
                break
            doc = dict(r)
            n = 0
            try:
                n = _store_facts(doc, extract_facts(doc))
                FACTS["found"] += n
                unbrauchbar_streak = 0     # Erfolg -> Systemik-Zaehler zuruecksetzen
            except ModellWeg:
                # KEIN Modell -> Dokument NICHT als erledigt vermerken, sonst
                # ist es fuer immer verloren. Stattdessen warten, bis Ollama
                # zurueck ist. (Beim Neustart von Ollama hat 1c so schon 2.347
                # Dokumente in Sekunden verbrannt.)
                if FACTS["phase"] != "wartet auf Modell":
                    FACTS["phase"] = "wartet auf Modell"
                    log("1c", "Modell antwortet nicht — 1c pausiert. Dokumente "
                              "werden NICHT als erledigt markiert.")
                    set_status("wartet", "1c: Modell weg — pausiert (keine "
                                         "Datenvergiftung)", busy=False)
                for _ in range(30):
                    if not FACTS["running"]:
                        break
                    time.sleep(1)
                break                      # Block abbrechen, neu versuchen
            except UnlesbareModellantwort:
                # Modell HAT geantwortet, aber unlesbar (abgeschnitten/Muell).
                # VEREINZELT = Gift-Dokument -> ueberspringen (0 Fakten, erledigt),
                # damit der Rueckstand leert (der Bug aus scraper.db: 40k offen,
                # weil ein solches Dok als 'Modell weg' ewig neu versucht wurde).
                # SYSTEMISCH (viele in Folge) = Modell offenbar kaputt -> NICHT den
                # ganzen Korpus als '0 Fakten' verbrennen, sondern pausieren (wie
                # Ausfall). Genau der 2.347-Dok-Verbrenn-Fall, nur andersherum.
                unbrauchbar_streak += 1
                if unbrauchbar_streak >= unbrauchbar_grenze:
                    if FACTS["phase"] != "wartet auf Modell":
                        FACTS["phase"] = "wartet auf Modell"
                        log("1c", f"{unbrauchbar_streak}x unlesbare Modell-Ausgabe in "
                                  f"Folge — 1c pausiert (Modell liefert Muell; der "
                                  f"Rueckstand wird NICHT verbrannt).")
                        set_status("wartet", "1c: Modell liefert unlesbar — pausiert",
                                   busy=False)
                    for _ in range(30):
                        if not FACTS["running"]:
                            break
                        time.sleep(1)
                    break                  # Block abbrechen, neu versuchen
                log("1c", f"Doc {doc['id']}: Modell-Ausgabe unlesbar — uebersprungen "
                          f"(0 Fakten).")     # faellt durch -> facts_done (erledigt)
            except Exception as e:
                log("1c", f"Doc {doc['id']}: {str(e)[:100]}")
            # Vermerken — auch 0 Fakten. Das ist ein ERGEBNIS des Modells
            # ('keine verwertbare Aussage'), kein Ausfall.
            try:
                q("INSERT OR REPLACE INTO facts_done(doc_id,n_facts) VALUES(?,?)",
                  (doc["id"], n), fetch=False)
            except Exception:
                pass
            done += 1
            FACTS["done"] = done
            if FACTS["phase"] == "wartet auf Modell":
                FACTS["phase"] = "Voller Lauf"
                log("1c", "Modell antwortet wieder — 1c laeuft weiter.")
            if done % 5 == 0:
                set_status("Fakten (1c)", f"{done}/{FACTS['total']} · "
                           f"{FACTS['found']} Fakten", busy=True)
        if limit and done >= limit:
            break
    FACTS.update(running=False, phase="fertig")
    set_status("bereit", f"1c fertig: {FACTS['found']} Fakten aus {FACTS['done']} Dok",
               busy=False)
    log("1c", f"Fakten-Extraktion fertig: {FACTS['found']} Fakten aus {done} Dok.")

def start_extraction(test_n=None):
    if FACTS["running"]:
        return False
    # Fable-B2 / Jens 08.08.: 1c ist die "nutzende Funktion" fuer Ollama -> beim Start SICHERSTELLEN, dass es
    # laeuft. Ist bei lokalem Routing das Modell nicht erreichbar, Ollama JETZT starten und die Absicht merken
    # (paused_facts); der ollama_guard startet 1c automatisch, sobald das Modell antwortet. Kein stiller
    # Nicht-Start mehr (frueher brach _run_extraction sofort mit "kein Modell aktiv" ab und blieb aus).
    _lokal = (ROUTING.get("facts") == "local" or ROUTING.get("relevance") == "local")
    if _lokal and not _ollama_erreichbar():
        _ollama_manuell_start()
        OLLAMA["paused_facts"] = True
        FACTS["phase"] = "wartet auf Modell (Ollama startet)"
        log("1c", "Ollama war nicht bereit — Start ausgeloest; 1c beginnt automatisch, sobald das Modell "
                  "antwortet.")
        return True
    def _safe():
        try:
            _run_extraction(test_n)
        except Exception as e:
            FACTS.update(running=False, phase="Fehler")
            log("1c", f"Lauf abgebrochen: {str(e)[:180]}")
        except BaseException as e:
            FACTS.update(running=False, phase="Fehler")
            try: log("1c", f"SCHWERER Fehler: {type(e).__name__}")
            except Exception: pass
    FACTS["thread"] = threading.Thread(target=_safe, daemon=True)
    FACTS["thread"].start()
    return True

# ==================================================================
#  ERWARTUNGSBILD  (die Aggregation der Ontologie)
# ==================================================================
STAGES = ["paper", "patent", "funding", "news"]

# ==================================================================
#  SPUREN: WECKEN (Themen-Erstarken) + VERFALL (Mainstream & bedeutungslos)
# ==================================================================
# Kein Verfall nach Zeit. Spuren bleiben grundsaetzlich. Sie werden GEWECKT,
# wenn ihr Thema neue Evidenz bekommt; ein THEMA verfaellt nur, wenn es seinen
# Lauf genommen hat: im Mainstream angekommen UND dabei bedeutungslos.
DECAY = {"gap_max": cfg_float("decay_gap_max", 0.2),      # niedriger Gap=Mainstream
         "rel_max": cfg_float("decay_rel_max", 0.45),     # geringe Bedeutung
         "min_docs": cfg_int("decay_min_docs", 30)}       # erst ab genug Belegen
WAKE = {"every": cfg_int("wake_every_ticks", 25)}          # Kadenz
_maint_i = {"n": 0}

def _theme_momentum(theme_id):
    """Belege der letzten 30 Tage vs. der 30 Tage davor -> steigt/faellt."""
    d30 = (date.today() - timedelta(days=30)).isoformat()
    d60 = (date.today() - timedelta(days=60)).isoformat()
    recent = q("SELECT COUNT(*) c FROM doc_themes dt JOIN documents d ON d.id=dt.doc_id "
               "WHERE dt.theme_id=? AND d.published_at>?", (theme_id, d30))[0]["c"]
    prev = q("SELECT COUNT(*) c FROM doc_themes dt JOIN documents d ON d.id=dt.doc_id "
             "WHERE dt.theme_id=? AND d.published_at>? AND d.published_at<=?",
             (theme_id, d60, d30))[0]["c"]
    return recent, prev

def decay_themes():
    """Markiert Themen als verfallen (excluded=2), die im Mainstream angekommen
    UND bedeutungslos sind UND deren Momentum faellt. Reversibel (nur Markierung).
    Aggregiert in SQL (kein Laden von Rohzeilen bei grossen Themen)."""
    decayed = 0
    rows = q(f"""SELECT th.id, th.name, COUNT(dt.doc_id) n,
                        SUM(CASE WHEN d.source_type='news' THEN 1 ELSE 0 END) news,
                        AVG(d.relevance) avg_rel
                 FROM themes th
                 LEFT JOIN doc_themes dt ON dt.theme_id=th.id
                 LEFT JOIN documents d ON d.id=dt.doc_id
                 WHERE th.excluded=0
                 GROUP BY th.id HAVING n >= {int(DECAY['min_docs'])}""")
    for th in rows:
        n = th["n"] or 0
        gap = 1 - (th["news"] or 0) / n
        avg_rel = th["avg_rel"] or 0
        recent, prev = _theme_momentum(th["id"])
        if gap <= DECAY["gap_max"] and avg_rel <= DECAY["rel_max"] and recent < prev:
            q("UPDATE themes SET excluded=2 WHERE id=?", (th["id"],), fetch=False)
            log("verfall", f"Thema verfallen (Mainstream+bedeutungslos): {th['name'][:50]}")
            decayed += 1
    return decayed

def wake_traces():
    """Weckt schlafende Spuren, deren Thema neue Evidenz bekommen hat: Spur-Titel
    gegen Schlagworte erstarkender Themen matchen, neu bewerten, ggf. zum Dokument
    befoerdern. Ausloeser ist neue Evidenz, nicht die Uhr."""
    # 'erstarkend' = Thema mit steigendem Momentum in den letzten 30 Tagen
    strengthening = []
    for th in q("SELECT id, name, keywords FROM themes WHERE excluded=0"):
        recent, prev = _theme_momentum(th["id"])
        if recent > prev and recent >= 3:
            try:
                kws = [k.lower() for k in json.loads(th["keywords"]) if len(k) > 3]
            except Exception:
                kws = []
            if kws:
                strengthening.append((th["id"], th["name"], kws))
    if not strengthening:
        return 0
    woken = 0
    # nur eine begrenzte Zahl Spuren pro Runde neu bewerten (Kosten/Zeit)
    for tid, tname, kws in strengthening[:10]:
        like = " OR ".join(["LOWER(title) LIKE ?"] * len(kws))
        args = [f"%{k}%" for k in kws]
        traces = q(f"SELECT * FROM discarded WHERE revived=0 AND ({like}) LIMIT 15", tuple(args))
        if not traces:
            continue
        docs = [dict(source_type=t["source_type"], title=t["title"], text=t["title"],
                     url=t["url"], published_at=t["published_at"], _trace_id=t["id"])
                for t in traces]
        evaluate(docs)
        for d in docs:
            if d["relevance"] >= REL_MIN:              # jetzt relevant -> befoerdern
                try:
                    did = q("INSERT INTO documents(source_id,source_type,title,text,"
                            "url,relevance,trust,published_at) VALUES(?,?,?,?,?,?,?,?)",
                            (None, d["source_type"], d["title"], d.get("text"),
                             d.get("url"), round(d["relevance"], 2), d["trust"],
                             _valid_date(d["published_at"])), fetch=False)
                    _assign(did, (d["title"] or ""))
                    woken += 1
                except Exception:
                    pass
            q("UPDATE discarded SET revived=1 WHERE id=?", (d["_trace_id"],), fetch=False)
        if woken:
            log("wecken", f"{woken} Spur(en) durch erstarkendes Thema geweckt: {tname[:40]}")
    return woken

def run_maintenance():
    """Regler + Wecken + Verfall in fester Kadenz (nicht jeden Tick)."""
    _maint_i["n"] += 1
    _reg_i["n"] += 1
    if _reg_i["n"] % REGULATOR["every"] == 0:
        try:
            update_regulator()
        except Exception as e:
            log("regler", f"Fehler: {str(e)[:120]}")
    if _maint_i["n"] % WAKE["every"] != 0:
        return
    try:
        w = wake_traces()
        d = decay_themes()
        if w or d:
            STATUS["woken"] = STATUS.get("woken", 0) + w
            STATUS["decayed"] = STATUS.get("decayed", 0) + d
    except Exception as e:
        log("wartung", f"Fehler: {str(e)[:120]}")

def expectation_model():
    """Aggregiert je Thema DIREKT in SQL (nicht Rohzeilen laden — sonst
    MemoryError bei grossen, verklumpten Themen). Eine Zeile pro Thema."""
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    rows = q("""
        SELECT th.id, th.name, th.created_by, th.keywords,
               COUNT(dt.doc_id) AS docs,
               SUM(CASE WHEN d.source_type='paper'   THEN 1 ELSE 0 END) AS c_paper,
               SUM(CASE WHEN d.source_type='patent'  THEN 1 ELSE 0 END) AS c_patent,
               SUM(CASE WHEN d.source_type='funding' THEN 1 ELSE 0 END) AS c_funding,
               SUM(CASE WHEN d.source_type='news'    THEN 1 ELSE 0 END) AS c_news,
               SUM(CASE WHEN d.published_at > ? THEN 1 ELSE 0 END) AS recent,
               AVG(d.relevance) AS avg_rel
        FROM themes th
        LEFT JOIN doc_themes dt ON dt.theme_id = th.id
        LEFT JOIN documents d ON d.id = dt.doc_id AND d.dup_of IS NULL
        WHERE th.excluded=0
        GROUP BY th.id
        ORDER BY recent DESC, docs DESC
        LIMIT 400""", (cutoff,))
    out = []
    for r in rows:
        n = r["docs"] or 0
        counts = {"paper": r["c_paper"] or 0, "patent": r["c_patent"] or 0,
                  "funding": r["c_funding"] or 0, "news": r["c_news"] or 0}
        try:
            kws = json.loads(r["keywords"])
        except Exception:
            kws = []
        out.append(dict(id=r["id"], name=r["name"], created_by=r["created_by"],
                        keywords=kws, docs=n,
                        stages=[s for s in STAGES if counts.get(s)],
                        stage_counts=counts, momentum=r["recent"] or 0,
                        gap=round(1 - counts["news"] / n, 2) if n else None,
                        avg_relevance=round(r["avg_rel"], 2) if r["avg_rel"] is not None else None))
    return out

# ==================================================================
#  SEED  (Quellen-Registry + Start-Ontologie; KEINE Demo-Dokumente)
# ==================================================================
def ensure_seed():
    seed_broad_anker()                       # Anker in meta sichtbar/editierbar machen (idempotent, vor early-return)
    if q("SELECT COUNT(*) c FROM sources")[0]["c"] > 0:
        return
    for name, kind, st, ep, note in [
        ("Google News (suchgesteuert)", "gnews", "news", None,
         "BREITE — aggregiert tausende Quellen; folgt der Ontologie"),
        ("The Guardian (Open Platform)", "guardian", "news", None,
         "komplementaer — nur Wirtschafts-/Tech-Ressorts"),
        ("Wissenschaft (OpenAlex)", "openalex", "paper", None,
         "frei, kein Key"),
        ("Preprints (arXiv)", "arxiv", "paper", None,
         "frei, kein Key"),
        ("Biomed-Forschung (Europe PMC)", "europepmc", "paper", None,
         "komplementaer — Life-Science, frei, kein Key"),
        ("Publikationen (Semantic Scholar)", "semanticscholar", "paper", None,
         "200M+ Publikationen, frei, kein Key — alle Disziplinen"),
        ("Volltext-Aggregator (CORE)", "core", "paper", None,
         "49M+ Volltexte aus 14.500 Repositorien — Key in core_key.txt"),
        ("Patente (EPO OPS, international)", "epo", "patent", None,
         "EPO/WIPO/national via INPADOC — kostenlose Zugangsdaten in config.txt "
         "(developers.epo.org). Reifestufe Paper->Patent->Funding->News."),
        ("Frühfinanzierung (EDGAR Form D)", "edgar", "funding", None,
         "frei — User-Agent-Kontakt in Datei setzen!"),
        ("US-Pflichtmeldungen (EDGAR 8-K)", "edgar8k", "news", None,
         "US-Aequivalent zur Ad-hoc — wesentliche Unternehmensereignisse, "
         "Ereignis-Signal. Bei Fehler: UA-Kontakt pruefen."),
        ("Wirtschaftsnews (Tagesschau)", "rss", "news",
         "https://www.tagesschau.de/xml/rss2/",
         "allgemeine News — Endpoint in der GUI aenderbar (z.B. Handelsblatt)"),
        ("Ad-hoc-Mitteilungen (Pflichtmeldungen)", "rss", "news",
         "https://www.ad-hoc-news.de/rss/adhocnews.xml",
         "echte Ad-hoc-Publizitaet boersennotierter Firmen — Ereignis-Signal, "
         "hohe Erwartungswirkung"),
        ("Tech-News (Ars Technica)", "rss", "news",
         "https://feeds.arstechnica.com/arstechnica/index",
         "frei — tiefe Technik/Science/Policy"),
        ("Tech-News (The Verge)", "rss", "news",
         "https://www.theverge.com/rss/index.xml",
         "frei — Tech/AI/Policy, hohe Frequenz"),
        ("Startup-Funding (TechCrunch)", "rss", "funding",
         "https://techcrunch.com/feed/",
         "frei, Volltext — Finanzierungsrunden/VC/Launches"),
        ("Ingenieur-News (IEEE Spectrum)", "rss", "paper",
         "https://spectrum.ieee.org/feeds/feed.rss",
         "frei — Halbleiter/Robotik/Energie/Raumfahrt"),
        ("Forschung (MIT News)", "rss", "paper",
         "https://news.mit.edu/rss/feed",
         "frei — Forschungsmeldungen einer Spitzenuni"),
        ("Hacker News (100+ Punkte)", "rss", "news",
         "https://hnrss.org/frontpage?points=100",
         "frei — Fruehsignale zu Tools/Tech, nach Relevanz gefiltert"),
        ("US-Notenbank (Fed, Pressemitteilungen)", "rss", "news",
         "https://www.federalreserve.gov/feeds/press_all.xml",
         "Zentralbank-Ereignisse erster Guete — Zinsen, Politik, Finanzstabilitaet"),
        ("EZB (Pressemitteilungen)", "rss", "news",
         "https://www.ecb.europa.eu/rss/press.html",
         "EZB — bei Fehler Endpoint pruefen (RSS-URL kann abweichen)"),
        ("Wirtschaftsanalyse (Marginal Revolution)", "rss", "news",
         "https://marginalrevolution.com/feed",
         "oekonomische Fachdiskussion/Fruehsignale"),
        ("Konjunktur (Calculated Risk)", "rss", "news",
         "https://www.calculatedriskblog.com/feeds/posts/default?alt=rss",
         "Makro-/Konjunkturdaten und -analyse"),
        # --- Unabhaengige Fachanalyse (Substack & Co.) ----------------------
        # Meinung/Fruehdiskussion statt Fakten: rauschiger als Fed/EZB, aber oft
        # WEIT vor dem Mainstream — genau die Konsens-Distanz, um die es geht.
        ("Net Interest (Finanzsektor)", "rss", "news",
         "https://www.netinterest.co/feed",
         "Banken, Kreditmaerkte, Private Credit — Fachanalyse des Finanzsystems"),
        ("The Overshoot (Makro/Finanzzyklen)", "rss", "news",
         "https://theovershoot.co/feed",
         "Matthew Klein — globale Makrotrends und Finanzzyklen"),
        ("Doomberg (Energie/Makro-Risiko)", "rss", "news",
         "https://newsletter.doomberg.com/feed",
         "Energie, Rohstoffe, Geopolitik — datengetrieben, kontraer"),
        ("Chartbook (Adam Tooze)", "rss", "news",
         "https://adamtooze.substack.com/feed",
         "Oekonomie, Geopolitik, Geschichte"),
        ("Noahpinion (Makro/Industriepolitik)", "rss", "news",
         "https://www.noahpinion.blog/feed",
         "Makro, Industriepolitik, Wachstum"),
        ("Fabricated Knowledge (Halbleiter)", "rss", "news",
         "https://www.fabricatedknowledge.com/feed",
         "Halbleiter/KI-Capex — Technologie trifft Kapital"),
        ("Apricitas Economics (Daten)", "rss", "news",
         "https://www.apricitas.io/feed",
         "datengetriebene Wirtschaftsanalyse"),
        ("Money and Macro (Geldpolitik)", "rss", "news",
         "https://moneyandmacro.substack.com/feed",
         "Geldpolitik und Maerkte"),
        # --- GROSSE STIMMEN (hohe Reichweite) -------------------------------
        # ACHTUNG: Konsensnaehe ist KEINE Eigenschaft der Quelle, sondern der
        # einzelnen AUSSAGE. Krugman ist meist Mainstream — aber etwa beim
        # EU-vs-US-Wachstum dezidiert kontraer. Deshalb wird hier NICHTS
        # quellenbasiert gewichtet: Stufe 4 urteilt je Aussage. Der Nutzen
        # dieser Quellen ist doppelt: sie zeigen, wo der Konsens steht UND
        # liefern gelegentlich echte Gegenthesen.
        ("Project Syndicate (Nobelpreistraeger & Co.)", "rss", "news",
         "https://www.project-syndicate.org/rss",
         "Op-eds von Nobelpreistraegern, Ex-Zentralbankern, Politikern "
         "(u.a. El-Erian, Stiglitz, Rogoff, Shiller) — meist Konsens, "
         "aber nicht immer: einzelne Thesen koennen weit davon abweichen."),
        ("Paul Krugman (Nobelpreistraeger)", "rss", "news",
         "https://paulkrugman.substack.com/feed",
         "Makro-Kommentar, hunderttausende Leser. Meist Mainstream, aber "
         "punktuell dezidiert kontraer (z.B. EU- vs. US-Wachstum)."),
        ("Econbrowser (Chinn/Hamilton)", "rss", "news",
         "https://econbrowser.com/feed",
         "akademische Makro-Oekonomen zu Politik und Daten"),
        ("Peterson Institute (PIIE)", "rss", "news",
         "https://www.piie.com/rss/update.xml",
         "Handel und internationale Finanzen — Denkfabrik"),
        # --- FRUEHE FORSCHUNG (Gegenpol zum Konsens) ------------------------
        ("CEPR Discussion Papers", "rss", "paper",
         "https://cepr.org/rss/discussion-paper",
         "europaeische Wirtschaftsforschung VOR der Publikation — Fruehsignal"),
        # --- Zentralbank-Politik (Diskussion, nicht nur Beschluesse) ---
        ("Zentralbank-Reden weltweit (BIS)", "rss", "news",
         "https://www.bis.org/doclist/cbspeeches.rss",
         "BIS aggregiert Reden ALLER Notenbanken — Politik-Diskussion, Fruehsignal"),
        ("Fed Reden & Testimony", "rss", "news",
         "https://www.federalreserve.gov/feeds/speeches.xml",
         "Fed-Reden — Politikdiskussion vor den Beschluessen"),
        ("Fed Geldpolitik (Beschluesse)", "rss", "news",
         "https://www.federalreserve.gov/feeds/press_monetary.xml",
         "FOMC-Beschluesse/Protokolle"),
        # --- Finanzbranche: gezielte Themenfeeds (Google News) ---
        ("Private Credit / Direct Lending", "gnews_topic", "news",
         "private credit OR direct lending OR \"leveraged loan\"",
         "Themenfeed — Kreditqualitaet, Ausfaelle, Fondsstress"),
        ("M&A / Uebernahmen", "gnews_topic", "news",
         "merger OR acquisition OR takeover bid OR \"M&A deal\"",
         "Themenfeed — Uebernahmen, Fusionen"),
        ("Unternehmensfinanzierung", "gnews_topic", "news",
         "corporate financing OR bond issuance OR capital raise OR refinancing",
         "Themenfeed — Anleihen, Kapitalerhoehungen, Refinanzierung"),
        ("Firmen-Funding / Venture", "gnews_topic", "news",
         "funding round OR Series A OR Series B OR venture funding",
         "Themenfeed — Finanzierungsrunden von Unternehmen"),
        ("Zentralbank-Politik (Debatte)", "gnews_topic", "news",
         "central bank policy OR interest rate decision OR monetary policy",
         "Themenfeed — Zinsdebatte, Politikerwartungen"),
    ]:
        # CORE braucht einen Key -> standardmaessig aus, damit es nicht
        # jeden Scan mit Fehlern flutet. In der GUI einschalten, wenn Key da.
        enabled = 0 if kind == "core" else 1
        q("INSERT INTO sources(name,kind,source_type,endpoint,enabled,"
          "rate_per_hour,note) VALUES(?,?,?,?,?,?,?)",
          (name, kind, st, ep, enabled, RATE_DEFAULTS.get(kind, 20), note),
          fetch=False)
    for name, kws in [
        ("KI-Strombedarf", ["data center power", "ai power demand", "grid strain",
                            "datacenter electricity"]),
        ("Halbleiter-Lieferkette", ["semiconductor", "chip supply", "substrate",
                                    "foundry capacity"]),
        ("Verteidigungstechnik", ["defense", "drone", "counter-uas", "munition"]),
        ("Batterien & Speicher", ["battery", "grid storage", "solid-state",
                                  "energy storage"]),
    ]:
        q("INSERT INTO themes(name,keywords) VALUES(?,?)",
          (name, json.dumps(kws)), fetch=False)
    log("seed", "Registry (6 Quellen) + Start-Ontologie (4 Themen). "
                "Dokumente kommen NUR aus Scans.")
# ==================================================================
#  OBERFLAECHE  (Kuratier-Konsole)
# ==================================================================
UI = r"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Modul 1 · Autonomer Sammler</title>
<style>
:root{--tiefe:#161C26;--panel:#1E2733;--rand:#2C3747;--schrift:#E7ECF3;
--gedaempft:#93A0B4;--leise:#5E6B7E;--bernstein:#E2A64B;--stahl:#7FA7CE;
--feuert:#D9706C;--ruhig:#74BE93;
--mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box;margin:0}
body{background:var(--tiefe);color:var(--schrift);font-family:var(--sans);
font-size:15px;line-height:1.5}
header{display:flex;align-items:center;justify-content:space-between;gap:14px;
padding:15px 26px;border-bottom:1px solid var(--rand);flex-wrap:wrap}
.marke{font-family:var(--mono);letter-spacing:.16em;font-size:12px;
color:var(--gedaempft);text-transform:uppercase}
.marke b{color:var(--schrift)}
.status{font-family:var(--mono);font-size:11px;color:var(--leise)}
.steuer{display:flex;gap:10px;align-items:center;font-family:var(--mono);font-size:12px}
.steuer select{background:var(--panel);color:var(--schrift);
border:1px solid var(--rand);border-radius:4px;padding:5px 8px;
font-family:var(--mono);font-size:12px}
.knopf{padding:8px 15px;border-radius:6px;border:1px solid var(--bernstein);
background:var(--bernstein);color:#1A1408;font-family:var(--mono);font-size:11px;
letter-spacing:.12em;text-transform:uppercase;font-weight:700;cursor:pointer}
.knopf:hover{background:#EBB566}
.knopf.leise{background:transparent;color:var(--gedaempft);border-color:var(--rand)}
.knopf.rot{background:transparent;color:var(--feuert);border-color:var(--feuert)}
.knopf:disabled{opacity:.5;cursor:wait}
.mini{padding:4px 10px;font-size:9.5px}
.raster{display:grid;grid-template-columns:390px 1fr;min-height:calc(100vh-58px)}
@media(max-width:960px){.raster{grid-template-columns:1fr}}
.links{border-right:1px solid var(--rand);padding:20px;overflow-y:auto}
.haupt{padding:20px 26px;max-width:980px}
.augenbraue{font-family:var(--mono);font-size:10.5px;letter-spacing:.2em;
text-transform:uppercase;color:var(--leise);margin:22px 0 10px}
.augenbraue:first-child{margin-top:0}
.kachel{background:var(--panel);border:1px solid var(--rand);border-radius:6px;
padding:11px 13px;margin-bottom:9px}
.zeile{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.qname{font-size:13.5px;font-weight:600}
.qnote{font-size:11px;color:var(--leise);margin-top:2px}
.qstat{font-family:var(--mono);font-size:10px;color:var(--gedaempft);margin-top:4px}
.qerr{font-family:var(--mono);font-size:10px;color:var(--feuert);margin-top:3px;word-break:break-all}
.schalter{font-family:var(--mono);font-size:10px;letter-spacing:.08em;
text-transform:uppercase;padding:4px 10px;border-radius:20px;cursor:pointer;
border:1px solid var(--rand);background:transparent;color:var(--leise)}
.schalter.an{border-color:var(--ruhig);color:var(--ruhig)}
.schalter.aus{border-color:var(--feuert);color:var(--feuert)}
.abzeichen{font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:12px;
border:1px solid var(--stahl);color:var(--stahl);text-transform:uppercase}
.abzeichen.maschine{border-color:var(--bernstein);color:var(--bernstein)}
.aktion{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.endpoint{width:100%;margin-top:6px;background:var(--tiefe);color:var(--gedaempft);
border:1px solid var(--rand);border-radius:4px;padding:5px 8px;
font-family:var(--mono);font-size:10.5px}
.leiter{display:flex;gap:4px;margin-top:7px}
.stufe{flex:1;height:5px;border-radius:3px;background:#39445A;position:relative}
.stufe.erreicht{background:var(--stahl)}
.leiter-namen{display:flex;gap:4px;margin-top:3px}
.leiter-namen div{flex:1;font-family:var(--mono);font-size:8.5px;
color:var(--leise);text-align:center}
.metriken{display:flex;gap:12px;margin-top:7px;font-family:var(--mono);
font-size:10.5px;color:var(--gedaempft);flex-wrap:wrap}
.metriken b{color:var(--schrift)}
.dok{background:var(--panel);border:1px solid var(--rand);border-radius:6px;
padding:10px 13px;margin-bottom:8px}
.dok .titel{font-size:13px;font-weight:600;line-height:1.4}
.dok .meta{font-family:var(--mono);font-size:10px;color:var(--leise);margin-top:3px}
.dok .steuerzeile{display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap}
.relwert{width:70px;background:var(--tiefe);color:var(--schrift);
border:1px solid var(--rand);border-radius:4px;padding:4px 7px;
font-family:var(--mono);font-size:12px}
.rellabel{font-family:var(--mono);font-size:10px;color:var(--gedaempft);
text-transform:uppercase;letter-spacing:.08em}
.protokoll{font-family:var(--mono);font-size:10.5px;color:var(--gedaempft);
background:var(--panel);border:1px solid var(--rand);border-radius:6px;
padding:10px 12px;max-height:240px;overflow-y:auto;white-space:pre-wrap}
.leer{color:var(--leise);font-size:13px;padding:18px;text-align:center;
border:1px dashed var(--rand);border-radius:8px}
.filter{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.filter select{background:var(--panel);color:var(--schrift);
border:1px solid var(--rand);border-radius:4px;padding:6px 9px;
font-family:var(--mono);font-size:11.5px}
.hinweis-llm{font-family:var(--mono);font-size:10px;padding:3px 9px;
border-radius:12px;border:1px solid var(--leise);color:var(--leise)}
.hinweis-llm.an{border-color:var(--ruhig);color:var(--ruhig)}
.livebar{display:flex;align-items:center;gap:10px;padding:9px 26px;
background:var(--panel);border-bottom:1px solid var(--rand);
font-family:var(--mono);font-size:12px}
.puls{width:10px;height:10px;border-radius:50%;background:var(--leise);flex:none}
.puls.busy{background:var(--bernstein);animation:pulsieren 1s infinite}
.puls.ruht{background:var(--ruhig)}
@keyframes pulsieren{0%,100%{opacity:1}50%{opacity:.3}}
.livebar .phase{color:var(--schrift);font-weight:700;text-transform:uppercase;
letter-spacing:.08em}
.livebar .detail{color:var(--gedaempft)}
.livebar .zeit{margin-left:auto;color:var(--leise)}
@media (prefers-reduced-motion:reduce){.puls.busy{animation:none}*{transition:none!important}}
</style></head><body>

<header>
 <div>
  <div class="marke">Modul 1 · <b>Autonomer Sammler</b> — Erwartungsbild der Wirtschaft</div>
  <div class="status" id="status">—</div>
 </div>
 <div class="steuer">
  <span class="hinweis-llm" id="llm">LLM?</span>
  <span class="hinweis-llm" id="lokal">Lokal?</span>
  <button class="knopf" id="colltoggle">Sammler starten</button>
  <button class="knopf leise" id="tick">Ein Schritt</button>
  <button class="knopf leise" id="reeval">Neu bewerten</button>
  <button class="knopf leise" id="facts-test">1c Test</button>
  <button class="knopf leise" id="facts-all">1c: alle Fakten</button>
  <label>Tempo <select id="speed">
   <option value="10">alle 10 s</option>
   <option value="30" selected>alle 30 s</option>
   <option value="60">jede Minute</option>
   <option value="300">alle 5 min</option>
  </select></label>
 </div>
</header>

<div class="livebar">
 <div class="puls" id="puls"></div>
 <span class="phase" id="lv-phase">—</span>
 <span class="detail" id="lv-detail"></span>
 <span class="zeit" id="lv-zeit"></span>
</div>

<div class="raster">
<aside class="links">
 <div class="augenbraue">Quellen — ausschalten · testen · Endpoint ändern</div>
 <div id="quellen"></div>

 <details style="margin-top:14px">
  <summary>+ Neue Quelle hinzufügen</summary>
  <form id="srcform">
   <label>Name</label>
   <input id="s-name" placeholder="z. B. Reuters Technology">
   <label>Art</label>
   <select id="s-kind">
    <option value="rss">RSS / Atom-Feed (URL nötig)</option>
    <option value="gnews">Google-News-Suche (kein Endpoint)</option>
    <option value="guardian">Guardian (kein Endpoint)</option>
    <option value="semanticscholar">Semantic Scholar (kein Endpoint)</option>
    <option value="europepmc">Europe PMC (kein Endpoint)</option>
    <option value="patents">USPTO Patente (Endpoint optional)</option>
   </select>
   <label>Signaltyp</label>
   <select id="s-type">
    <option value="news">news — Ereignis</option>
    <option value="paper">paper — Wissenschaft</option>
    <option value="patent">patent</option>
    <option value="funding">funding — Finanzierung</option>
   </select>
   <label id="s-eplabel">Endpoint-URL (nur bei RSS)</label>
   <input id="s-endpoint" placeholder="https://…/feed.xml">
   <div style="display:flex;gap:8px;margin-top:12px">
    <button type="button" class="knopf leise" id="s-testbtn">Erst testen</button>
    <button type="submit" class="knopf" id="s-savebtn">Hinzufügen</button>
   </div>
   <div class="meldung" id="s-msg" style="margin-top:8px;font-family:var(--mono);font-size:11.5px"></div>
  </form>
 </details>

 <details style="margin-top:14px">
  <summary>Lokales Modell (Ollama) · Kosten sparen</summary>
  <div style="font-size:11.5px;color:var(--gedaempft);margin:8px 0">
   Hochvolumige Arbeit (Relevanz, Ontologie) laeuft lokal & gratis; das
   Frontier-LLM bleibt fuer spaetere hochwertige Analyse.</div>
  <label>Ollama-URL</label>
  <input id="lo-url" placeholder="http://localhost:11434">
  <label>Modellname</label>
  <input id="lo-model" placeholder="qwen3:30b">
  <div style="display:flex;gap:8px;margin-top:10px">
   <button type="button" class="knopf leise" id="lo-test">Testen</button>
   <button type="button" class="knopf" id="lo-save">Übernehmen</button>
  </div>
  <div id="lo-msg" style="margin-top:8px;font-family:var(--mono);font-size:11px"></div>
  <div style="margin-top:12px;border-top:1px solid var(--rahmen,#2a3550);padding-top:10px">
   <label>Ollama-Dienst</label>
   <div id="ol-status" style="font-family:var(--mono);font-size:11.5px;margin:4px 0">Status: —</div>
   <div style="display:flex;gap:8px">
    <button type="button" class="knopf" id="ol-start">Ollama starten</button>
    <button type="button" class="knopf leise" id="ol-stop">Ollama stoppen</button>
   </div>
   <div id="ol-msg" style="margin-top:6px;font-family:var(--mono);font-size:11px;color:var(--gedaempft)"></div>
  </div>
  <div style="margin-top:12px">
   <label>Relevanz-Bewertung</label>
   <select id="rt-relevance"><option value="local">lokal (gratis)</option>
    <option value="frontier">Frontier (bezahlt)</option></select>
   <label style="margin-top:8px">Ontologie-Bildung</label>
   <select id="rt-ontology"><option value="local">lokal (gratis)</option>
    <option value="frontier">Frontier (bezahlt)</option></select>
  </div>
  <label style="display:flex;align-items:center;gap:8px;margin-top:12px;
   font-size:12px;cursor:pointer">
   <input type="checkbox" id="fr-allow" style="width:auto">
   Frontier-LLM (Anthropic) erlauben — <b>kostet Geld</b></label>
  <div style="font-size:10.5px;color:var(--leise);margin-top:4px">
   Aus: Wenn das lokale Modell nicht antwortet, wird NICHT bezahlt —
   die Aufgabe wird übersprungen statt still Kosten zu erzeugen.</div>
 </details>

 <div class="augenbraue">Protokoll</div>
 <div class="protokoll" id="protokoll"></div>
</aside>

<main class="haupt">
 <div class="augenbraue">Fakten (Modul 1c) — Subjekt · Beziehung · Objekt · ist/wird · Achsen</div>
 <div id="fakten"></div>

 <div class="augenbraue">Erwartungsbild — die gewachsene Ontologie</div>
 <div id="ontologie"></div>

 <div class="augenbraue" style="margin-top:30px">Dokumente — Bewertung ändern · löschen</div>
 <div class="filter">
  <select id="fthema"><option value="">alle Themen</option></select>
  <select id="ftyp"><option value="">alle Typen</option>
   <option value="paper">Wissenschaft</option><option value="patent">Patent</option>
   <option value="funding">Frühfinanzierung</option><option value="news">News</option>
  </select>
 </div>
 <div id="dokumente"></div>
</main>
</div>

<script>
const $=id=>document.getElementById(id);
const STUFEN=["paper","patent","funding","news"];
const KURZ={"paper":"Wiss.","patent":"Patent","funding":"Funding","news":"News"};
let Z=null;

async function post(u,b){return (await fetch(u,{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}

async function laden(){
 Z=await (await fetch('/api/state')).json();
 $('llm').textContent=Z.llm?(Z.frontier_allowed?'Frontier AN':'Frontier gesperrt'):'kein Key';
 $('llm').title=Z.frontier_allowed?'Frontier-LLM erlaubt — kann Kosten verursachen'
   :(Z.llm?'Key vorhanden, aber Frontier gesperrt (nur lokal/gratis)':'Kein Anthropic-Key');
 $('llm').className='hinweis-llm'+(Z.frontier_allowed?' an':'');
 const lo=Z.local||{};
 $('lokal').textContent=lo.available?('Lokal: '+(lo.model||'?')):'Lokal aus';
 $('lokal').title=lo.available?('Ollama erreichbar unter '+lo.url):'Ollama nicht erreichbar — Mock/Frontier';
 $('lokal').className='hinweis-llm'+(lo.available?' an':'');
 const c=Z.collector||{};
 $('status').textContent=`Sammler: ${c.running?'läuft':'gestoppt'} · Schritt alle ${c.tick_seconds}s · Entdeckung ${Math.round((c.discovery_ratio||0.7)*100)}% · Dokumente: ${Z.n_docs} · Spuren: ${Z.n_traces||0}`;
 const st=Z.status||{};
 if(st.seen)$('status').textContent+=` · Strom: ${st.seen} gesehen → ${st.kept} behalten / ${st.traced} Spur / ${st.dropped} verworfen`;
 if(st.woken||st.decayed)$('status').textContent+=` · geweckt: ${st.woken||0} · verfallen: ${st.decayed||0}`;
 const rg=Z.regulator;
 if(rg&&rg.enabled)$('status').textContent+=` · Regler: ${rg.state} (Paper×${rg.paper_factor} News×${rg.news_factor}, ΔRel ${rg.incr>=0?'+':''}${rg.incr})`;
 const g=Z.gpu;
 if(g&&g.enabled&&g.temp!=null)$('status').textContent+=
   ` · GPU ${g.temp}°C/${g.max_temp}${g.paused?' ('+g.paused+'x pausiert)':''}`;
 const ol=Z.ollama;
 if(ol&&!ol.ok){$('status').textContent =
   `⚠ OLLAMA WEG: ${ol.note} — Sammler pausiert (schützt die DB vor Mock-Daten). `
   + $('status').textContent;
   $('status').style.color='var(--warn)'; return;}
 const wd=Z.watchdog;
 if(wd&&wd.note)$('status').textContent+=` · ⚠ ${wd.note}`;
 if(wd&&wd.restarts)$('status').textContent+=` · Watchdog-Neustarts: ${wd.restarts}`;
 if(Z.model_alarm){$('status').textContent =
   `⚠ MODELL FEHLT: Ollama kennt „${Z.model_alarm}" nicht — es wird NICHT echt bewertet `
   +`(Mock!). Beheben:  ollama pull ${Z.model_alarm}  · ` + $('status').textContent;
   $('status').style.color='var(--warn)';}
 else{$('status').style.color='';}
 $('speed').value=String(c.tick_seconds||30);
 $('colltoggle').textContent=c.running?'Sammler stoppen':'Sammler starten';
 $('colltoggle').className='knopf'+(c.running?' rot':'');
 const rv=Z.reeval||{};
 if(rv.running){$('reeval').textContent=`Neu bewerten… ${rv.done}/${rv.total}`;
   $('reeval').className='knopf rot';}
 else{$('reeval').textContent='Neu bewerten';$('reeval').className='knopf leise';}
 const fx=Z.facts||{};
 if(fx.running){$('facts-test').textContent=`1c läuft… ${fx.done}/${fx.total}`;
   $('facts-test').className='knopf rot';$('facts-all').style.display='none';}
 else{$('facts-test').textContent='1c Test';$('facts-test').className='knopf leise';
   $('facts-all').style.display='';$('facts-all').textContent='1c: alle Fakten';}
 zLive();zQuellen();zFakten();zOntologie();zDokumente();zLog();fillLocal();
}

function zFakten(){
 const f=Z.facts||{}, el=$('fakten');
 if(!el) return;
 const ges=Z.facts_total||0, erl=Z.facts_erledigt||0;
 const prog = f.running
   ? `<span style="color:var(--warn)">läuft: ${erl}/${ges} Dokumente erledigt `
     +`(${ges?Math.round(100*erl/ges):0}%) · ${Z.n_facts||0} Fakten · +${f.done} in diesem Lauf</span>`
   : `${Z.n_facts||0} Fakten aus ${erl}/${ges} Dokumenten (${ges?Math.round(100*erl/ges):0}% erledigt)`
     +`${f.phase==='fertig'?' · letzter Lauf fertig':''}`;
 let rows='';
 for(const x of (Z.fact_samples||[]).slice(0,25)){
   const modus = x.modus==='wird'
     ? '<b style="color:var(--warn)">wird</b>' : '<b style="color:var(--gut)">ist</b>';
   rows += `<tr>
     <td>${(x.subjekt||'').slice(0,40)}</td>
     <td style="color:var(--gedaempft)">${(x.beziehung||'').slice(0,26)}</td>
     <td>${(x.objekt||'').slice(0,40)}</td>
     <td>${modus}</td>
     <td>${x.signalart==='technologie'?'🔬 Tech':'⚡ Ereignis'}</td>
     <td>${x.reife||'—'} <span style="color:var(--gedaempft)">${x.reife_score!=null?'('+x.reife_score+')':''}</span></td>
     <td>Latenz ${x.latenz||'—'}</td>
     <td>Tempo ${x.erwartungstempo!=null?x.erwartungstempo:'—'}</td>
   </tr>`;
 }
 el.innerHTML = `<div style="margin-bottom:6px">${prog}</div>` + (rows
   ? `<table class="tab"><thead><tr><th>Subjekt</th><th>Beziehung</th><th>Objekt</th>
      <th>Modus</th><th>Signalart</th><th>Reife (Achse 1)</th><th>Latenz</th>
      <th>Erw.-Tempo (Achse 2)</th></tr></thead><tbody>${rows}</tbody></table>`
   : '<div style="color:var(--gedaempft)">Noch keine Fakten. „1c Test" verarbeitet '
     +'20 Dokumente über Frontier zur Prüfung; „1c: alle Fakten" den ganzen Bestand.</div>');
}

function zLive(){
 const s=Z.status||{};
 $('puls').className='puls '+(s.busy?'busy':'ruht');
 $('lv-phase').textContent=s.phase||'—';
 $('lv-detail').textContent=s.detail||'';
 $('lv-zeit').textContent=(s.since?'seit '+s.since:'')
   +(s.scans_done?' · Scans: '+s.scans_done:'');
}

function cursorText(s){
 if(!s.cursor) return '<span style="color:var(--leise)">Cursor: frisch/Anfang</span>';
 try{const c=JSON.parse(s.cursor);
  if('ti' in c) return `Cursor: Begriff #${c.ti}`;
  if('start' in c) return `Cursor: Position ${c.start}`;
  if('page' in c) return `Cursor: Seite ${c.page}`;
  if('from' in c) return `Cursor: ab ${c.from}`;
  return 'Cursor: '+s.cursor.slice(0,24);
 }catch(e){return 'Cursor: —';}
}
function budgeteText(s){
 return s.total_queries?` · seit Start ${s.total_queries}×`:'';
}

function zQuellen(){
 const b=$('quellen');b.innerHTML='';
 for(const s of Z.sources){
  b.insertAdjacentHTML('beforeend',`<div class="kachel">
   <div class="zeile"><div><span class="qname">${s.name}</span>
    <div class="qnote">${s.note||''}</div></div>
    <button class="schalter ${s.enabled?'an':'aus'} s-toggle" data-id="${s.id}"
     data-en="${s.enabled}">${s.enabled?'aktiv':'aus'}</button></div>
   ${s.endpoint!=null?`<input class="endpoint s-ep" data-id="${s.id}" value="${s.endpoint||''}"
     title="Endpoint — ändern und Enter drücken">`:''}
   <div class="qstat">zuletzt ${s.last_crawl||'—'} · gefunden ${s.last_found} · behalten ${s.last_kept} · Abfragen gesamt ${s.total_queries||0}</div>
   ${s.n_geprueft ? `<div class="qstat">Konsens-Distanz (gemessen): <b>${Math.round(100*s.n_signal/s.n_geprueft)}%</b> Nicht-Konsens
      <span style="color:var(--gedaempft)">· ${s.n_trivial} Allgemeinwissen, ${s.n_einzelfall} Einzelfall, ${s.n_geprueft} geprüft</span></div>` : ''}
   <div class="qstat">${cursorText(s)} · <span class="rellabel">Rate/h</span>
     <input class="relwert s-rate" data-id="${s.id}" type="number" min="1" max="500"
      value="${s.rate_per_hour||20}" title="Abfragen pro Stunde — ändern und Enter"
      style="width:56px">${budgeteText(s)}</div>
   ${s.paused_until&&new Date(s.paused_until)>new Date()?`<div class="qerr" style="color:var(--bernstein)">pausiert bis ${s.paused_until.slice(11,16)} (zu viele Fehler) — „Testen" hebt auf</div>`:''}
   ${s.last_error?`<div class="qerr">Fehler: ${s.last_error}</div>`:''}
   <div class="aktion">
    <button class="knopf leise mini s-test" data-id="${s.id}">Testen</button>
    <button class="knopf rot mini s-purge" data-id="${s.id}">Dokumente löschen</button>
    <button class="knopf rot mini s-remove" data-id="${s.id}" data-name="${s.name}">Quelle entfernen</button>
   </div></div>`);
 }
 b.querySelectorAll('.s-remove').forEach(k=>k.onclick=async()=>{
  if(confirm(`Quelle „${k.dataset.name}" komplett entfernen?\n\n`
   +`Die Quelle und alle ihre Dokumente werden gelöscht.`)){
   await post('/api/source_remove',{id:+k.dataset.id});laden();}});
 b.querySelectorAll('.s-rate').forEach(inp=>inp.onkeydown=async ev=>{
  if(ev.key==='Enter'){await post('/api/source_rate',
   {id:+inp.dataset.id,rate_per_hour:+inp.value});laden();}});
 b.querySelectorAll('.s-toggle').forEach(k=>k.onclick=async()=>{
  await post('/api/source',{id:+k.dataset.id,enabled:k.dataset.en==1?0:1});laden();});
 b.querySelectorAll('.s-ep').forEach(inp=>inp.onkeydown=async ev=>{
  if(ev.key==='Enter'){await post('/api/source',{id:+inp.dataset.id,endpoint:inp.value});laden();}});
 b.querySelectorAll('.s-test').forEach(k=>k.onclick=async()=>{
  k.disabled=true;k.textContent='testet…';
  const r=await post('/api/source_test',{id:+k.dataset.id});
  alert(r.ok?`OK — ${r.found} Fundstellen. Beispiel: ${r.sample||'—'}`:`Fehler: ${r.error}`);
  k.disabled=false;k.textContent='Testen';laden();});
 b.querySelectorAll('.s-purge').forEach(k=>k.onclick=async()=>{
  if(confirm('Alle Dokumente dieser Quelle unwiderruflich löschen?')){
   await post('/api/purge_source',{id:+k.dataset.id});laden();}});
}

function zOntologie(){
 const b=$('ontologie');b.innerHTML='';
 const sel=$('fthema');const merken=sel.value;
 sel.innerHTML='<option value="">alle Themen</option>';
 if(!Z.model.length){b.innerHTML='<div class="leer">Noch keine Themen mit Belegen — starte einen Scan.</div>';}
 for(const t of Z.model){
  sel.insertAdjacentHTML('beforeend',`<option value="${t.id}">${t.name}</option>`);
  const stufen=STUFEN.map(s=>`<div class="stufe ${t.stages.includes(s)?'erreicht':''}"
    title="${KURZ[s]}: ${t.stage_counts[s]}"></div>`).join('');
  const namen=STUFEN.map(s=>`<div>${KURZ[s]} ${t.stage_counts[s]||''}</div>`).join('');
  b.insertAdjacentHTML('beforeend',`<div class="kachel">
   <div class="zeile"><div><span class="qname">${t.name}</span>
     <span class="abzeichen ${t.created_by=='machine'?'maschine':''}">${t.created_by}</span>
     <div class="qnote">${t.keywords.join(' · ')}</div></div>
    <div style="display:flex;gap:6px">
     <button class="knopf leise mini t-ex" data-id="${t.id}">ausschließen</button>
     <button class="knopf rot mini t-del" data-id="${t.id}">löschen</button></div></div>
   <div class="leiter">${stufen}</div><div class="leiter-namen">${namen}</div>
   <div class="metriken"><span>Belege <b>${t.docs}</b></span>
    <span>Momentum (180T) <b>${t.momentum}</b></span>
    <span>Narrativ-Gap <b>${t.gap==null?'—':t.gap.toFixed(2)}</b></span>
    <span>Ø-Relevanz <b>${t.avg_relevance==null?'—':t.avg_relevance.toFixed(2)}</b></span>
   </div></div>`);
 }
 sel.value=merken;
 b.querySelectorAll('.t-ex').forEach(k=>k.onclick=async()=>{
  await post('/api/theme',{id:+k.dataset.id,excluded:1});laden();});
 b.querySelectorAll('.t-del').forEach(k=>k.onclick=async()=>{
  if(confirm('Thema samt Zuordnungen löschen? (Dokumente bleiben)')){
   await post('/api/theme_delete',{id:+k.dataset.id});laden();}});
}

function zDokumente(){
 const b=$('dokumente');b.innerHTML='';
 let docs=Z.docs;
 const th=$('fthema').value, ty=$('ftyp').value;
 if(th)docs=docs.filter(d=>(d.theme_ids||[]).includes(+th));
 if(ty)docs=docs.filter(d=>d.source_type===ty);
 if(!docs.length){b.innerHTML='<div class="leer">Keine Dokumente (Filter oder noch kein Scan).</div>';return;}
 for(const d of docs.slice(0,60)){
  b.insertAdjacentHTML('beforeend',`<div class="dok">
   <div class="titel">${d.url?`<a href="${d.url}" target="_blank" style="color:inherit">${d.title}</a>`:d.title}</div>
   <div class="meta">${KURZ[d.source_type]||d.source_type} · ${d.published_at} · ${d.themes.join(', ')||'ohne Thema'}</div>
   <div class="steuerzeile">
    <span class="rellabel">Relevanz</span>
    <input class="relwert d-rel" data-id="${d.id}" type="number" min="0" max="1"
     step="0.05" value="${(d.relevance??0).toFixed(2)}"
     title="ändern und Enter drücken">
    <button class="knopf rot mini d-del" data-id="${d.id}">löschen</button>
   </div></div>`);
 }
 b.querySelectorAll('.d-rel').forEach(inp=>inp.onkeydown=async ev=>{
  if(ev.key==='Enter'){await post('/api/doc_relevance',
   {id:+inp.dataset.id,relevance:parseFloat(inp.value)});laden();}});
 b.querySelectorAll('.d-del').forEach(k=>k.onclick=async()=>{
  await post('/api/doc_delete',{id:+k.dataset.id});laden();});
}

function zLog(){
 $('protokoll').textContent=Z.log.map(l=>`${l.at.slice(5,16)}  [${l.stage}] ${l.message}`).join('\n');
}

$('colltoggle').onclick=async()=>{
 const running=(Z.collector||{}).running;
 await post(running?'/api/collector_stop':'/api/collector_start',{});laden();};
$('tick').onclick=async()=>{
 $('tick').disabled=true;$('tick').textContent='…';
 await post('/api/tick',{});
 $('tick').disabled=false;$('tick').textContent='Ein Schritt';laden();};
$('speed').onchange=async()=>{
 await post('/api/collector_speed',{tick_seconds:+$('speed').value});laden();};
$('reeval').onclick=async()=>{
 const rv=Z.reeval||{};
 if(rv.running){await post('/api/reeval_stop',{});laden();return;}
 if(confirm('Alle Dokumente mit dem geschärften Prompt NEU bewerten und die '
  +'Ontologie neu aufbauen?\n\nDie Dokumente selbst bleiben erhalten. '
  +'Der Sammler pausiert währenddessen.')){
  await post('/api/reeval_start',{});laden();}};

$('facts-test').onclick=async()=>{
 const f=Z.facts||{};
 if(f.running){await post('/api/facts_stop',{});laden();return;}
 const local = Z.local && Z.local.available;
 if(!local && !Z.frontier_allowed){alert('Modul 1c braucht ein aktives Modell: '
  +'entweder Ollama lokal (läuft der Dienst?) oder Frontier freigeben.');return;}
 const via = local ? ('dem lokalen Modell'+(Z.facts_model?' ('+Z.facts_model+')':''))
   : 'dem Frontier-Modell (kostenpflichtig!)';
 if(confirm('Testlauf: 20 Dokumente über '+via+' auf Fakten prüfen?\n\n'
  +(local?'Läuft lokal — gratis. ':'ACHTUNG: erzeugt Frontier-Kosten. ')
  +'Der Sammler pausiert kurz.')){
  await post('/api/facts_test',{n:20});laden();}};

$('facts-all').onclick=async()=>{
 const f=Z.facts||{};
 if(f.running){await post('/api/facts_stop',{});laden();return;}
 const local = Z.local && Z.local.available;
 if(!local && !Z.frontier_allowed){alert('Modul 1c braucht ein aktives Modell: '
  +'entweder Ollama lokal oder Frontier freigeben.');return;}
 const warn = local
   ? 'Läuft lokal über '+(Z.facts_model||'das lokale Modell')+' — gratis, aber bei '
     +'zehntausenden Dokumenten dauert es (Stunden). Der Lauf ist stoppbar und setzt fort.'
   : 'ACHTUNG: läuft über FRONTIER — bei zehntausenden Dokumenten SPÜRBARE Kosten!';
 if(confirm('ALLE noch nicht verarbeiteten Dokumente auf Fakten prüfen?\n\n'+warn
  +'\n\nErst „1c Test" prüfen! Der Sammler pausiert.')){
  await post('/api/facts_all',{});laden();}};

// --- Lokales Modell ---
function fillLocal(){
 const lo=Z.local||{}, rt=Z.routing||{};
 if(document.activeElement!==$('lo-url')) $('lo-url').value=lo.url||'';
 if(document.activeElement!==$('lo-model')) $('lo-model').value=lo.model||'';
 $('rt-relevance').value=rt.relevance||'local';
 $('rt-ontology').value=rt.ontology||'local';
 $('fr-allow').checked=!!Z.frontier_allowed;
}
$('lo-test').onclick=async()=>{
 const m=$('lo-msg');m.style.color='var(--gedaempft)';m.textContent='teste…';
 const r=await post('/api/local_test',{url:$('lo-url').value,model:$('lo-model').value});
 m.style.color=r.ok?'var(--ruhig)':'var(--feuert)';m.textContent=r.message;laden();};
$('lo-save').onclick=async()=>{
 const r=await post('/api/local_config',{url:$('lo-url').value,model:$('lo-model').value});
 $('lo-msg').style.color='var(--ruhig)';$('lo-msg').textContent='Übernommen: '+r.model;laden();};
// --- Ollama Start/Stop/Status (Jens 08.08.) ---
async function olStatus(){
 try{
  const s=await post('/api/ollama_status',{});
  const gpu=s.gpu_sperre||{};
  const sperre=gpu.belegt?(' · GPU-Sperre: '+(gpu.modell||'?')):'';
  $('ol-status').innerHTML='Status: '+(s.erreichbar
    ?'<b style="color:var(--ruhig)">erreichbar</b>':'<b style="color:var(--feuert)">nicht erreichbar</b>')
    +' · '+(s.model||'?')+' / '+(s.embed_model||'?')+sperre;
 }catch(e){$('ol-status').textContent='Status: unbekannt';}
}
$('ol-start').onclick=async()=>{
 $('ol-msg').textContent='starte…';
 const r=await post('/api/ollama_start',{});
 $('ol-msg').textContent=r.meldung||''; setTimeout(olStatus,1500);};
$('ol-stop').onclick=async()=>{
 if(!confirm('Ollama-Dienst stoppen? Laufende Extraktion/Embeddings brechen ab.'))return;
 $('ol-msg').textContent='stoppe…';
 const r=await post('/api/ollama_stop',{});
 $('ol-msg').textContent=r.meldung||''; setTimeout(olStatus,1000);};
olStatus(); setInterval(olStatus,7000);
$('rt-relevance').onchange=async()=>{await post('/api/routing',{task:'relevance',target:$('rt-relevance').value});laden();};
$('rt-ontology').onchange=async()=>{await post('/api/routing',{task:'ontology',target:$('rt-ontology').value});laden();};
$('fr-allow').onchange=async()=>{
 if($('fr-allow').checked && !confirm('Frontier-LLM (Anthropic) erlauben?\n\n'
  +'Das kostet echtes Guthaben, sobald das lokale Modell nicht verfügbar ist. '
  +'Nur einschalten, wenn du das bewusst willst.')){$('fr-allow').checked=false;return;}
 await post('/api/frontier',{allowed:$('fr-allow').checked});laden();};

$('fthema').onchange=zDokumente;$('ftyp').onchange=zDokumente;

// --- Neue Quelle: Endpoint-Feld je nach Art zeigen/verbergen ---
const srcKind=$('s-kind'), srcEp=$('s-endpoint'), srcEpLabel=$('s-eplabel');
function toggleEp(){
 // RSS braucht eine URL; USPTO-Patente koennen eine haben (sonst Standard).
 const k=srcKind.value;
 const show = (k==='rss' || k==='patents');
 srcEp.style.display=show?'block':'none';
 srcEpLabel.style.display=show?'block':'none';
 srcEpLabel.textContent = k==='patents'
   ? 'Endpoint-URL (optional — leer = Standard)'
   : 'Endpoint-URL (nur bei RSS)';
 if(k==='patents' && !srcEp.value)
   srcEp.placeholder='https://search.patentsview.org/api/v1/patent/';
}
srcKind.onchange=toggleEp; toggleEp();

// --- Erst testen: prueft die Quelle, OHNE sie zu speichern ---
$('s-testbtn').onclick=async()=>{
 const m=$('s-msg'); m.style.color='var(--gedaempft)'; m.textContent='teste…';
 const r=await post('/api/source_probe',{
   kind:srcKind.value, source_type:$('s-type').value, endpoint:srcEp.value});
 if(r.ok){m.style.color='var(--ruhig)';
   m.textContent=`OK — ${r.found} Fundstellen. Beispiel: ${(r.sample||'—').slice(0,70)}`;}
 else{m.style.color='var(--feuert)'; m.textContent='Fehler: '+(r.error||'unbekannt');}
};

// --- Hinzufuegen ---
$('srcform').onsubmit=async ev=>{
 ev.preventDefault();
 const m=$('s-msg');
 if(!$('s-name').value.trim()){m.style.color='var(--feuert)';m.textContent='Name fehlt.';return;}
 const r=await post('/api/source_add',{
   name:$('s-name').value.trim(), kind:srcKind.value,
   source_type:$('s-type').value, endpoint:srcEp.value.trim()||null});
 if(r.ok){m.style.color='var(--ruhig)';m.textContent='Quelle hinzugefügt.';
   ev.target.reset(); toggleEp(); laden();}
 else{m.style.color='var(--feuert)';m.textContent='Fehler: '+(r.error||'unbekannt');}
};

laden();setInterval(laden,5000);   // Status ist serverseitig zwischengespeichert
</script></body></html>"""

# ==================================================================
#  SERVER
# ==================================================================
# ==================================================================
#  STATUS FUER DIE OBERFLAECHE  (zwischengespeichert!)
# ==================================================================
# Die Oberflaeche pollt alle 3s. Der Aufbau kostet 6 Abfragen auf zehntausenden
# Dokumenten und dauert unter Last mehrere Sekunden. Ohne Zwischenspeicher
# stapeln sich die Anfragen (der Server ist mehrfaedig und unbegrenzt) — jeder
# Thread baut sein eigenes grosses Objekt, bis der Speicher weg ist. Genau das
# war die MemoryError-Ursache. Jetzt: hoechstens EIN Aufbau, alle anderen
# bekommen die letzte Fassung.
STATE_CACHE = {"data": None, "at": 0}
STATE_LOCK = threading.Lock()

def _state_build():
    docs = q("SELECT id,source_id,source_type,title,url,relevance,trust,"
             "published_at,ingested_at FROM documents "
             "ORDER BY published_at DESC, id DESC LIMIT 150")   # ohne Volltext!
    per = defaultdict(lambda: {"names": [], "ids": []})
    if docs:
        ids = [d["id"] for d in docs]
        marks = ",".join("?" * len(ids))
        for r in q("SELECT dt.doc_id, th.id AS tid, th.name FROM doc_themes dt "
                   "JOIN themes th ON th.id=dt.theme_id "
                   f"WHERE dt.doc_id IN ({marks})", tuple(ids)):
            per[r["doc_id"]]["names"].append(r["name"])
            per[r["doc_id"]]["ids"].append(r["tid"])
    for d in docs:
        d["themes"] = per[d["id"]]["names"]
        d["theme_ids"] = per[d["id"]]["ids"]
    n_docs = q("SELECT COUNT(*) c FROM documents")[0]["c"]
    return {
        "sources": q("SELECT s.*, p.n_geprueft, p.n_trivial, p.n_einzelfall, "
                     "p.n_signal FROM sources s "
                     "LEFT JOIN quellen_profil p ON p.source_id=s.id ORDER BY s.id"),
        "model": expectation_model(),
        "docs": docs,
        "log": q("SELECT * FROM log ORDER BY id DESC LIMIT 40"),
        "n_docs": n_docs,
        "n_traces": q("SELECT COUNT(*) c FROM discarded")[0]["c"],
        "collector": {"running": COLLECTOR["running"],
                      "tick_seconds": COLLECTOR["tick_seconds"],
                      "last": COLLECTOR["last"],
                      "discovery_ratio": DISCOVERY_RATIO},
        "status": STATUS,
        "reeval": REEVAL,
        "facts": {k: v for k, v in FACTS.items() if k != "thread"},
        "dedup": {k: v for k, v in DEDUP.items() if k != "thread"},
        "n_dups": q("SELECT COUNT(*) c FROM documents WHERE dup_of IS NOT NULL")[0]["c"],
        "watchdog": {"restarts": WATCHDOG["restarts"],
                     "disk_free_mb": WATCHDOG["disk_free_mb"],
                     "note": WATCHDOG["note"]},
        "ollama": {"ok": OLLAMA["ok"], "note": OLLAMA["note"],
                   "lage": OLLAMA["lage"], "restarts": OLLAMA["restarts"]},
        "gpu": {"enabled": GPU["enabled"], "temp": GPU["temp"],
                "max_temp": GPU["max_temp"], "paused": GPU["paused"]},
        "facts_total": n_docs,
        "facts_erledigt": q("SELECT COUNT(*) c FROM facts_done")[0]["c"],
        "model_alarm": MODEL_ALARM["missing"],
        "facts_model": cfg("facts_model") or LOCAL["model"],
        "n_facts": q("SELECT COUNT(*) c FROM facts")[0]["c"],
        "fact_samples": q("SELECT f.*, d.title dtitle FROM facts f "
                          "LEFT JOIN documents d ON d.id=f.doc_id "
                          "ORDER BY f.id DESC LIMIT 40"),
        "llm": llm_available(),
        "key_source": KEY_SOURCE["where"],
        "local": {"available": local_available()[0],
                  "model": LOCAL["model"], "url": LOCAL["url"]},
        "routing": ROUTING,
        "frontier_allowed": FRONTIER["allowed"],
        "regulator": {"enabled": REGULATOR["enabled"], "state": REGULATOR["state"],
                      "paper_factor": REGULATOR["paper_factor"],
                      "news_factor": REGULATOR["news_factor"],
                      "incr": REGULATOR["incr_ema"],
                      "news_share": REGULATOR["news_share"]},
        "cache_alter": 0}

def _state_light():
    """LEICHTER Status (Jens 08.08.: der Sammler-Tab/die Prozess-Ansicht laedt zu lang). NUR billige Zaehler +
    die In-Memory-Prozess-Flags — KEIN `_state_build` (das liest 72k Docs + Themen-Join + Fakt-Samples und
    ist die Ursache der langen Ladezeit). Fuer Live-Polling (Ampeln, Fortschritt, Ollama-Status) gedacht; die
    reichen Listen (Dokumente/Log/Quellen) holt die inhaltliche Ansicht separat. COUNT(*) ist auf den
    indizierten Tabellen guenstig; keine Joins."""
    def _c(sql):
        try:
            return q(sql)[0]["c"]
        except Exception:                                        # noqa: BLE001 — fail-safe Zaehler
            return None
    return {
        "collector": {"running": COLLECTOR["running"], "tick_seconds": COLLECTOR["tick_seconds"],
                      "last": COLLECTOR["last"], "discovery_ratio": DISCOVERY_RATIO},
        "facts": {k: v for k, v in FACTS.items() if k != "thread"},
        "dedup": {k: v for k, v in DEDUP.items() if k != "thread"},
        "ollama": {"ok": OLLAMA["ok"], "note": OLLAMA["note"], "lage": OLLAMA["lage"],
                   "restarts": OLLAMA["restarts"]},
        "gpu": {"enabled": GPU["enabled"], "temp": GPU["temp"], "paused": GPU["paused"]},
        "status": STATUS,
        "routing": ROUTING, "facts_model": cfg("facts_model") or LOCAL["model"],
        "llm": llm_available(), "frontier_allowed": FRONTIER["allowed"],
        "n_docs": _c("SELECT COUNT(*) c FROM documents"),
        "n_facts": _c("SELECT COUNT(*) c FROM facts"),
        "facts_erledigt": _c("SELECT COUNT(*) c FROM facts_done"),
        "n_dups": _c("SELECT COUNT(*) c FROM documents WHERE dup_of IS NOT NULL"),
        "log": (q("SELECT at, stage, message FROM log ORDER BY id DESC LIMIT 20") or []),  # kleiner Tail (indexiert, billig)
        "leicht": True}

def _state_cached():
    """Baut den Status hoechstens alle state_cache_seconds neu. Alle anderen
    Anfragen bekommen die letzte Fassung — so koennen sich die Anfragen nicht
    mehr stapeln und den Speicher sprengen."""
    max_alter = cfg_int("state_cache_seconds", 5)
    now = time.time()
    if STATE_CACHE["data"] is not None and now - STATE_CACHE["at"] < max_alter:
        d = dict(STATE_CACHE["data"])
        d["cache_alter"] = round(now - STATE_CACHE["at"], 1)
        return d
    # Nur EIN Thread baut; die anderen warten kurz und nehmen dann die Fassung.
    if not STATE_LOCK.acquire(timeout=20):
        if STATE_CACHE["data"] is not None:
            return STATE_CACHE["data"]
        return {"error": "Status wird gerade gebaut"}
    try:
        now = time.time()
        if STATE_CACHE["data"] is not None and now - STATE_CACHE["at"] < max_alter:
            return STATE_CACHE["data"]      # ein anderer war schneller
        data = _state_build()
        STATE_CACHE.update(data=data, at=time.time())
        return data
    except Exception as e:
        log("state", f"Statusaufbau fehlgeschlagen: {str(e)[:120]}")
        if STATE_CACHE["data"] is not None:
            return STATE_CACHE["data"]
        raise
    finally:
        STATE_LOCK.release()

class Handler(BaseHTTPRequestHandler):
    def _write(self, b):
        """Schreibt und schluckt harmlose Client-Abbrueche (Browser hat die
        Verbindung waehrend des Pollings geschlossen) — kein echter Fehler."""
        try:
            self.wfile.write(b)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _json(self, obj, code=200):
        try:
            b = json.dumps(obj, ensure_ascii=False).encode()
        except MemoryError:
            # Unter systemweitem Speicherdruck (Ollama laedt qwen3:30b teils in den
            # Host-RAM + winzige Auslagerungsdatei -> Commit-Limit erschoepft) kann
            # schon das Serialisieren des GUI-Zustands scheitern. Das darf den
            # Prozess NICHT reissen: Speicher freigeben, sauber 503 zurueck, weiter
            # leben. (Der eigentliche Fix ist operativ: Auslagerungsdatei groesser
            # bzw. Displays auf die iGPU -> Modell passt in den VRAM.)
            import gc
            gc.collect()
            b = (b'{"error":"speicher","hinweis":"Zustand konnte nicht serialisiert '
                 b'werden (Speicher erschoepft) - Auslagerungsdatei vergroessern oder '
                 b'Displays auf iGPU, damit qwen3:30b in den VRAM passt."}')
            code = 503
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        self._write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try: return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError: return {}

    def log_message(self, *a): pass

    def handle_one_request(self):
        """Wie das Original, aber Client-Abbrueche werden still verworfen."""
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            b = UI.encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return
            self._write(b)
        elif u.path == "/api/state":
            self._json(_state_cached())
        elif u.path == "/api/state_light":
            self._json(_state_light())              # leichter Live-Status (kein _state_build) — Jens 08.08.
        elif u.path == "/api/broad_anker":
            self._json({"terms": broad_anker()})          # breite Such-Anker lesen (GUI/DB, Jens 29.07.)
        else:
            self._json({"error": "nicht gefunden"}, 404)

    _ERLAUBTE_HOSTS = {"127.0.0.1:8000", "localhost:8000"}
    _ERLAUBTE_ORIGINS = {"http://127.0.0.1:8000", "http://localhost:8000",
                         "http://127.0.0.1:8770", "http://localhost:8770"}

    def do_POST(self):
        # Fable-M4: die POST-Routen loesen Seiteneffekte aus (Sammler-Start, ollama_stop=taskkill, …). Ein
        # boeser Tab im selben Browser koennte per fetch() einen "simple request" an 127.0.0.1:8000 schicken
        # (Drive-by-CSRF). Darum Host pruefen + Origin (falls gesetzt) auf die eigene :8000 / das Cockpit :8770
        # beschraenken. Das eingebettete Cockpit-iframe laedt VON :8000 -> dessen Fetches sind same-origin.
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        if host not in self._ERLAUBTE_HOSTS or (origin is not None and origin.lower() not in self._ERLAUBTE_ORIGINS):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            try:
                self._write(b"403 - fremder Host/Origin (CSRF-Schutz)")
            except Exception:                       # noqa: BLE001
                pass
            return
        u = urlparse(self.path); d = self._body()
        try:
            if u.path == "/api/tick":
                self._json(collect_tick())
            elif u.path == "/api/collector_start":
                start_collector(); self._json({"running": True})
            elif u.path == "/api/collector_stop":
                stop_collector(); self._json({"running": False})
            elif u.path == "/api/collector_speed":
                COLLECTOR["tick_seconds"] = max(5, int(d.get("tick_seconds", 30)))
                self._json({"tick_seconds": COLLECTOR["tick_seconds"]})
            elif u.path == "/api/ollama_start":                  # Jens 08.08.: Start-Knopf
                ok, msg = _ollama_manuell_start()
                self._json({"ok": ok, "meldung": msg})
            elif u.path == "/api/ollama_stop":                   # Jens 08.08.: Stop-Knopf
                ok, msg = _ollama_manuell_stop()
                self._json({"ok": ok, "meldung": msg})
            elif u.path == "/api/ollama_status":                 # Jens 08.08.: Status (billig, kein Gen-Ping)
                self._json(_ollama_status_kurz())
            elif u.path == "/api/broad_anker":
                # Breite Such-Anker GUI-editierbar (Jens 29.07.): {"terms": [...]} setzen (leer = Reset auf Seed),
                # GET liest sie. Symmetrisch zu den gnews_topic-Begriffen (sources.endpoint).
                self._json({"terms": set_broad_anker(d.get("terms"))})
            elif u.path == "/api/reeval_start":
                ok = start_reeval()
                self._json({"started": ok})
            elif u.path == "/api/facts_test":
                self._json({"started": start_extraction(test_n=int(d.get("n", 20)))})
            elif u.path == "/api/facts_all":
                self._json({"started": start_extraction(test_n=None)})
            elif u.path == "/api/facts_stop":
                FACTS["running"] = False
                self._json({"stopped": True})
            elif u.path == "/api/dedup_start":
                # Einmaliger Bestands-Dedup (nicht-destruktiv, resumierbar).
                n = d.get("n")
                self._json({"started": start_dedup(int(n) if n else None),
                            "offen": _offene_embeddings()})
            elif u.path == "/api/dedup_stop":
                DEDUP["running"] = False
                self._json({"stopped": True})
            elif u.path == "/api/reeval_stop":
                REEVAL["running"] = False
                self._json({"stopped": True})
            elif u.path == "/api/source_rate":
                q("UPDATE sources SET rate_per_hour=? WHERE id=?",
                  (max(1, int(d["rate_per_hour"])), int(d["id"])), fetch=False)
                self._json({"ok": True})
            elif u.path == "/api/local_config":
                if d.get("url"): LOCAL["url"] = d["url"].rstrip("/")
                if d.get("model"): LOCAL["model"] = d["model"]
                local_available(force=True)
                self._json({"ok": True, "model": LOCAL["model"], "url": LOCAL["url"]})
            elif u.path == "/api/local_test":
                if d.get("url"): LOCAL["url"] = d["url"].rstrip("/")
                if d.get("model"): LOCAL["model"] = d["model"]
                local_available(force=True)
                self._json(local_test())
            elif u.path == "/api/frontier":
                FRONTIER["allowed"] = bool(d.get("allowed"))
                log("weiche", "Frontier-LLM " + ("FREIGEGEBEN (kostet Geld)"
                    if FRONTIER["allowed"] else "gesperrt (nur lokal/gratis)"))
                self._json({"allowed": FRONTIER["allowed"]})
            elif u.path == "/api/routing":
                task = d.get("task"); target = d.get("target")
                if task in ("relevance", "ontology") and target in ("local", "frontier"):
                    ROUTING[task] = target
                self._json({"ok": True, "routing": ROUTING})
            elif u.path == "/api/source":
                if "enabled" in d:
                    q("UPDATE sources SET enabled=? WHERE id=?",
                      (int(d["enabled"]), int(d["id"])), fetch=False)
                if "endpoint" in d:
                    q("UPDATE sources SET endpoint=? WHERE id=?",
                      (d["endpoint"], int(d["id"])), fetch=False)
                self._json({"ok": True})
            elif u.path == "/api/source_test":
                src = q("SELECT * FROM sources WHERE id=?", (int(d["id"]),))[0]
                fn = HARVESTERS.get(src["kind"])
                try:
                    cur = json.loads(src["cursor"]) if src["cursor"] else None
                    batch, _ = fn(dict(src), cur)
                    # Erfolgreicher Test -> Pause/Fehlerzaehler zuruecksetzen
                    q("UPDATE sources SET fail_count=0, paused_until=NULL, "
                      "last_error=NULL WHERE id=?", (int(d["id"]),), fetch=False)
                    self._json({"ok": True, "found": len(batch),
                                "sample": batch[0]["title"] if batch else None})
                except Exception as e:
                    self._json({"ok": False, "error": _friendly_err(e)})
            elif u.path == "/api/source_probe":
                # Testet eine NOCH NICHT gespeicherte Quelle.
                kind = d.get("kind", "rss")
                fn = HARVESTERS.get(kind)
                if not fn:
                    self._json({"ok": False, "error": f"Unbekannte Art: {kind}"}); return
                if kind == "rss" and not (d.get("endpoint") or "").strip():
                    self._json({"ok": False, "error": "RSS braucht eine Endpoint-URL."}); return
                fake = {"endpoint": d.get("endpoint"),
                        "source_type": d.get("source_type", "news")}
                try:
                    batch, _ = fn(fake, None)
                    self._json({"ok": True, "found": len(batch),
                                "sample": batch[0]["title"] if batch else None})
                except Exception as e:
                    self._json({"ok": False, "error": _friendly_err(e)})
            elif u.path == "/api/source_add":
                name = (d.get("name") or "").strip()
                kind = d.get("kind", "rss")
                if not name:
                    self._json({"ok": False, "error": "Name fehlt."}); return
                if kind not in HARVESTERS:
                    self._json({"ok": False, "error": f"Unbekannte Art: {kind}"}); return
                if kind == "rss" and not (d.get("endpoint") or "").strip():
                    self._json({"ok": False, "error": "RSS braucht eine Endpoint-URL."}); return
                try:
                    q("INSERT INTO sources(name,kind,source_type,endpoint,note) "
                      "VALUES(?,?,?,?,?)",
                      (name, kind, d.get("source_type", "news"),
                       d.get("endpoint"), "vom Nutzer hinzugefügt"), fetch=False)
                    log("kuratiert", f"Neue Quelle: {name} ({kind})")
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"ok": False,
                                "error": "Name schon vergeben?" if "UNIQUE" in str(e)
                                         else str(e)[:200]})
                q("DELETE FROM doc_themes WHERE doc_id IN "
                  "(SELECT id FROM documents WHERE source_id=?)",
                  (int(d["id"]),), fetch=False)
                q("DELETE FROM documents WHERE source_id=?",
                  (int(d["id"]),), fetch=False)
                log("kuratiert", f"Dokumente der Quelle #{d['id']} geloescht.")
                self._json({"ok": True})
            elif u.path == "/api/source_remove":
                q("DELETE FROM doc_themes WHERE doc_id IN "
                  "(SELECT id FROM documents WHERE source_id=?)",
                  (int(d["id"]),), fetch=False)
                q("DELETE FROM documents WHERE source_id=?", (int(d["id"]),), fetch=False)
                q("DELETE FROM sources WHERE id=?", (int(d["id"]),), fetch=False)
                log("kuratiert", f"Quelle #{d['id']} samt Dokumenten entfernt.")
                self._json({"ok": True})
            elif u.path == "/api/theme":
                q("UPDATE themes SET excluded=? WHERE id=?",
                  (int(d.get("excluded", 1)), int(d["id"])), fetch=False)
                self._json({"ok": True})
            elif u.path == "/api/theme_delete":
                q("DELETE FROM doc_themes WHERE theme_id=?", (int(d["id"]),), fetch=False)
                q("DELETE FROM themes WHERE id=?", (int(d["id"]),), fetch=False)
                self._json({"ok": True})
            elif u.path == "/api/doc_relevance":
                did = int(d["id"])
                alt, neu = lerne_relevanz(did, d["relevance"])   # setzt + protokolliert Label
                log("kuratiert", f"Relevanz Dokument #{did}: {alt} -> {neu} (gelernt)")
                self._json({"ok": True})
            elif u.path == "/api/doc_delete":
                q("DELETE FROM doc_themes WHERE doc_id=?", (int(d["id"]),), fetch=False)
                q("DELETE FROM documents WHERE id=?", (int(d["id"]),), fetch=False)
                self._json({"ok": True})
            else:
                self._json({"error": "nicht gefunden"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def _ensure_column(table, col, decl):
    """Idempotentes ADD COLUMN (SQLite kennt kein IF NOT EXISTS dafür). True = ergänzt."""
    cols = [r["name"] for r in q(f"PRAGMA table_info({table})")]
    if col not in cols:
        q(f"ALTER TABLE {table} ADD COLUMN {col} {decl}", fetch=False)
        return True
    return False

def _migrate_schema():
    """Nicht-destruktive Schema-Nachrüstungen für Bestands-DBs (idempotent, safe
    auf der 70k-Doc-Produktions-DB). Neue Tabellen kommen über CREATE IF NOT
    EXISTS im SCHEMA; hier nur die ALTER-Fälle (neue Spalten)."""
    if _ensure_column("documents", "dup_of", "INTEGER"):
        print("Migration: Spalte documents.dup_of ergänzt (Semantik-Dedup, nicht-destruktiv).")

def _prevent_sleep():
    """Windows: haelt den PC wach, SOLANGE der Sammler laeuft — ohne die Energie-
    Einstellungen dauerhaft zu aendern und ohne Admin-Rechte. Anlass: der Prozess
    stand am 19.07. tot, weil der Rechner schlief/herunterfuhr (kein Code-Defekt,
    aber vermeidbar). SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    haelt das SYSTEM wach (der Bildschirm darf abschalten); beim Prozess-Ende
    faellt der Flag automatisch zurueck. No-op ausserhalb Windows, failt nie den
    Start. Deckt NUR Schlaf ab — gegen Absturz/Neustart hilft der Auto-Neustart-
    Starter (System/Scraper_Start.bat) + optionaler Autostart."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        print("Schlaf-Sperre aktiv (Windows) — der PC bleibt wach, solange der "
              "Sammler laeuft (Bildschirm darf abschalten).")
    except Exception as e:                              # nie den Start blockieren
        print(f"Schlaf-Sperre nicht gesetzt ({str(e)[:60]}) — bitte den "
              f"Energiesparmodus in den Windows-Einstellungen manuell abschalten.")


def main():
    global DB
    # PORT-GUARD (Fable-B1, Jens 07.08.): laeuft die Steuerung schon auf :PORT (Doppelklick / die Watchdog-
    # Wache hat sie nach Login schon wiederbelebt), dann NICHT ein zweites Mal starten — sonst liefe die
    # KOMPLETTE Startsequenz inkl. Schema-Migrationen SCHREIBEND auf die live benutzte scraper.db, nur um am
    # Bind zu sterben; Scraper_Start.bat wuerde ewig neu starten (Migrations-Writes + Tracebacks im 10s-Takt).
    # Der Check kommt VOR jeder DB-Beruehrung. Exit-Code 3 = "laeuft schon" (die .bat bricht darauf ab).
    import socket as _socket
    try:
        with _socket.create_connection(("127.0.0.1", PORT), timeout=0.6):
            print(f"Steuerung laeuft bereits auf http://localhost:{PORT} — kein zweiter Start "
                  f"(im Cockpit den Tab 'Sammler' oeffnen).", file=sys.stderr)
            return 3
    except OSError:
        pass                              # Port frei -> normal starten
    _prevent_sleep()                      # Windows: PC nicht schlafen legen, waehrend 1c laeuft
    # EINE Key-Quelle: die in config.txt hinterlegten Keys (inline oder Dateipfad)
    # als Umgebungsvariablen bereitstellen, damit das In-Process-Ensemble
    # (Groq/Gemini/… via MTF_LLM=router), EODHD, HuggingFace, Drive dieselben
    # Keys sehen wie der Scraper. Setzt nur, was noch nicht in der OS-Umgebung
    # steht (die schlaegt die Datei). Fail-soft.
    if _schluessel is not None:
        geladen = _schluessel.lade_ins_environ(cfg)
        if geladen:
            print(f"API-Keys aus config.txt geladen ({len(geladen)}): "
                  + ", ".join(sorted(geladen)))
    if "--reset" in sys.argv and os.path.exists(DB_PATH):
        os.remove(DB_PATH); print("scraper.db geloescht.")
    DB = connect()
    with LOCK:
        DB.executescript(SCHEMA); DB.commit()
    _migrate_schema()                     # Bestands-DB auf das neue Schema heben
    ensure_seed()
    # Einmalige Bereinigung: dauerhaft kaputte/simulierte Quellen entfernen
    for dead in ("gdelt", "demo"):
        rows = q("SELECT id FROM sources WHERE kind=?", (dead,))
        for r in rows:
            q("DELETE FROM doc_themes WHERE doc_id IN "
              "(SELECT id FROM documents WHERE source_id=?)", (r["id"],), fetch=False)
            q("DELETE FROM documents WHERE source_id=?", (r["id"],), fetch=False)
            q("DELETE FROM sources WHERE id=?", (r["id"],), fetch=False)
        if rows:
            print(f"Quelle '{dead}' entfernt (dauerhaft kaputt/simuliert).")
    # Migration: echten Ad-hoc-Feed ergaenzen, falls noch nicht vorhanden.
    adhoc_url = "https://www.ad-hoc-news.de/rss/adhocnews.xml"
    if not q("SELECT id FROM sources WHERE endpoint=?", (adhoc_url,)):
        q("INSERT INTO sources(name,kind,source_type,endpoint,rate_per_hour,note) "
          "VALUES(?,?,?,?,?,?)",
          ("Ad-hoc-Mitteilungen (Pflichtmeldungen)", "rss", "news", adhoc_url,
           RATE_DEFAULTS.get("rss", 6),
           "echte Ad-hoc-Publizitaet — Ereignis-Signal, hohe Erwartungswirkung"),
          fetch=False)
        print("Quelle 'Ad-hoc-Mitteilungen (Pflichtmeldungen)' ergaenzt.")
    if not q("SELECT id FROM sources WHERE kind='edgar8k'"):
        q("INSERT INTO sources(name,kind,source_type,endpoint,rate_per_hour,note) "
          "VALUES(?,?,?,?,?,?)",
          ("US-Pflichtmeldungen (EDGAR 8-K)", "edgar8k", "news", None,
           RATE_DEFAULTS.get("edgar8k", 10),
           "US-Aequivalent zur Ad-hoc — wesentliche Unternehmensereignisse"),
          fetch=False)
        print("Quelle 'US-Pflichtmeldungen (EDGAR 8-K)' ergaenzt.")
    # USPTO-Patentquelle war nicht frei zugaenglich -> durch EPO OPS ersetzen.
    for r in q("SELECT id FROM sources WHERE kind='patents'"):
        q("DELETE FROM documents WHERE source_id=?", (r["id"],), fetch=False)
        q("DELETE FROM sources WHERE id=?", (r["id"],), fetch=False)
        print("Alte USPTO-Patentquelle entfernt (nicht frei zugaenglich).")
    if not q("SELECT id FROM sources WHERE kind='epo'"):
        q("INSERT INTO sources(name,kind,source_type,endpoint,rate_per_hour,note) "
          "VALUES(?,?,?,?,?,?)",
          ("Patente (EPO OPS, international)", "epo", "patent", None,
           RATE_DEFAULTS.get("epo", 20),
           "EPO/WIPO/national via INPADOC — Zugangsdaten in config.txt "
           "(developers.epo.org)."),
          fetch=False)
        print("Quelle 'Patente (EPO OPS, international)' ergaenzt.")
    # Zentralbank-Politik + Finanzbranchen-Themenfeeds ergaenzen.
    for nm, kind, ep, note in [
        ("Zentralbank-Reden weltweit (BIS)", "rss",
         "https://www.bis.org/doclist/cbspeeches.rss", "BIS — Reden aller Notenbanken"),
        ("Fed Reden & Testimony", "rss",
         "https://www.federalreserve.gov/feeds/speeches.xml", "Fed-Reden"),
        ("Fed Geldpolitik (Beschluesse)", "rss",
         "https://www.federalreserve.gov/feeds/press_monetary.xml", "FOMC"),
        ("Private Credit / Direct Lending", "gnews_topic",
         "private credit OR direct lending OR \"leveraged loan\"", "Themenfeed"),
        ("M&A / Uebernahmen", "gnews_topic",
         "merger OR acquisition OR takeover bid OR \"M&A deal\"", "Themenfeed"),
        ("Unternehmensfinanzierung", "gnews_topic",
         "corporate financing OR bond issuance OR capital raise OR refinancing", "Themenfeed"),
        ("Firmen-Funding / Venture", "gnews_topic",
         "funding round OR Series A OR Series B OR venture funding", "Themenfeed"),
        ("Zentralbank-Politik (Debatte)", "gnews_topic",
         "central bank policy OR interest rate decision OR monetary policy", "Themenfeed"),
        # Unabhaengige Fachanalyse (Substack & Co.) — oft vor dem Mainstream
        ("Net Interest (Finanzsektor)", "rss",
         "https://www.netinterest.co/feed", "Banken/Kreditmaerkte/Private Credit"),
        ("The Overshoot (Makro/Finanzzyklen)", "rss",
         "https://theovershoot.co/feed", "Makrotrends, Finanzzyklen"),
        ("Doomberg (Energie/Makro-Risiko)", "rss",
         "https://newsletter.doomberg.com/feed", "Energie, Rohstoffe, Geopolitik"),
        ("Chartbook (Adam Tooze)", "rss",
         "https://adamtooze.substack.com/feed", "Oekonomie/Geopolitik"),
        ("Noahpinion (Makro/Industriepolitik)", "rss",
         "https://www.noahpinion.blog/feed", "Makro, Industriepolitik"),
        ("Fabricated Knowledge (Halbleiter)", "rss",
         "https://www.fabricatedknowledge.com/feed", "Halbleiter/KI-Capex"),
        ("Apricitas Economics (Daten)", "rss",
         "https://www.apricitas.io/feed", "datengetriebene Wirtschaftsanalyse"),
        ("Money and Macro (Geldpolitik)", "rss",
         "https://moneyandmacro.substack.com/feed", "Geldpolitik und Maerkte"),
        # Konsens-Messfuehler (Nobelpreistraeger, grosse Namen)
        ("Project Syndicate (Nobelpreistraeger & Co.)", "rss",
         "https://www.project-syndicate.org/rss",
         "Nobelpreistraeger/Ex-Zentralbanker (u.a. El-Erian)"),
        ("Paul Krugman (Nobelpreistraeger)", "rss",
         "https://paulkrugman.substack.com/feed", "Makro; meist Konsens, punktuell kontraer"),
        ("Econbrowser (Chinn/Hamilton)", "rss",
         "https://econbrowser.com/feed", "akademische Makro-Oekonomen"),
        ("Peterson Institute (PIIE)", "rss",
         "https://www.piie.com/rss/update.xml", "Handel/intern. Finanzen"),
    ]:
        if not q("SELECT id FROM sources WHERE endpoint=?", (ep,)):
            q("INSERT INTO sources(name,kind,source_type,endpoint,rate_per_hour,note) "
              "VALUES(?,?,?,?,?,?)",
              (nm, kind, "news", ep, RATE_DEFAULTS.get(kind, 6), note), fetch=False)
            print(f"Quelle ergaenzt: {nm}")
    # Fruehe Forschung (source_type=paper, damit der Regler sie der
    # Forschungsseite zurechnet — nicht den News).
    _cepr = "https://cepr.org/rss/discussion-paper"
    if not q("SELECT id FROM sources WHERE endpoint=?", (_cepr,)):
        q("INSERT INTO sources(name,kind,source_type,endpoint,rate_per_hour,note) "
          "VALUES(?,?,?,?,?,?)",
          ("CEPR Discussion Papers", "rss", "paper", _cepr, 6,
           "europaeische Wirtschaftsforschung VOR der Publikation — Fruehsignal"),
          fetch=False)
        print("Quelle ergaenzt: CEPR Discussion Papers")
    # Falsch etikettierte alte Quelle ehrlich umbenennen (zeigte auf Tagesschau).
    q("UPDATE sources SET name='Wirtschaftsnews (Tagesschau)' "
      "WHERE name='Ad-hoc-Mitteilungen (RSS)' AND endpoint LIKE '%tagesschau%'",
      fetch=False)
    # Regler uebernimmt die Balance dynamisch -> feste Drosselung zuruecknehmen.
    r = q("SELECT rate_per_hour FROM sources WHERE kind='semanticscholar'")
    if r and (r[0]["rate_per_hour"] or 0) < 20:
        q("UPDATE sources SET rate_per_hour=30 WHERE kind='semanticscholar'", fetch=False)
        print("Semantic-Scholar-Drosselung zurueckgenommen (Regler balanciert jetzt).")
    # Bereits VERSUCHTE Dokumente in facts_done nachtragen, damit ein laufender
    # 1c-Lauf nach dem Update dort weitermacht statt von vorn zu beginnen.
    # Der alte Lauf ging strikt "neueste zuerst" (ORDER BY id DESC) — also wurde
    # alles ab der kleinsten Doc-ID mit Fakten versucht, auch die ~86%, die 0 Fakten
    # ergaben und bisher NIRGENDS vermerkt sind.
    # Einmalige Nachtragung fuer DBs aus der Zeit VOR facts_done. Der alte Lauf
    # ging strikt "neueste zuerst", deshalb laesst sich aus der kleinsten
    # Fakten-ID auf die versuchte Spanne schliessen.
    # WICHTIG: Das gilt NUR fuer die alte Reihenfolge. Der heutige Lauf arbeitet
    # ereignisorientiert (news/funding/patent zuerst) und springt quer durch den
    # ID-Bereich — dann waere dieser Schluss FALSCH und wuerde zehntausende
    # Dokumente faelschlich als erledigt markieren. Deshalb: nur einmal, und nur
    # wenn facts_done noch komplett leer ist.
    n = q("SELECT COUNT(*) c FROM facts_done")[0]["c"]
    marker = q("SELECT value FROM meta WHERE key='backfill_v2'")
    if n == 0 and not marker and q("SELECT COUNT(*) c FROM facts")[0]["c"] > 0:
        lo = q("SELECT MIN(doc_id) m FROM facts")[0]["m"]
        q("INSERT OR IGNORE INTO facts_done(doc_id,n_facts) "
          "SELECT doc_id, COUNT(*) FROM facts GROUP BY doc_id", fetch=False)
        q("INSERT OR IGNORE INTO facts_done(doc_id,n_facts) "
          "SELECT id, 0 FROM documents WHERE id >= ?", (lo,), fetch=False)
        m = q("SELECT COUNT(*) c FROM facts_done")[0]["c"]
        leer = q("SELECT COUNT(*) c FROM facts_done WHERE n_facts=0")[0]["c"]
        print(f"1c-Fortschritt uebernommen: {m} Dokumente als erledigt vermerkt "
              f"({m-leer} mit Fakten, {leer} ohne). Der Lauf macht dort weiter.")
    q("INSERT OR REPLACE INTO meta(key,value) VALUES('backfill_v2','1')", fetch=False)
    # REPARATUR: Waehrend eines Ollama-Neustarts hat 1c Dokumente im Sekundentakt
    # als 'erledigt, 0 Fakten' markiert, obwohl gar kein Modell antwortete —
    # sie waeren fuer immer verloren. Solche Ausbrueche sind erkennbar: sehr
    # viele Null-Ergebnisse in derselben Minute. Echte Verarbeitung schafft
    # keine hunderte Dokumente pro Minute (ein Modellaufruf dauert Sekunden).
    if not q("SELECT value FROM meta WHERE key='repair_poison_v1'"):
        rows = q("""SELECT substr(at,1,16) min, COUNT(*) c FROM facts_done
                    WHERE n_facts=0 GROUP BY min HAVING c > 200""")
        weg = 0
        for r in rows:
            weg += q("SELECT COUNT(*) c FROM facts_done WHERE n_facts=0 "
                     "AND substr(at,1,16)=?", (r["min"],))[0]["c"]
            q("DELETE FROM facts_done WHERE n_facts=0 AND substr(at,1,16)=?",
              (r["min"],), fetch=False)
        if weg:
            print(f"REPARATUR: {weg} Dokumente waren faelschlich als erledigt "
                  f"markiert (Modell war weg) — werden neu verarbeitet.")
        q("INSERT OR REPLACE INTO meta(key,value) VALUES('repair_poison_v1','1')",
          fetch=False)
    # Neue News-/Ereignisquellen ergaenzen (Balance ueber mehr News-Reichweite).
    for nm, url, note in [
        ("US-Notenbank (Fed, Pressemitteilungen)",
         "https://www.federalreserve.gov/feeds/press_all.xml", "Zentralbank-Ereignisse"),
        ("EZB (Pressemitteilungen)", "https://www.ecb.europa.eu/rss/press.html",
         "EZB — Endpoint ggf. pruefen"),
        ("Wirtschaftsanalyse (Marginal Revolution)",
         "https://marginalrevolution.com/feed", "oekonomische Fachdiskussion"),
        ("Konjunktur (Calculated Risk)",
         "https://www.calculatedriskblog.com/feeds/posts/default?alt=rss", "Makro-Analyse"),
    ]:
        if not q("SELECT id FROM sources WHERE endpoint=?", (url,)):
            q("INSERT INTO sources(name,kind,source_type,endpoint,rate_per_hour,note) "
              "VALUES(?,?,?,?,?,?)", (nm, "rss", "news", url, 8, note), fetch=False)
            print(f"News-Quelle ergaenzt: {nm}")
    # CONTROL-ONLY-Start (Jens 07.08.): mit MTF_COLLECTOR_AUTOSTART=0 kommt die Steuer-Oberflaeche (:8000)
    # hoch, aber Sammler + 1c laufen NICHT von selbst an — sie werden aus der Oberflaeche gestartet
    # („everything else needs starting from there"). ENV NICHT gesetzt / !=0 -> unveraendertes Verhalten
    # (Dauerbetrieb, wie ihn die Watchdog-Autostart-Kette erwartet).
    _auto = os.environ.get("MTF_COLLECTOR_AUTOSTART", "1").strip().lower() not in ("0", "off", "false", "nein", "no")
    if _auto:
        start_collector()             # Dauersammler laeuft sofort, resumt Cursor
        # 1c ebenfalls automatisch starten (Dauerbetrieb): sonst idlet die GPU,
        # waehrend der Sammler Dokumente anhaeuft, die nie Fakten bekommen.
        # Beide teilen sich die GPU ueber LLM_GATE und alternieren.
        if cfg_bool("facts_autostart", True):
            offen = q("SELECT COUNT(*) c FROM documents d LEFT JOIN facts_done fd "
                      "ON fd.doc_id=d.id WHERE fd.doc_id IS NULL AND d.dup_of IS NULL")[0]["c"]
            start_extraction(None)
            print(f"Modul 1c laeuft automatisch mit ({offen} offene Dokumente) — "
                  f"nutzt die GPU-Leerlaufzeit und verarbeitet Neuzugaenge laufend.")
    else:
        print("CONTROL-ONLY: Steuerung auf http://localhost:8000 — Sammler + 1c aus der Oberflaeche starten "
              "(MTF_COLLECTOR_AUTOSTART=0).")
    # Semantik-Dedup: laufender Ingestions-Dedup ist automatisch aktiv (dedup_aktiv).
    # Der einmalige BESTANDS-Dedup (70k-Backfill) ist bewusst OPT-IN — er ist zwar
    # nicht-destruktiv, kostet aber GPU-Zeit und konkurriert mit 1c. Bewusst starten.
    offen_emb = _offene_embeddings()
    if cfg_bool("dedup_autostart", False) and offen_emb:
        start_dedup(None)
        print(f"Semantik-Dedup (Bestand) laeuft mit ({offen_emb} Dokumente ohne Embedding).")
    elif offen_emb:
        print(f"Semantik-Dedup bereit: {offen_emb} Bestandsdokumente ohne Embedding. "
              f"Einmaligen Backfill starten mit  POST /api/dedup_start  (nicht-destruktiv, "
              f"resumierbar) — braucht ein Embed-Modell (config embed_model, Default "
              f"nomic-embed-text: ollama pull nomic-embed-text).")
    print(f"Modul 1 · Dauersammler:  http://localhost:{PORT}")
    print(f"Datenbank: {DB_PATH}")
    if CONFIG.get("_loaded_from"):
        print(f"Konfiguration geladen: {CONFIG['_loaded_from']}")
        print(f"  Kontakt-Mail (EDGAR): {UA_CONTACT}")
        print(f"  Frontier erlaubt: {FRONTIER['allowed']} | "
              f"Ollama-Modell: {LOCAL['model']}")
    else:
        print("Keine Config/config.txt gefunden — eingebaute Defaults aktiv.")
    print("LLM:", ("AKTIV — Key aus: " + (KEY_SOURCE["where"] or "?")) if llm_available()
          else "Mock-Modus — Key als  Anthropic API Key\\Anthropic API Key.txt  ablegen")
    print(f"Dauersammler laeuft (ein Schritt alle {COLLECTOR['tick_seconds']}s), "
          "rate-begrenzt je Quelle. Stopp-/Start-Schalter in der Oberflaeche.")
    print("Cursor + Rate-Budget werden in scraper.db gespeichert — nach Neustart "
          "macht der Sammler dort weiter, wo er war.")
    print("Beenden mit Strg+C.")
    # Watchdog: haelt den Betrieb ueber Wochen am Leben (tote Threads neu starten,
    # DB aufraeumen, Plattenplatz ueberwachen).
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    print("Watchdog aktiv — startet tote Threads neu, raeumt die DB auf, "
          "ueberwacht den Plattenplatz.")
    # Ollama-Absicherung sichtbar machen — ABER (Fable-M1) NICHT im CONTROL-ONLY-Modus: `_ollama_probe()` macht
    # einen echten Generierungs-Ping (bis 90 s Timeout + kalter 30B-Modell-Load) und liefe VOR dem :PORT-Bind
    # -> die Steuerung/der eingebettete Sammler-Tab braucht 30-90 s bis erreichbar ("haengt"). Ohne Autostart
    # extrahiert nichts -> der Ping ist unnoetig; wir binden sofort und pruefen Ollama erst beim UI-Start von 1c.
    if _auto:
        lage, grund = _ollama_probe()
        if lage == "ok":
            print(f"Ollama: antwortet ({cfg('facts_model') or LOCAL['model']}).")
        else:
            print(f"Ollama: NICHT EINSATZBEREIT — {grund}")
            print("  -> Der Sammler pausiert, statt wertlose Mock-Bewertungen zu "
                  "erzeugen. Er laeuft automatisch weiter, sobald Ollama antwortet.")
        # Fable-m5: den TATSAECHLICH genutzten Start-Befehl zeigen (nicht den rohen config-Wert, der ggf.
        # als Altpfad verworfen wird) — sonst suggeriert das Banner einen aktiven Befehl, der nie feuert.
        _ocmd, _osh, _onote = _ollama_start_command()
        _ogz = _ocmd if isinstance(_ocmd, str) else " ".join(_ocmd)
        print(f"  Auto-Neustart: {_ogz[:66]}")
        if _onote:
            print(f"  Hinweis: {_onote}")
        print("  (greift erst nach 2 Fehlversuchen in Folge, also ~2 Minuten)")
    else:
        print("Ollama-Check uebersprungen (CONTROL-ONLY) — beim Start von 1c aus der Oberflaeche geprueft.")
    # EPO-Patente: sofort sichtbar machen, ob die Zugangsdaten greifen —
    # sonst faellt eine ganze Stufe der Reifegrad-Leiter still aus.
    _ek, _es, _eg = _epo_creds()
    if _ek and _es:
        print(f"EPO-Patente: Zugangsdaten gelesen (Key ...{_ek[-4:]}). "
              f"Mit 'Testen' pruefen, ob EPO sie akzeptiert.")
    else:
        print(f"EPO-Patente: INAKTIV — {_eg}")
        print("  -> Die Stufe 'Patent' der Reifegrad-Leiter liefert nichts.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    while True:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nBeendet.")
            break
        except Exception as e:
            # Der Webserver darf den Dauerbetrieb nicht mitreissen.
            try: log("server", f"Fehler: {str(e)[:150]} — mache weiter.")
            except Exception: pass
            time.sleep(2)


def connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


if __name__ == "__main__":
    raise SystemExit(main() or 0)      # Exit-Code aus main() (3 = "laeuft schon", Fable-B1) an die .bat geben
