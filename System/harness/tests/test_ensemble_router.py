"""
test_ensemble_router.py — Offline-Tests des Semantik-Kanal-2-Routers (netz-/modellfrei).

Die Adapter werden über die injizierbare `adapter_fabrik` durch Fakes ersetzt; geprüft werden die
Router-EIGENSCHAFTEN, die die Strategie stringent machen:
  - Fähigkeits-Leiter-Reihenfolge (Registry): gleiches Modell anderer Anbieter, dann Familie, dann Wechsel.
  - Failover: Quota -> nächste Sprosse + Anbieter laufweit gesperrt; Hart -> Anbieter deaktiviert;
    Transient -> Backoff-Retry beim selben Anbieter, dann weiter.
  - Tagespersistentes Quota-Gedächtnis (schreiben + über eine zweite Instanz lesen).
  - Vorwärts/Retro-Riegel (Frontier im Retro fail-closed gesperrt).
  - Schiedsrichter-Callback (Präsenz-Tiebreak, fail-closed ohne Schiedsrichter).
  - Provenienz-Log (welches Modell tatsächlich geantwortet hat — auch der familienfremde Ersatz).

Der Adapter-Parser (`openai_compat_llm`) wird gegen realdaten-nahe Antwort-Strukturen getestet
(OpenAI-Schema, Reasoning-Modell mit content=None, 429/402-Bodies) — die Fehlerklassifikation ist
die Naht, an der der Failover hängt.
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import anbieter_registry as R                                            # noqa: E402
from ensemble_router import (EnsembleRouter, RouterZustand, QuotaGedaechtnis,                # noqa: E402
                             _klassifiziere_exception, _ist_tageslimit)
from openai_compat_llm import (QuotaFehler, HarterFehler, TransienterFehler, EnthaltungsFehler,   # noqa: E402
                               _klassifiziere_fehler, _extrahiere_json_objekt, OpenAICompatLLM)


def _ist_ergebnis(v):
    """Eine Ergebnis-Liste = Liste aus (kat, staerke)-Tupeln (auch leer)."""
    return isinstance(v, list) and all(isinstance(x, tuple) and len(x) == 2 for x in v)


def _normalisiere_plan(v):
    """dict-Wert -> Aktions-Plan (Liste). Eine einzelne Exception oder eine einzelne Ergebnis-Liste
    wird zu einem Ein-Aktions-Plan; eine Liste gemischter Aktionen bleibt, wie sie ist."""
    if isinstance(v, Exception):
        return [v]
    if _ist_ergebnis(v):
        return [v]
    return list(v)                     # bereits eine Aktions-Liste (mix aus Exceptions/Ergebnissen)


class _FakeAdapter:
    """Netzfreier Stand-in für einen Anbieter-Adapter. `plan` = Liste von Aktionen je Aufruf
    (Ergebnis-Liste ODER Exception). Ist der Plan erschöpft, wird die LETZTE Aktion wiederholt."""

    def __init__(self, anbieter, model, plan):
        self.anbieter = anbieter
        self.model = f"{anbieter}:{model}"
        self._plan = list(plan) or [[]]
        self.aufrufe = 0

    def kategorisiere(self, text):
        self.aufrufe += 1
        akt = self._plan.pop(0) if len(self._plan) > 1 else self._plan[0]
        if isinstance(akt, Exception):
            raise akt
        return akt


def _fabrik(plaene):
    """Baut eine adapter_fabrik, die je (anbieter,model) einen _FakeAdapter mit vorgegebenem Plan liefert.
    `plaene`: dict (anbieter,model)->plan ODER anbieter->plan (Modell egal)."""
    cache = {}

    def fab(anbieter, model, vokabular):
        key = (anbieter, model)
        if key in cache:
            return cache[key]
        roh = plaene.get((anbieter, model), plaene.get(anbieter, []))
        cache[key] = _FakeAdapter(anbieter, model, _normalisiere_plan(roh))
        return cache[key]
    return fab


def _leeres_ged():
    return QuotaGedaechtnis(pfad=os.path.join(tempfile.mkdtemp(), "q.json"), heute="2026-07-30")


class TestLeiter(unittest.TestCase):
    def test_faehigkeit_zuerst_dann_gleiches_modell(self):
        leiter = R.leiter(("groq", "llama-3.3-70b-versatile"), "failover")
        eintraege = [(m["anbieter"], m["model"], m["klasse"]) for m in leiter]
        self.assertEqual(eintraege[0], ("groq", "llama-3.3-70b-versatile", "gross"))   # exakt
        self.assertEqual(eintraege[1][0], "sambanova")           # gleichwertig + gleiches Modell zuerst
        # ALLE gleichwertigen (gross) kommen VOR jedem schlechteren (Jens-Refinement)
        raenge = [R.KLASSE_RANG[k] for _, _, k in eintraege]
        p = R.KLASSE_RANG["gross"]
        erster_schlechter = next((i for i, r in enumerate(raenge) if r < p), len(raenge))
        self.assertTrue(all(r >= p for r in raenge[:erster_schlechter]))
        self.assertTrue(all(r < p for r in raenge[erster_schlechter:]))   # danach nur noch schlechtere

    def test_schlechteres_modell_nur_letzter_ausweg(self):
        # ein gleichwertiges Modell einer FREMDEN Familie schlägt ein schlechteres beim SELBEN Anbieter
        namen = [(m["anbieter"], m["model"]) for m in R.leiter(("groq", "llama-3.3-70b-versatile"), "initial")]
        idx_gemini = next(i for i, (a, _) in enumerate(namen) if a == "gemini")     # gross, fremde Familie
        idx_groq8b = next(i for i, (a, m) in enumerate(namen) if m == "llama-3.1-8b-instant")  # klein, selber Anbieter
        self.assertLess(idx_gemini, idx_groq8b)                  # gleichwertig-fremd VOR schlechter-selber-Anbieter

    def test_schiedsrichter_rolle_kein_notnagel(self):
        # Schiedsrichter-Leiter enthält NUR schiedsrichter-fähige Modelle (kein degradiertes Fallback)
        for m in R.leiter(R.DEFAULT_SCHIEDSRICHTER, "schiedsrichter"):
            self.assertIn("schiedsrichter", m["rollen"])

    def test_cerebras_nicht_in_leiter(self):
        # cerebras trägt keine Modelle/Rollen (402) -> nie Kandidat
        for m in R.alle_modelle():
            self.assertNotEqual(m["anbieter"], "cerebras")


class TestFailover(unittest.TestCase):
    def _router(self, plaene, slots):
        return EnsembleRouter(slots=slots, vokabular=None, gedaechtnis=_leeres_ged(),
                              adapter_fabrik=_fabrik(plaene))

    def test_quota_wechselt_und_sperrt_modell(self):
        # groq-70b wirft Quota -> Leiter geht zu sambanova (gleiches Modell); NUR groq-70b gesperrt (B3)
        plaene = {("groq", "llama-3.3-70b-versatile"): QuotaFehler("429"),
                  "sambanova": [("Lithium", "stark")]}
        r = self._router(plaene, [("groq", "llama-3.3-70b-versatile")])
        stimme = r.stimmen()[0]
        self.assertEqual(stimme.kategorisiere("t"), [("Lithium", "stark")])
        self.assertIn(("groq", "llama-3.3-70b-versatile"), r.z.lauf_erschoepft)
        self.assertEqual(stimme.kategorisiere("t2"), [("Lithium", "stark")])

    def test_schlechteres_modell_erst_wenn_nichts_gleichwertiges(self):
        # Alle GLEICHWERTIGEN (gross) + mittleren Modelle tot -> erst dann greift das schlechtere
        # groq-8b (klein) als letzter Ausweg (Jens-Refinement + B3 Modell-Ebene).
        plaene = {("groq", "llama-3.1-8b-instant"): [("Kupfer", "schwach")]}
        for m in R.alle_modelle():
            if m["klasse"] != "klein":                          # alles ausser klein quota-tot
                plaene[(m["anbieter"], m["model"])] = QuotaFehler("429")
        for m in ("Meta-Llama-3.1-8B-Instruct",):               # andere kleine auch tot -> nur groq-8b lebt
            plaene[("sambanova", m)] = QuotaFehler("429")
        plaene[("mistral", "ministral-8b-latest")] = QuotaFehler("429")
        r = self._router(plaene, [("groq", "llama-3.3-70b-versatile")])
        stimme = r.stimmen()[0]
        self.assertEqual(stimme.kategorisiere("t"), [("Kupfer", "schwach")])
        genutzt = [p for p in r.z.provenienz if "ergebnis" in p][-1]
        self.assertEqual((genutzt["anbieter"], genutzt["model"]), ("groq", "llama-3.1-8b-instant"))

    def test_hart_deaktiviert_modell(self):
        plaene = {("groq", "llama-3.3-70b-versatile"): HarterFehler("402"),
                  "sambanova": [("Kupfer", "mittel")]}
        r = self._router(plaene, [("groq", "llama-3.3-70b-versatile")])
        self.assertEqual(r.stimmen()[0].kategorisiere("t"), [("Kupfer", "mittel")])
        self.assertIn(("groq", "llama-3.3-70b-versatile"), r.z.hart_aus)

    def test_transient_retry_dann_weiter(self):
        # groq: 2x transient (=max_transient) -> aufgeben -> sambanova
        plaene = {"groq": [TransienterFehler("blip"), TransienterFehler("blip"),
                           TransienterFehler("blip")],
                  "sambanova": [("Stromnetz", "schwach")]}
        r = self._router(plaene, [("groq", "llama-3.3-70b-versatile")])
        stimme = r.stimmen()[0]
        stimme.max_transient = 2
        self.assertEqual(stimme.kategorisiere("t"), [("Stromnetz", "schwach")])
        self.assertNotIn(("groq", "llama-3.3-70b-versatile"), r.z.lauf_erschoepft)   # transient sperrt NICHT

    def test_enthaltung_wenn_leiter_erschoepft(self):
        # alle werfen Quota -> ENTHALTUNG wirft EnthaltungsFehler (unterscheidbar von leerer Antwort, M7)
        plaene = {a: QuotaFehler("429") for a in ("groq", "sambanova", "gemini", "mistral",
                                                  "openrouter", "ollama")}
        r = self._router(plaene, [("groq", "llama-3.3-70b-versatile")])
        with self.assertRaises(EnthaltungsFehler):
            r.stimmen()[0].kategorisiere("t")
        self.assertTrue(any(p.get("enthaltung") for p in r.z.provenienz))

    def test_provenienz_zeigt_familienfremden_ersatz(self):
        # llama-Slot fällt komplett aus -> Wechsel auf gemini; Provenienz muss den Wechsel zeigen
        plaene = {"groq": QuotaFehler("q"), "sambanova": QuotaFehler("q"),
                  "gemini": [("Lithium", "mittel")]}
        r = self._router(plaene, [("groq", "llama-3.3-70b-versatile")])
        r.stimmen()[0].kategorisiere("t")
        erfolg = [p for p in r.z.provenienz if "ergebnis" in p]
        self.assertEqual(erfolg[-1]["familie"], "gemini")      # familienfremder Ersatz protokolliert


class TestQuotaGedaechtnis(unittest.TestCase):
    def test_persistenz_ueber_instanzen(self):
        pfad = os.path.join(tempfile.mkdtemp(), "q.json")
        g1 = QuotaGedaechtnis(pfad=pfad, heute="2026-07-30")
        g1.markiere("groq", "llama-3.3-70b-versatile")
        g2 = QuotaGedaechtnis(pfad=pfad, heute="2026-07-30")
        self.assertTrue(g2.ist_erschoepft("groq", "llama-3.3-70b-versatile"))
        self.assertFalse(g2.ist_erschoepft("groq", "llama-3.1-8b-instant"))   # anderes Modell frei (B3)
        # anderer Tag -> Eintrag verfällt
        g3 = QuotaGedaechtnis(pfad=pfad, heute="2026-07-31")
        self.assertFalse(g3.ist_erschoepft("groq", "llama-3.3-70b-versatile"))

    def test_kaputtes_gedaechtnis_faellt_safe(self):
        pfad = os.path.join(tempfile.mkdtemp(), "q.json")
        with open(pfad, "w") as f:
            f.write("{kaputt")                                  # unlesbares JSON
        g = QuotaGedaechtnis(pfad=pfad, heute="2026-07-30")     # darf NICHT werfen
        self.assertFalse(g.ist_erschoepft("groq", "m"))

    def test_gesperrtes_modell_wird_uebersprungen(self):
        pfad = os.path.join(tempfile.mkdtemp(), "q.json")
        QuotaGedaechtnis(pfad=pfad, heute="2026-07-30").markiere("groq", "llama-3.3-70b-versatile")
        ged = QuotaGedaechtnis(pfad=pfad, heute="2026-07-30")
        plaene = {"sambanova": [("Lithium", "stark")]}          # groq-70b gar nicht aufgerufen
        r = EnsembleRouter(slots=[("groq", "llama-3.3-70b-versatile")], gedaechtnis=ged,
                           adapter_fabrik=_fabrik(plaene))
        self.assertEqual(r.stimmen()[0].kategorisiere("t"), [("Lithium", "stark")])


class TestRetroRiegel(unittest.TestCase):
    def test_frontier_im_retro_gesperrt(self):
        r = EnsembleRouter(slots=[("groq", "llama-3.3-70b-versatile")], modus="retro",
                           gedaechtnis=_leeres_ged(), adapter_fabrik=_fabrik({"groq": [("x", "stark")]}))
        with self.assertRaises(HarterFehler):
            r.stimmen()[0].kategorisiere("altes doku")

    def test_schiedsrichter_im_retro_wirft_laut(self):
        # Retro-Riegel = Konfigurationsfehler -> fail-LOUD (HarterFehler), nicht stiller Signalverlust (B2)
        r = EnsembleRouter(modus="retro", gedaechtnis=_leeres_ged(),
                           adapter_fabrik=_fabrik({"openrouter": [("Lithium", "stark")]}))
        with self.assertRaises(HarterFehler):
            r.schlichte("alt", "Lithium")


class TestSchiedsrichter(unittest.TestCase):
    def test_praesenz_urteil(self):
        plaene = {"openrouter": [("Lithium", "stark")]}          # Schiedsrichter-Anbieter
        r = EnsembleRouter(slots=[], gedaechtnis=_leeres_ged(), adapter_fabrik=_fabrik(plaene))
        self.assertEqual(r.schlichte("t", "Lithium"), (True, "stark"))
        self.assertEqual(r.schlichte("t", "Windkraft"), (False, None))   # aus Cache, kein zweiter Aufruf

    def test_fail_closed_wenn_schiedsrichter_erschoepft(self):
        plaene = {a: QuotaFehler("q") for a in ("openrouter", "gemini")}
        r = EnsembleRouter(slots=[], gedaechtnis=_leeres_ged(), adapter_fabrik=_fabrik(plaene))
        self.assertEqual(r.schlichte("t", "Lithium"), (False, None))

    def test_harter_fehler_wird_nicht_gecacht(self):
        # B2: der Retro-Riegel (HarterFehler) propagiert und darf NICHT als negatives Ergebnis
        # dauerhaft im Cache landen (sonst verdeckt er den Konfigurationsfehler still).
        r = EnsembleRouter(modus="retro", gedaechtnis=_leeres_ged(),
                           adapter_fabrik=_fabrik({"openrouter": [("Lithium", "stark")]}))
        with self.assertRaises(HarterFehler):
            r.schlichte("t", "Lithium")
        self.assertNotIn("t", r._arbiter_cache)   # kein negativer Cache-Eintrag

    def test_erfolg_wird_genau_einmal_gecacht(self):
        # Erfolg (auch leere Kategorien-Menge) wird gecacht -> zweiter strittiger Kat kein zweiter Aufruf
        fab = _fabrik({"openrouter": [("Lithium", "stark")]})
        r = EnsembleRouter(slots=[], gedaechtnis=_leeres_ged(), adapter_fabrik=fab)
        r.schlichte("t", "Lithium")
        r.schlichte("t", "Kupfer")
        adapter = fab("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", None)
        self.assertEqual(adapter.aufrufe, 1)      # nur EIN Schiedsrichter-Aufruf je Doku


class TestFehlerklassifikation(unittest.TestCase):
    def test_status_mapping(self):
        self.assertIs(_klassifiziere_fehler(429, ""), QuotaFehler)
        self.assertIs(_klassifiziere_fehler(200, "rate limit exceeded"), QuotaFehler)
        self.assertIs(_klassifiziere_fehler(402, "payment"), HarterFehler)
        self.assertIs(_klassifiziere_fehler(401, ""), HarterFehler)
        self.assertIs(_klassifiziere_fehler(500, ""), TransienterFehler)
        self.assertIs(_klassifiziere_fehler(0, "timeout"), TransienterFehler)

    def test_fremd_exception_klassifiziert(self):
        # GeminiLLM wirft generisches RuntimeError -> muss als Quota erkannt werden
        self.assertIsInstance(_klassifiziere_exception(RuntimeError("quota exhausted")), QuotaFehler)
        self.assertIsInstance(_klassifiziere_exception(RuntimeError("HTTP 402 payment")), HarterFehler)
        self.assertIsInstance(_klassifiziere_exception(RuntimeError("connection reset")), TransienterFehler)

    def test_m1_generateContent_kein_quota(self):
        # Claude-QS M1: 'rate' darf NICHT in 'generateContent' matchen -> sonst wird JEDER Gemini-Fehler Quota
        self.assertIsInstance(_klassifiziere_exception(RuntimeError("generateContent: internal error")),
                              TransienterFehler)
        # echter Gemini-Quota-Fehler -> Quota
        self.assertIsInstance(_klassifiziere_exception(RuntimeError("generateContent: quota exhausted")),
                              QuotaFehler)
        # harter Key-Fehler -> Hart (vor Quota geprüft)
        self.assertIsInstance(_klassifiziere_exception(RuntimeError("generateContent: API key not valid")),
                              HarterFehler)

    def test_m4_tageslimit_erkennung(self):
        self.assertTrue(_ist_tageslimit("rate limit exceeded per day"))
        self.assertTrue(_ist_tageslimit("daily quota exhausted"))
        self.assertFalse(_ist_tageslimit("rate limit reached, retry in 20s"))   # RPM -> nicht tagespersistent


class TestQsFixes(unittest.TestCase):
    """Verhaltens-Tests der aus der Doppel-QS eingearbeiteten Befunde (M2/M4/M5/M6)."""

    def test_m4_rpm_quota_nicht_tagespersistent(self):
        # RPM-429 (Reset in Xs) sperrt den Lauf, aber NICHT den Tag; ein Tageslimit persistiert.
        ged = _leeres_ged()
        r = EnsembleRouter(slots=[("groq", "llama-3.3-70b-versatile")], gedaechtnis=ged,
                           adapter_fabrik=_fabrik({("groq", "llama-3.3-70b-versatile"):
                                                   QuotaFehler("rate limit reached, retry in 20s"),
                                                   "sambanova": [("Lithium", "stark")]}))
        r.stimmen()[0].kategorisiere("t")
        self.assertIn(("groq", "llama-3.3-70b-versatile"), r.z.lauf_erschoepft)   # Lauf: aus
        self.assertFalse(ged.ist_erschoepft("groq", "llama-3.3-70b-versatile"))   # Tag: NICHT persistiert

    def test_m4_tageslimit_persistiert(self):
        ged = _leeres_ged()
        r = EnsembleRouter(slots=[("groq", "llama-3.3-70b-versatile")], gedaechtnis=ged,
                           adapter_fabrik=_fabrik({("groq", "llama-3.3-70b-versatile"):
                                                   QuotaFehler("quota exceeded per day"),
                                                   "sambanova": [("Lithium", "stark")]}))
        r.stimmen()[0].kategorisiere("t")
        self.assertTrue(ged.ist_erschoepft("groq", "llama-3.3-70b-versatile"))    # Tag: persistiert

    def test_m5_schiedsrichter_meidet_abstimmende_familie(self):
        # Standard-Schiedsrichter (openrouter/nemotron) tot; gemini stimmt ab UND wäre nächster Arbiter-
        # Kandidat -> muss ausgeschlossen sein -> Arbiter fällt auf gpt_oss (openrouter), NICHT gemini.
        flach = {("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"): QuotaFehler("q"),
                 ("openrouter", "openai/gpt-oss-20b:free"): [("Lithium", "stark")],
                 ("gemini", "gemini-flash"): [("Kupfer", "stark")]}
        r = EnsembleRouter(slots=[("gemini", "gemini-flash")], gedaechtnis=_leeres_ged(),
                           adapter_fabrik=_fabrik(flach))
        praesent, _ = r.schlichte("t", "Lithium")
        self.assertTrue(praesent)                          # gpt_oss hat geantwortet
        arbiter_prov = [p for p in r.z.provenienz if p.get("slot") == "schiedsrichter" and "ergebnis" in p]
        self.assertEqual(arbiter_prov[-1]["familie"], "gpt_oss")   # NICHT gemini (abstimmende Familie)

    def test_m6_dedup_verhindert_kollaps_auf_selbes_modell(self):
        # zwei llama-Slots; groq tot, NUR sambanova-llama lebt (alle Familien-Wechsel-Ziele tot) ->
        # Slot 1 nimmt sambanova; Slot 2 darf NICHT dasselbe Modell erneut nehmen (Dedup) -> Enthaltung
        # (keine korrelierte Zweitstimme). Mit lebender Fremd-Familie würde Slot 2 korrekt dorthin wechseln.
        plaene = {("groq", "llama-3.3-70b-versatile"): QuotaFehler("q"),
                  ("sambanova", "Meta-Llama-3.3-70B-Instruct"): [("Lithium", "stark")]}
        for a in ("gemini", "mistral", "ollama", "openrouter"):
            plaene[a] = QuotaFehler("q")
        for m in ("llama-3.1-8b-instant",):
            plaene[("groq", m)] = QuotaFehler("q")
        plaene[("sambanova", "Meta-Llama-3.1-8B-Instruct")] = QuotaFehler("q")
        r = EnsembleRouter(slots=[("groq", "llama-3.3-70b-versatile"),
                                  ("sambanova", "Meta-Llama-3.3-70B-Instruct")],
                           gedaechtnis=_leeres_ged(), adapter_fabrik=_fabrik(plaene))
        s1, s2 = r.stimmen()
        self.assertEqual(s1.kategorisiere("t"), [("Lithium", "stark")])   # Slot 1 nimmt sambanova
        with self.assertRaises(EnthaltungsFehler):                        # Slot 2: sambanova belegt, Rest tot -> Enthaltung
            s2.kategorisiere("t")

    def test_m2_200_body_code_wird_hart(self):
        # HTTP 200 mit {"error":{"code":402}} -> HarterFehler (nicht transient/Endlos-Retry)
        llm = OpenAICompatLLM(anbieter="openrouter", model="x", base_url="http://x/v1", key_env=None,
                              throttle_s=0)
        llm._curl = lambda body: (200, json.dumps({"error": {"code": 402, "message": "needs credits"}}))
        with self.assertRaises(HarterFehler):
            llm.kategorisiere("t")


class TestParser(unittest.TestCase):
    def test_json_aus_reasoning_und_fences(self):
        self.assertEqual(_extrahiere_json_objekt('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(_extrahiere_json_objekt('Denke... {"kategorien":[]} fertig')["kategorien"], [])
        self.assertIsNone(_extrahiere_json_objekt("kein json hier"))

    def test_m1_bevorzugt_objekt_mit_schluessel(self):
        # Reasoning-Vorspann-Objekt {"thought":…} vor der echten Antwort -> das mit `kategorien` gewinnt
        txt = '{"thought":"hmm"} dann {"kategorien":[{"kategorie":"Lithium","staerke":"stark"}]}'
        obj = _extrahiere_json_objekt(txt, bevorzugt_schluessel="kategorien")
        self.assertIn("kategorien", obj)


if __name__ == "__main__":
    unittest.main(verbosity=1)
