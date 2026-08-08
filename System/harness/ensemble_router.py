"""
ensemble_router.py — quota-/health-bewusster Failover-Router über die Free-Tier-Anbieter.

Realisiert die mit Jens entschiedene Strategie (Design-Doc Semantik-Kanal-2):
  - **Initialbewertung:** je heterogene Familie eine Stimme (`stimmen()` -> Liste für Modul 2).
  - **Failover:** jede Stimme läuft ihre Fähigkeits-Leiter ab (anbieter_registry.leiter) — erst anderes
    Modell beim selben/anderen Anbieter, Familien-Wechsel zuletzt (Jens' Regel). Cross-Familie ist
    erlaubt, wird aber PROTOKOLLIERT (Provenienz), damit die Kalibrierung den familienfremden Ersatz sieht.
  - **Schiedsrichter:** bei Uneinigkeit ruft Modul 2 `schlichte(text, kat)` — ein starkes, an der
    Abstimmung UNBETEILIGTES Modell (Design: Nemotron 550B; seltene Aufrufe passen zum engen Limit).
  - **Quota-Handling:** Quota (429) -> Anbieter für Rest des Laufs UND tagespersistent erschöpft
    (Reset ~täglich), auf die nächste Leiter-Sprosse. Hart (401/402/403/400) -> Anbieter deaktiviert.
    Transient (Timeout/5xx) -> Backoff-Retry beim selben Anbieter. (Fehlerklassen aus openai_compat_llm.)
  - **Vorwärts/Retro-Riegel:** im Retro-Modus sind ALLE Frontier-Stimmen GESPERRT (fail-closed) — ein
    heutiges Modell auf altem Doku = Vintage-Leck (der Grund für den deterministischen Anker-Umweg).

Konfidenz-Aggregation (MIN) + Tiebreak-Auslösung leben in Modul 2 (`ensemble_extrakt`, keine Insel);
der Router liefert die Stimmen + den Schiedsrichter-Callback + die Provenienz.

Nur Standardbibliothek.
"""
import json
import os
import time
from datetime import datetime, timezone

import anbieter_registry as R
from openai_compat_llm import (OpenAICompatLLM, QuotaFehler, HarterFehler, TransienterFehler,
                               EnthaltungsFehler, env_case_tolerant)

_DEFAULT_GED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quota_gedaechtnis.json")


def _klassifiziere_exception(e):
    """Fremd-Adapter (z. B. GeminiLLM) werfen generische RuntimeError -> in unsere Fehlerklassen
    einordnen. Claude-QS M1: HART VOR Quota prüfen, und Quota an ENGEN Wörtern erkennen
    (`rate limit`, nicht bloß `rate` — sonst matcht `rate` in `generateContent` und JEDER Gemini-
    Fehler würde zu Quota = Gemini tagesweit gesperrt)."""
    if isinstance(e, (QuotaFehler, HarterFehler, TransienterFehler)):
        return e
    s = str(e).lower()
    if any(w in s for w in ("401", "402", "403", "payment required", "payment", "unauthorized",
                            "forbidden", "api key not valid", "invalid api key", "kein ")):
        return HarterFehler(str(e))
    if any(w in s for w in ("quota", "rate limit", "rate_limit", "ratelimit", "429",
                            "resource_exhausted", "too many requests")):
        return QuotaFehler(str(e))
    return TransienterFehler(str(e))


def _ist_tageslimit(msg):
    """Claude-QS M4: nur ein TAGES-Limit (RPD) rechtfertigt die tagespersistente Sperre; ein
    Minuten-Limit (RPM, Reset in ~60s) darf den Anbieter NICHT für den ganzen Tag ausschalten."""
    s = (msg or "").lower()
    return any(w in s for w in ("per day", "/day", "daily", "rpd", "day)", "24 h", "24h", "per-day"))


def _mk(anbieter, model):
    return f"{anbieter}:{model}"


class QuotaGedaechtnis:
    """Tagespersistente Erschöpfungs-Liste auf **Modell-Ebene** (`anbieter:model`): ein Modell, das
    heute sein Limit riss, bleibt bis zum (UTC-)Datumswechsel aus — ANDERE Modelle desselben Anbieters
    bleiben nutzbar (Jens' Regel „erst anderes Modell beim selben Anbieter", Gemini-B3, Grenzkosten 3.13).
    Datei-Format: {"YYYY-MM-DD": ["anbieter:model", …]}. Nur der heutige Eintrag zählt."""

    def __init__(self, pfad=None, heute=None):
        self.pfad = pfad or os.environ.get("MTF_QUOTA_GED", _DEFAULT_GED)
        self.heute = heute or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._erschoepft = set(self._laden())

    def _laden(self):
        try:
            with open(self.pfad, encoding="utf-8") as f:
                return json.load(f).get(self.heute, [])
        except (OSError, json.JSONDecodeError):
            return []

    def ist_erschoepft(self, anbieter, model):
        return _mk(anbieter, model) in self._erschoepft

    def markiere(self, anbieter, model):
        schluessel = _mk(anbieter, model)
        if schluessel in self._erschoepft:
            return
        self._erschoepft.add(schluessel)
        try:
            # Claude-QS M10: mit der aktuellen Datei-Heute-Liste MERGEN (nicht last-writer-wins) —
            # ein Parallel-Prozess darf keine Einträge verlieren. Claude-QS M9: fail-safe gegen
            # kaputtes JSON (JSONDecodeError=ValueError, KEIN OSError) — sonst crasht der Lauf genau
            # im Quota-Fall. Nur der heutige Eintrag bleibt (Alt-Tage verfallen).
            bestand = set()
            try:
                with open(self.pfad, encoding="utf-8") as f:
                    bestand = set(json.load(f).get(self.heute, []))
            except (OSError, ValueError):
                bestand = set()
            vereint = self._erschoepft | bestand
            self._erschoepft = vereint
            tmp = self.pfad + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({self.heute: sorted(vereint)}, f)
            os.replace(tmp, self.pfad)
        except (OSError, ValueError):
            pass                                              # Persistenz ist best-effort; Lauf-Zustand trägt weiter


def _baue_adapter(anbieter, model, vokabular, voice=None):
    """Ein Modell -> Adapter (dispatch auf Protokoll). openai_compat für Groq/SambaNova/Mistral/
    OpenRouter/Ollama; Gemini über seinen eigenen Adapter (wählt sein Modell selbst)."""
    cfg = R.ANBIETER[anbieter]
    if cfg["protokoll"] == "gemini":
        from gemini_llm import GeminiLLM
        return GeminiLLM(vokabular=vokabular, voice=voice or f"gemini:{model}", max_retries=1)
    return OpenAICompatLLM(
        anbieter=anbieter, model=model, base_url=cfg["base_url"], key_env=cfg["key_env"],
        familie=(R.finde_modell(anbieter, model) or {}).get("familie", anbieter),
        vokabular=vokabular, voice=voice, throttle_s=cfg.get("throttle_s", 1.0),
        max_tokens=2048)


class RouterZustand:
    """Geteilter Zustand über alle Stimmen EINES Laufs: Erschöpfung (Lauf + tagespersistent),
    harte Deaktivierung, Provenienz-Log (append-only), Adapter-Cache."""

    def __init__(self, gedaechtnis=None, adapter_fabrik=None):
        self.ged = gedaechtnis if gedaechtnis is not None else QuotaGedaechtnis()
        self.lauf_erschoepft = set()      # {(anbieter,model)} diesen Lauf quota-erschöpft (Modell-Ebene)
        self.hart_aus = set()             # {(anbieter,model)} dauerhaft (Auth/Payment/Bad-Model) — dieser Lauf
        self.provenienz = []              # [{slot, anbieter, model, familie, ergebnis|fehler, ts?}]
        self._adapter = {}                # (anbieter,model) -> Adapter (wiederverwendet)
        self._fabrik = adapter_fabrik or _baue_adapter   # injizierbar (Tests: netzfreie Fake-Adapter)
        self.genutzt_pro_text = {}        # text -> {(anbieter,model)} (Claude-QS M6: Slot-Kollaps-Dedup)

    def modell_gesperrt(self, anbieter, model):
        """Modell-Ebene (Gemini-B3): nur dieses (anbieter,model) ist gesperrt, nicht der ganze Anbieter
        — so bleibt „erst anderes Modell beim selben Anbieter" (Jens' Failover-Regel) möglich."""
        return ((anbieter, model) in self.lauf_erschoepft or (anbieter, model) in self.hart_aus
                or self.ged.ist_erschoepft(anbieter, model))

    def schon_genutzt(self, text, anbieter, model):
        """Claude-QS M6: hat eine ANDERE Stimme dieses Dokuments bereits genau dieses Modell benutzt?
        Verhindert, dass ein Failover-Kollaps zweier Slots auf DASSELBE Modell als zwei unabhängige
        Stimmen zählt (korrelierte Fehler zurück durch die Hintertür, §4)."""
        return (anbieter, model) in self.genutzt_pro_text.get(text, set())

    def merke_genutzt(self, text, anbieter, model):
        self.genutzt_pro_text.setdefault(text, set()).add((anbieter, model))

    def adapter(self, anbieter, model, vokabular):
        key = (anbieter, model)
        if key not in self._adapter:
            self._adapter[key] = self._fabrik(anbieter, model, vokabular)
        return self._adapter[key]


class RouterStimme:
    """Eine abstimmende Stimme = ein Slot mit Wunsch-(anbieter,model) + Fähigkeits-Leiter.
    `kategorisiere` läuft die Leiter ab, bis eine Sprosse antwortet; Fehler steuern den Wechsel."""

    def __init__(self, slot, zustand, vokabular, rolle="initial", modus="vorwaerts",
                 max_transient=2, slot_name=None, ausgeschlossene_familien=(), dedup=True):
        self.slot = slot                              # (anbieter, model) Wunsch
        self.z = zustand
        self.vokabular = vokabular
        self.rolle = rolle
        self.modus = modus
        self.max_transient = max_transient
        self.slot_name = slot_name or f"{slot[0]}:{slot[1]}"
        self.voice = f"router[{self.slot_name}]"
        self.model = f"router:{slot[0]}:{slot[1]}"    # stabile Slot-Identität (per-Call-Wahrheit im Log)
        self.ausgeschlossene_familien = set(ausgeschlossene_familien)   # M5: Schiedsrichter-Unabhängigkeit
        self.dedup = dedup                            # M6: Slot-Kollaps auf dasselbe Modell vermeiden

    def pin(self):
        """Stabile SLOT-Signatur (Wunsch-anbieter:model) fürs Ensemble-Vintage. ⚠ Nach einem Failover
        kann das tatsächlich genutzte Modell abweichen — die per-Aufruf-WAHRHEIT steht append-only im
        Provenienz-Log (`RouterZustand.provenienz`); deren Zeilen-genaue Persistenz an `fact_kategorie`
        ist die aufzeichnung.db-Naht (home-terminiert, Claude-QS M8). `modell_vintage` ist also die
        stabile ENSEMBLE-Konfiguration, nicht die per-Zeile-Modell-Identität."""
        return self.model

    def kategorisiere(self, text):
        if self.modus != "vorwaerts":
            raise HarterFehler(
                "Vorwärts/Retro-Riegel: Frontier-Stimmen sind im Retro-Modus gesperrt "
                "(Vintage-Leck). Retro nutzt den deterministischen/gepinnten Extraktor.")
        leiter = R.leiter(self.slot, self.rolle)
        letzter = None
        for kand in leiter:
            anb, mod, fam = kand["anbieter"], kand["model"], kand["familie"]
            if fam in self.ausgeschlossene_familien:         # M5: Schiedsrichter ≠ abstimmende Familie
                continue
            if self.z.modell_gesperrt(anb, mod):             # Modell-Ebene (B3): nur dieses Modell, nicht der Anbieter
                continue
            if self.dedup and self.z.schon_genutzt(text, anb, mod):   # M6: Kollaps-Dedup pro Dokument
                continue
            try:
                adapter = self.z.adapter(anb, mod, self.vokabular)    # m3: Baufehler (fehlender Key/Netz) abfangen
            except Exception as e:                            # noqa: BLE001
                letzter = self._verbuche_fehler(anb, mod, fam, e)
                continue
            for versuch in range(self.max_transient + 1):
                try:
                    res = adapter.kategorisiere(text)
                    self.z.merke_genutzt(text, anb, mod)
                    # `text` mitführen: Grundlage der Pseudolabel-Korrekturfall-Liste (aufzeichnung.py) —
                    # welches (höherwertige) Modell hat das lokale bei WELCHEM Dokument korrigiert.
                    self.z.provenienz.append({"slot": self.slot_name, "anbieter": anb, "model": mod,
                                              "familie": fam, "ergebnis": res, "text": text})
                    return res
                except Exception as e:                        # noqa: BLE001 — klassifiziert direkt danach
                    fehler = _klassifiziere_exception(e)
                    letzter = fehler
                    if isinstance(fehler, TransienterFehler) and versuch < self.max_transient:
                        time.sleep(2 ** versuch)              # Backoff, selbes Modell erneut
                        continue
                    self._verbuche_fehler(anb, mod, fam, fehler)
                    break                                     # nächste Sprosse
        # Leiter erschöpft -> ENTHALTUNG (unterscheidbar von „keine Kategorie", Claude-QS M7):
        # ensemble_extrakt zählt diese Stimme NICHT mit, statt sie als korrelierte Null-Stimme zu werten.
        self.z.provenienz.append({"slot": self.slot_name, "enthaltung": True,
                                  "letzter_fehler": type(letzter).__name__ if letzter else None})
        raise EnthaltungsFehler(f"{self.slot_name}: Fähigkeits-Leiter erschöpft "
                                f"(letzter: {type(letzter).__name__ if letzter else 'keiner'})")

    def _verbuche_fehler(self, anb, mod, fam, e):
        """Fehler klassifizieren, Modell-Ebene sperren (Quota tagespersistent NUR bei Tageslimit, M4),
        Provenienz protokollieren. -> die typisierte Fehler-Instanz (für `letzter`)."""
        fehler = _klassifiziere_exception(e)
        if isinstance(fehler, QuotaFehler):
            self.z.lauf_erschoepft.add((anb, mod))           # dieser Lauf: aus
            if _ist_tageslimit(str(fehler)):                 # M4: nur ein RPD-Limit persistiert über den Tag
                self.z.ged.markiere(anb, mod)
        elif isinstance(fehler, HarterFehler):
            self.z.hart_aus.add((anb, mod))
        self.z.provenienz.append({"slot": self.slot_name, "anbieter": anb, "model": mod,
                                  "familie": fam, "fehler": type(fehler).__name__})
        return fehler


class EnsembleRouter:
    """Baut die Initial-Stimmen + den Schiedsrichter-Callback über einen geteilten Zustand."""

    def __init__(self, slots=None, schiedsrichter=None, vokabular=None, modus="vorwaerts",
                 gedaechtnis=None, adapter_fabrik=None):
        self.vokabular = vokabular
        self.modus = modus
        self.slots = slots if slots is not None else R.ensemble_aus_env()
        self.schiedsrichter_slot = schiedsrichter or R.DEFAULT_SCHIEDSRICHTER
        self.z = RouterZustand(gedaechtnis=gedaechtnis, adapter_fabrik=adapter_fabrik)
        self._arbiter_cache = {}          # text -> arbiter-Kategorien (ein Aufruf je Doku, nicht je Kat)

    def _slot_familien(self):
        """Familien der aktiven abstimmenden Slots (für die Schiedsrichter-Unabhängigkeit, M5)."""
        fams = set()
        for a, m in self.slots:
            eintrag = R.finde_modell(a, m)
            if eintrag:
                fams.add(eintrag["familie"])
        return fams

    def stimmen(self):
        """Die abstimmenden Initial-Stimmen (eine je Slot). Für make_klassifikation(llms)."""
        aus = []
        for (a, m) in self.slots:
            eintrag = R.finde_modell(a, m)
            if eintrag and "initial" not in eintrag["rollen"]:      # m4: explizite Wahl nicht initial-fähig
                print(f"  ⚠ Router: Slot {a}:{m} ist nicht initial-fähig -> Fähigkeits-Leiter stuft um.")
            aus.append(RouterStimme((a, m), self.z, self.vokabular, rolle="initial",
                                    modus=self.modus, slot_name=f"{a}:{m}"))
        return aus

    def _arbiter_kategorien(self, text):
        """Schiedsrichter-Kategorien für ein Doku (ein Aufruf, nicht je Kat). NUR ERFOLGE werden
        gecacht (Gemini-B2): ein transient/quota-bedingtes Nicht-Antworten darf nicht dauerhaft alle
        strittigen Kategorien desselben Dokuments verwerfen. HarterFehler (Retro-Riegel/Auth) wird
        LAUT durchgereicht (fail-loud auf Konfigurationsfehler — z. B. Router im Retro-Modus)."""
        if text in self._arbiter_cache:
            return self._arbiter_cache[text]
        # M5: der Schiedsrichter darf KEINE Familie sein, die bereits abgestimmt hat (Unabhängigkeit).
        stimme = RouterStimme(self.schiedsrichter_slot, self.z, self.vokabular,
                              rolle="schiedsrichter", modus=self.modus, slot_name="schiedsrichter",
                              ausgeschlossene_familien=self._slot_familien(), dedup=False)
        try:
            kats = dict(stimme.kategorisiere(text))          # {kat: staerke}
        except HarterFehler:
            raise                                            # Retro-Riegel/Auth: laut, NICHT cachen
        except (QuotaFehler, TransienterFehler, EnthaltungsFehler):
            return None                                      # nicht verfügbar/enthalten -> NICHT cachen (M2)
        self._arbiter_cache[text] = kats                     # nur Erfolge cachen
        return kats

    def schlichte(self, text, kat):
        """Tiebreak-Callback für Modul 2: entscheidet die PRÄSENZ einer strittigen Kategorie.
        -> (praesent: bool, staerke: str|None). Fail-closed: erschöpfter/nicht-verfügbarer Schiedsrichter
        -> (False, None) (im Zweifel KEIN Signal emittieren — Zielfunktion/§4). Im Retro-Modus wirft der
        Riegel über RouterStimme -> HarterFehler propagiert (fail-loud, kein stiller Signalverlust)."""
        kats = self._arbiter_kategorien(text)
        if not kats:
            return (False, None)
        if kat in kats:
            return (True, kats[kat])
        return (False, None)


class RouterLabeler:
    """Adapter: eine Router-Stimme (Free-Tier-Failover-Leiter, Standard = das fähigste Initial-Modell = 70B)
    INS Labeler-Protokoll (haiku_labeler-kompatibel) — damit ist der 70B-Router der VORLABLER im
    --pruef/Vorlabel-Pfad, Haiku (paid/Ventil, Jens 30.07.) raus. Drop-in wie HaikuLabeler:
    `.kandidaten` · `.vorschlag` · `.kategorisiere` · `.klassifiziere_relation`. Nutzt den Router
    (Quota/Health/Failover + Vorwärts/Retro-Riegel) statt eines rohen Einzel-Calls. KEINE INSEL — wickelt
    die vorhandene `RouterStimme`, definiert nichts neu. `zustand` teilbar über mehrere Labeler EINES Laufs
    (gemeinsames Quota-/Health-Gedächtnis; so zählt der Primär- + Zweitstimmen-Verbrauch zusammen)."""

    _RANG = {"stark": 3, "mittel": 2, "schwach": 1, "keine": 0}

    def __init__(self, kandidaten, slot=None, art="kategorie", modus="vorwaerts",
                 zustand=None, gedaechtnis=None, adapter_fabrik=None):
        self.kandidaten = list(kandidaten)
        self.art = art
        self.slot = slot or R.DEFAULT_ENSEMBLE[0]        # Standard-Vorlabler = fähigstes Initial-Modell (70B)
        self.zustand = (zustand if zustand is not None
                        else RouterZustand(gedaechtnis=gedaechtnis, adapter_fabrik=adapter_fabrik))
        self.stimme = RouterStimme(self.slot, self.zustand, self.kandidaten, rolle="initial",
                                   modus=modus, slot_name=f"{self.slot[0]}:{self.slot[1]}")
        self.zuletzt_enthalten = False        # QS-M-1: Enthaltung (Leiter erschöpft) ≠ "keine Kategorie"

    def kategorisiere(self, text):
        # QS-M-1 (fail-open-Blocker): die Enthaltung (Fähigkeits-Leiter leer, z. B. Quota-Schwanz) MUSS
        # unterscheidbar bleiben von einer echten leeren Antwort — sonst liest `pruef_reihenfolge` zwei
        # Enthaltungen als "einig" und drückt genau die unbewertbaren Texte ans Ende (fail-open). Das
        # Flag propagiert die Enthaltung sichtbar; der HARTE Retro-Riegel (HarterFehler) läuft weiter durch.
        self.zuletzt_enthalten = False
        try:
            return self.stimme.kategorisiere(text)
        except EnthaltungsFehler:
            self.zuletzt_enthalten = True
            return []                                    # kein Label — aber `zuletzt_enthalten` markiert WARUM

    def vorschlag(self, text):
        tr = [(k, s) for (k, s) in self.kategorisiere((text or "").strip()) if self._RANG.get(s, 0) > 0]
        if not tr:
            return None                                  # QS-m-1: Rang-0/"keine" defensiv gedroppt (fail-closed)
        return sorted(tr, key=lambda kv: (-self._RANG.get(kv[1], 0), kv[0]))[0][0]   # stärkste, stabil

    def klassifiziere_relation(self, phrase, klassen=None):
        self.zuletzt_enthalten = False               # QS: kein stale Enthaltungs-Flag (falls je als Relations-Stimme)
        # Der Router KATEGORISIERT (geschlossenes Vokabular), extrahiert KEINE Relationen -> fail-closed None
        # (art='relation' braucht eine Relations-Stimme [NLI/Haiku]; lieber kein Label als ein fabriziertes).
        return None
