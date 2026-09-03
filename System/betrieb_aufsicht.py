"""
betrieb_aufsicht.py — Schicht S: die PROZESS-UNABHÄNGIGE Laufzeit-Aufsicht (v0-Kern).

Betriebs- und Kontroll-Schicht (Aufgaben S3/S6/S7/S8/S9/S10).
Der Wächter ist ein SEPARATER Prozess (kein Hook in scraper.py/watchdog_batch.sh); er LIEST von außen
(Heartbeats, DB-Frische, rohe HTTP-Codes) und schreibt in eine EIGENE, physisch getrennte ops-DB —
damit er den Ausfall der überwachten Speicher/Prozesse ÜBERLEBT und melden kann.

**Konzept-Grenze (§11):** S *aggregiert/trendet/alarmiert*, misst KEINE fachliche Qualität selbst (sonst
Modul-8-Nachbau). Dieser Kern ist rein TECHNISCH. Die Alpha-Achse (S6-alpha) bleibt bei Modul 8/16.

**🔑 Tragende Invariante: STILLE ≠ GRÜN** (fail-closed durchgezogen, QS-Runde 2): ein fehlendes/veraltetes/
zukunfts-gestempeltes Heartbeat-Signal, ein `None`-Lauf, ein fehlender Quota-Beleg — nichts davon wird je
stumm als gesund gelesen; im Zweifel `gelb`/`rot`, nie `gruen`.

**Evaluatoren (§3):** `liveness_pruefung`/`dead_mans_switch` (Prozess-Tod), `stagnation_pruefung` (Quellen-
Leerlauf, F114), `quota_pruefung` (LLM-Kapazität ROH aus HTTP-Codes, §3.5), `canary_pruefung` (Fähigkeits-
Probe Auth/Format/Deprecation/3.12, F110), `pruefe_dokument_kontrakte` (operativer documents/facts-Kontrakt-
BEOBACHTER, F113 — liest, blockiert NIE). Alle liefern EIN einheitliches Dict (status/kategorie/drift_art/
empfehlung/beleg), direkt an `schreibe_gesundheit` übergebbar (QS-B1). Vokabular für den Kontrakt-Beobachter ist
IMPORTIERT (sammler_db/contracts, KEINE INSEL).

**Alle Evaluatoren REIN (Zeit injizierbar, kein `datetime.now()`).** Die LIVE-Nähte (echte scraper.db-`n_neu`,
echte HTTP-Codes, echte Prozess-Heartbeats + die erwartete Loop-Menge, die echte Canary-Probe) sind home-gated.
Nur Standardbibliothek + projektinterne Vokabular-Importe. F109–F114 im Feinkonzept entschieden.
"""
import json
import sqlite3

STATUS = ("gruen", "gelb", "rot")
KATEGORIEN = ("technik", "qualitaet", "alpha", "ressource")
DRIFT_ARTEN = ("keine", "technisch", "alpha")
EMPFEHLUNGEN = ("ok", "reparatur", "quelle_pausieren", "ressource_umschalten",
                "provider_deaktivieren", "prozess_neustart", "mensch_tor")
_ALERT_ZUSTAENDE = (None, "aktiv", "beobachtend", "geheilt", "quittiert")
_SKEW_TOLERANZ_SEK = 120        # kleine Uhr-Jitter zwischen Prozessen ist normal; darüber = verdächtig
_DUP_SCHWELLE = 0.999           # 99.9 % Dubletten = effektiv stagnant (float-robust statt exakt 1.0)

SCHEMA_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;       -- QS-M4: zwei Schreiber (Bash-Grind + Python-Wächter) -> Lock-Wartezeit statt Crash
CREATE TABLE IF NOT EXISTS system_gesundheit (
  id INTEGER PRIMARY KEY, komponente TEXT, t_ereignis TEXT, t_ingest TEXT,
  kategorie TEXT, metrik TEXT, wert_numerisch REAL, schwelle REAL, status TEXT,
  drift_art TEXT, empfehlung TEXT, beleg TEXT);
CREATE INDEX IF NOT EXISTS ix_gesundheit_komp ON system_gesundheit(komponente, metrik);
CREATE TABLE IF NOT EXISTS alert_zustand (
  komponente TEXT, metrik TEXT, zustand TEXT, seit TEXT, letzte_aenderung TEXT,
  PRIMARY KEY (komponente, metrik));
CREATE TABLE IF NOT EXISTS monitor_heartbeat (
  loop TEXT PRIMARY KEY, letzter_tick TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS steuer_audit (
  id INTEGER PRIMARY KEY, t TEXT, akteur TEXT, aktion TEXT, ziel TEXT, wert TEXT);
-- Transparenz-/Kontroll-Schicht (F128/F131/F133): Prozess-Fortschritt + Control-Plane + Lauf-Diagnose.
CREATE TABLE IF NOT EXISTS prozess_status (
  prozess TEXT PRIMARY KEY, phase TEXT, aktuell REAL, gesamt REAL, gestartet TEXT,
  aktualisiert TEXT, herzschlag TEXT, zustand TEXT, beleg TEXT);
CREATE TABLE IF NOT EXISTS prozess_steuerung (
  prozess TEXT PRIMARY KEY, gewuenscht TEXT, gesetzt_von TEXT, gesetzt_am TEXT);
CREATE TABLE IF NOT EXISTS lauf_diagnose (
  id INTEGER PRIMARY KEY, t TEXT, kennz TEXT);
CREATE TABLE IF NOT EXISTS vorwaerts_verdikt (
  id INTEGER PRIMARY KEY, t TEXT, power TEXT, gereift TEXT);
"""

# F128/F131 Vokabulare (fail-closed erzwungen).
PROZESS_ZUSTAENDE = ("läuft", "pausiert", "gestoppt", "fertig", "fehler", "ruht")
STEUER_WUNSCH = ("run", "pause", "stop")
_TERMINAL_ZUSTAENDE = ("gestoppt", "fertig", "ruht")   # Fable-m2: kein tot-Alarm auf legitim beendeten Prozessen
_LEBEND_ZUSTAENDE = ("läuft", "pausiert")              # nur diese heartbeaten -> nur diese können `tot` werden


# ------------------------------------------------------------------ #
# Zeit-Helfer (injizierbar; tz-robust; Subtraktion IM try, QS-M1)
# ------------------------------------------------------------------ #
def _sekunden_zwischen(frueher, spaeter):
    """spaeter − frueher in Sekunden (ISO-Zeitstempel). Gemischt naive/aware -> beide naiv normiert (kein
    TypeError-Crash, QS-M1). None/Formfehler/jede Ausnahme -> None (fail-closed)."""
    import datetime
    try:
        a = datetime.datetime.fromisoformat(str(frueher)).replace(tzinfo=None)
        b = datetime.datetime.fromisoformat(str(spaeter)).replace(tzinfo=None)
        return (b - a).total_seconds()
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ #
# §3.1 Liveness-Wächter + Dead-Man's-Switch — der Prozess-Tod-Melder
# ------------------------------------------------------------------ #
def liveness_pruefung(heartbeats, jetzt, max_alter_sek, erwartete=None):
    """heartbeats: {loop: letzter_tick_iso}; jetzt: iso; max_alter_sek: {loop: grenze} ODER int (global);
    erwartete: Menge der ERWARTETEN Loops (Pflicht im int-Modus — QS-BLOCKER: sonst ist ein NIE-gemeldeter
    Prozess unsichtbar). -> {loop: {'status','alter_sek','drift_art','beleg'}} über die ERWARTETE Menge.
    Ein erwarteter Loop, der (a) fehlt = nie gemeldet, (b) veraltet, (c) zukunfts-gestempelt jenseits der
    Skew-Toleranz, (d) unlesbar -> `rot`/`gelb`, NIE stumm grün."""
    if erwartete is None:
        if isinstance(max_alter_sek, dict):
            erwartete = set(max_alter_sek)
        else:
            raise ValueError("liveness_pruefung: `erwartete` Loop-Menge Pflicht im int-Modus "
                             "(ein nie-gemeldeter Prozess ist sonst unsichtbar — QS-BLOCKER)")
    # QS-MINOR-4: fehlt einem erwarteten Loop im dict-Modus die Schwelle (Config-Drift: kanonische Loop-Menge
    # ⊋ Schwellen-dict), NICHT crashen — die Schwelle als None melden -> der Loop wird `rot` (Config-Lücke sichtbar).
    grenze_fn = (lambda lp: max_alter_sek.get(lp)) if isinstance(max_alter_sek, dict) else (lambda lp: max_alter_sek)
    out = {}
    for loop in sorted(erwartete):
        tick = (heartbeats or {}).get(loop)
        grenze = grenze_fn(loop)
        if not tick:
            out[loop] = _live_rot(None, "kein Heartbeat (Prozess nie gemeldet / tot)")
        elif grenze is None:
            out[loop] = _live_rot(None, f"keine Max-Age-Schwelle für '{loop}' konfiguriert (Config-Lücke)")
        elif (alter := _sekunden_zwischen(tick, jetzt)) is None:
            out[loop] = _live_rot(None, "Heartbeat-Zeitstempel unlesbar")
        elif alter < -_SKEW_TOLERANZ_SEK:
            out[loop] = _live_rot(alter, f"Zukunfts-Zeitstempel {alter:.0f}s (Clock-Skew — Liveness nicht verifizierbar)")
        elif alter > grenze:
            out[loop] = _live_rot(alter, f"Heartbeat {alter:.0f}s alt > {grenze}s (tot/hängt)")
        else:
            out[loop] = {"status": "gruen", "kategorie": "technik", "alter_sek": alter,
                         "drift_art": "keine", "empfehlung": "ok", "beleg": "lebendig"}
    return out


def _live_rot(alter, beleg):
    """Einheitliches rot-Urteil des Liveness-Wächters (QS-B1: konsistentes Evaluator-Schema —
    status/kategorie/drift_art/empfehlung/beleg, direkt an `schreibe_gesundheit` übergebbar)."""
    return {"status": "rot", "kategorie": "technik", "alter_sek": alter, "drift_art": "technisch",
            "empfehlung": "prozess_neustart", "beleg": beleg}


def dead_mans_switch(monitor_ticks, jetzt, max_alter_sek, erwartete=None):
    """Der Monitor-des-Monitors über die EIGENEN Wächter-Ticks. **QS-B3 (extern aufzurufen!):** ein
    abgestürzter Wächter ruft dies nicht mehr selbst — der Aufruf MUSS von einem EXTERNEN Anker kommen
    (die gegenseitige Kreuz-Prüfung Bash↔Python, Feinkonzept §3.1; ODER cron/systemd-Probe). Gleiche
    fail-closed-Logik wie liveness_pruefung; ein nie-getickter Wächter (erwartet, aber fehlend) = `rot`."""
    return liveness_pruefung(monitor_ticks, jetzt, max_alter_sek, erwartete=erwartete)


# ------------------------------------------------------------------ #
# §3.4 Quellen-Stagnations-/Leerlauf-Monitor (drift_art IMMER technisch, F114)
# ------------------------------------------------------------------ #
def _ist_lieferung(v):
    """Genau eine positive endliche Zahl zählt als echte Lieferung. Alles andere — 0, None, NaN, negativ,
    String, bool — ist „nichts geliefert" (QS-M6/MINOR-6 symmetrisch fail-closed: kein gescheiterter/kaputter
    Lauf-Wert darf die Stagnation maskieren)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0


def _no_delivery_streak(reihe):
    """Trailing-Streak von „nichts geliefert" (jeder Nicht-Lieferung-Wert zählt mit)."""
    n = 0
    for v in reversed(reihe or []):
        if _ist_lieferung(v):
            break
        n += 1
    return n


def stagnation_pruefung(n_neu_reihe, K, dup_anteil_reihe=None):
    """n_neu_reihe: [n_neu je Lauf, jüngster zuletzt] (`None` = Lauf gescheitert/keine Zahl); K: Schwelle
    (aus Kadenz §3.6); dup_anteil_reihe: optional [Anteil neuer Docs als dup_of kanonisiert]. -> {'status',
    'streak','drift_art':'technisch','beleg'}. rot ab K „nichts geliefert" (0/None) ODER K Läufen mit
    dup_anteil ≥ 0.999; gelb ab K/2. **QS-B4:** leere Reihe / keine Baseline -> `gelb` (Kaltstart-beobachtend,
    NIE grün). Jeder echte n_neu>0 setzt den Streak zurück."""
    if K <= 0:
        return {"status": "gruen", "streak": 0, "kategorie": "technik", "drift_art": "keine",
                "empfehlung": "ok", "beleg": "K<=0 (deaktiviert)"}
    if not n_neu_reihe:
        return {"status": "gelb", "streak": 0, "kategorie": "technik", "drift_art": "technisch",
                "empfehlung": "ok", "beleg": "keine Baseline (Kaltstart — beobachtend, F109)"}
    streak = _no_delivery_streak(n_neu_reihe)
    grund = f"{streak} Läufe ohne neues Dokument (n_neu=0/None)"
    if dup_anteil_reihe:
        dup_streak = 0
        for a in reversed(dup_anteil_reihe):
            if a is not None and a >= _DUP_SCHWELLE:
                dup_streak += 1
            else:
                break
        if dup_streak > streak:
            streak, grund = dup_streak, f"{dup_streak} Läufe ≥{_DUP_SCHWELLE:.1%} Dubletten (effektiv stagnant)"
    if streak >= K:
        status, empf = "rot", "quelle_pausieren"
    elif streak >= K / 2:
        status, empf = "gelb", "ok"
    else:
        status, empf = "gruen", "ok"
    return {"status": status, "streak": streak, "kategorie": "technik",
            "drift_art": "technisch" if status != "gruen" else "keine", "empfehlung": empf, "beleg": grund}


# ------------------------------------------------------------------ #
# §3.5 LLM-Kapazitäts-/Quota-Monitor — ROH aus HTTP-Codes (nie nur Router-Selbstauskunft)
# ------------------------------------------------------------------ #
_ERSCHOEPFT_CODES = (429, 402)      # Rate/Quota (429) + Payment (402) -> umschalten
_GESPERRT_CODES = (400, 401, 403)   # Bad-Request/Auth/Forbidden -> Provider hart gesperrt (deaktivieren)


def _als_int(x):
    """HTTP-Code robust zu int (die Live-Naht kann ihn als '429'-String liefern; QS-MINOR-5). Sonst None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    try:
        return int(str(x).strip())
    except (ValueError, TypeError):
        return None


def quota_pruefung(provider_signale, rest_warn_anteil=0.1):
    """provider_signale: {provider: {'http_code':int|None, 'body_fehlercode':int|None, 'rest_budget':float|None,
    'budget_total':float|None}} — die ROHEN Belege (echte HTTP-Antwort), NICHT die Router-Selbstauskunft
    (§3.5/QS-M2/M7). -> {provider: {'status','kategorie':'ressource','drift_art','empfehlung','beleg'}}.
    429/402 (Code ODER Body) = erschöpft (rot, umschalten); 400/401/403 = gesperrt (rot, provider_deaktivieren).
    Rest-Budget < `rest_warn_anteil` = proaktiv gelb.
    **QS-M3/MAJOR-1 (fail-closed, headline-Invariante):** `gruen` NUR bei 2xx UND `body_fehlercode is None`.
    Ein Fehlercode im 200-Body (die Semantik-Kanal-2-Falle) ODER ein unbekannter Body-Fehler ODER ein
    fehlender Beleg -> NIE stumm grün (rot bei bekanntem Sperr-/Erschöpft-Code, sonst gelb unbestimmt)."""
    out = {}
    for prov, sig in (provider_signale or {}).items():
        sig = sig or {}
        code, body = _als_int(sig.get("http_code")), _als_int(sig.get("body_fehlercode"))
        rest, total = sig.get("rest_budget"), sig.get("budget_total")
        if code in _ERSCHOEPFT_CODES or body in _ERSCHOEPFT_CODES:
            out[prov] = _quota_urteil("rot", "ressource_umschalten", f"HTTP {code}/body {body} = erschöpft (Quota/Payment)")
        elif code in _GESPERRT_CODES or body in _GESPERRT_CODES:
            out[prov] = _quota_urteil("rot", "provider_deaktivieren", f"HTTP {code}/body {body} = gesperrt (Auth/Bad-Request)")
        elif total is not None and total > 0 and rest is not None and (rest / total) < rest_warn_anteil:
            out[prov] = _quota_urteil("gelb", "ressource_umschalten",
                                      f"Rest-Budget {rest}/{total} < {rest_warn_anteil:.0%} (proaktiv)")
        elif sig.get("http_code") is None and sig.get("body_fehlercode") is None and rest is None:
            out[prov] = _quota_urteil("gelb", "reparatur",
                                      "keine Belege (Signal-Beschaffung ausgefallen — unbestimmt, nicht gesund)")
        elif body is not None:
            # 200 mit unbekanntem Body-Fehlercode -> NICHT grün (die Falle: Fehler versteckt im 200er-Body)
            out[prov] = _quota_urteil("gelb", "reparatur", f"Body-Fehlercode {body} unbekannt (unbestimmt, nicht gesund)")
        elif code is not None and 200 <= code < 300:
            out[prov] = _quota_urteil("gruen", "ok", "Kapazität ok")
        else:
            out[prov] = _quota_urteil("gelb", "reparatur", f"unerwarteter/unlesbarer HTTP {sig.get('http_code')!r} (unbestimmt)")
    return out


def _quota_urteil(status, empfehlung, beleg):
    """Einheitliches Quota-Evaluator-Schema (QS-B1: `drift_art` mitgeführt — Quota-Erschöpfung ist ein
    Ressource-Ereignis, KEIN Drift → drift_art='keine'; hält das Übergabe-Schema an schreibe_gesundheit rein)."""
    return {"status": status, "kategorie": "ressource", "drift_art": "keine", "empfehlung": empfehlung, "beleg": beleg}


# ------------------------------------------------------------------ #
# §3.5/F110 Capability-Canary — REINER Klassifikator einer Probe-Antwort (Live-Probe home-gated)
# ------------------------------------------------------------------ #
_AUTH_CODES = (401, 403)                                 # Auth/Endpoint (Schlüssel/Recht weg)
_LAST_CODES = (429, 402, 500, 502, 503, 504)             # Last/Quota/Server -> NICHT Canary, §3.5


def canary_pruefung(antwort, erwartetes_schema=None):
    """REINER Klassifikator der Antwort auf den gepinnten Ein-Token-Canary (F110). Live-Probe (Prompt senden)
    ist home-gated; DIESE Funktion urteilt nur über die schon geholte `antwort`.
    antwort: {'http_code':int|None, 'modell_existiert':bool|None, 'geparst':dict|None}.
    erwartetes_schema: {'pflicht_felder':[...], 'ordinal_feld':str, 'ordinal_werte':set} (optional).
    **Scope HART begrenzt (F110):** Auth/Endpoint · Antwort-Schema/Format · Modell-Existenz/Deprecation ·
    ordinal/3.12 — NICHT last-/größenabhängig (429/402/5xx bleiben §3.5 quota_pruefung). Fail-closed:
    keine Antwort / Last-blockiert / unbestimmt -> `gelb` (Fähigkeit NICHT verifiziert), NIE stumm grün."""
    if not antwort:
        return _canary("gelb", "reparatur", "keine Probe-Antwort (Fähigkeit nicht verifizierbar)")
    code = antwort.get("http_code")
    if code in _AUTH_CODES:
        return _canary("rot", "provider_deaktivieren", f"HTTP {code} = Auth/Endpoint (Schlüssel/Recht weg)")
    if code == 404 or antwort.get("modell_existiert") is False:
        return _canary("rot", "provider_deaktivieren", "Modell nicht gefunden / deprecatet (Endpoint/Deprecation)")
    if code in _LAST_CODES:
        return _canary("gelb", "ressource_umschalten",
                       f"HTTP {code} = Last/Quota — an §3.5 übergeben, Fähigkeit unbestimmt")
    geparst = antwort.get("geparst")
    if geparst is None:
        return _canary("rot", "reparatur", "Antwort nicht als erwartetes Schema parsebar (Format-Kippe)")
    schema = erwartetes_schema or {}
    for feld in schema.get("pflicht_felder", []):
        if feld not in geparst:
            return _canary("rot", "reparatur", f"Pflichtfeld '{feld}' fehlt (Format-Kippe)")
    ord_feld, ord_werte = schema.get("ordinal_feld"), schema.get("ordinal_werte")
    if ord_feld is not None and ord_werte is not None:
        wert = geparst.get(ord_feld)
        if isinstance(wert, (int, float)) and not isinstance(wert, bool):
            return _canary("rot", "reparatur",
                           f"'{ord_feld}'={wert} ist numerisch statt ordinal (3.12-Kippe — Dezimalkonfidenz)")
        if wert not in ord_werte:
            return _canary("rot", "reparatur", f"'{ord_feld}'={wert!r} nicht im Ordinalvokabular {sorted(ord_werte)}")
    if code == 200 or (code is not None and 200 <= code < 300):
        return _canary("gruen", "ok", "Fähigkeit ok (Auth/Format/Ordinal bestanden)")
    return _canary("gelb", "reparatur", f"unerwarteter HTTP {code} (Fähigkeit unbestimmt)")


def _canary(status, empfehlung, beleg):
    return {"status": status, "kategorie": "technik", "drift_art": "technisch" if status == "rot" else "keine",
            "empfehlung": empfehlung, "beleg": beleg}


# ------------------------------------------------------------------ #
# §3.2/F113 documents/facts-Kontrakt-BEOBACHTER (liest, blockiert NIE — die Naht erzwingt selbst)
# ------------------------------------------------------------------ #
_DOC_QUELLTYP = ("paper", "patent", "funding", "news")   # geschlossenes Zielvokabular (F113)


def _in_einheit(wert):
    """numerischer Wert ∈ [0,1]? (None = ungesetzt = ok)."""
    return wert is None or (isinstance(wert, (int, float)) and not isinstance(wert, bool) and 0.0 <= wert <= 1.0)


def pruefe_dokument_kontrakte(conn, limit=100000, rot_anteil=0.01):
    """BEOBACHTET die operativen documents/facts-Kontrakte (F113) auf einer scraper.db-`conn` und meldet
    strukturelle Verstöße — **blockiert NIE** (die Durchsetzung bleibt an der Produzenten-Naht, der Wächter
    liest nur, Feinkonzept §3.2). Vokabular IMPORTIERT aus `connectors.sammler_db`/`harness.contracts`
    (KEINE INSEL — keine zweite Definition von source_type/modus/signalart/reife). -> {'status','kategorie':
    'technik','drift_art':'technisch','empfehlung','verstoesse':[...],'beleg'}. Fehlende/leere Tabelle ->
    `gelb` (Kaltstart-beobachtend, fail-closed — NIE stumm grün). Jeder Verstoß -> `rot`."""
    from connectors.sammler_db import _MODUS, _SIGNALART                       # noqa: E402  (KEINE INSEL)
    vorhandene = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "documents" not in vorhandene or "facts" not in vorhandene:
        return _kontrakt_urteil("gelb", "reparatur", [], "documents/facts-Tabelle fehlt (Kaltstart/leer)")
    n_doc = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_fact_geprueft = 0
    verstoesse = []
    for row in conn.execute(
            "SELECT id, source_type, title, published_at, relevance, trust FROM documents LIMIT ?", (limit,)):
        i, st, titel, pub, rel, tru = row
        if (st or "").strip().lower() not in _DOC_QUELLTYP:
            verstoesse.append({"tabelle": "documents", "id": i, "feld": "source_type", "wert": st,
                               "problem": f"nicht im geschlossenen Vokabular {list(_DOC_QUELLTYP)}"})
        if not (titel or "").strip():
            verstoesse.append({"tabelle": "documents", "id": i, "feld": "title", "wert": titel, "problem": "leer/fehlt"})
        if not (pub or "").strip():
            verstoesse.append({"tabelle": "documents", "id": i, "feld": "published_at", "wert": pub,
                               "problem": "leer (Offenlegungszeit fehlt)"})
        if not _in_einheit(rel):
            verstoesse.append({"tabelle": "documents", "id": i, "feld": "relevance", "wert": rel, "problem": "∉ [0,1]"})
        if not _in_einheit(tru):
            verstoesse.append({"tabelle": "documents", "id": i, "feld": "trust", "wert": tru, "problem": "∉ [0,1]"})
    for row in conn.execute(
            "SELECT id, doc_id, subjekt, beziehung, objekt, modus, signalart, reife, reife_score, "
            "erwartungstempo, konfidenz FROM facts LIMIT ?", (limit,)):
        i, did, subj, bez, obj, modus, sig, reife, rscore, etempo, konf = row
        n_fact_geprueft += 1
        if did is None:
            verstoesse.append({"tabelle": "facts", "id": i, "feld": "doc_id", "wert": did, "problem": "fehlt (Waise)"})
        for feld, wert in (("subjekt", subj), ("beziehung", bez), ("objekt", obj)):
            if not (wert or "").strip():
                verstoesse.append({"tabelle": "facts", "id": i, "feld": feld, "wert": wert, "problem": "leer/fehlt"})
        if modus is not None and modus not in _MODUS:
            verstoesse.append({"tabelle": "facts", "id": i, "feld": "modus", "wert": modus,
                               "problem": f"∉ {sorted(_MODUS)}"})
        if sig is not None and sig not in _SIGNALART:
            verstoesse.append({"tabelle": "facts", "id": i, "feld": "signalart", "wert": sig,
                               "problem": f"∉ {sorted(_SIGNALART)}"})
        # `reife` ist ein MATURITAETS-STUFEN-Label (scraper._reife_label: Grundlagenforschung..Markt), KEINE
        # ordinale Staerke — der Kontrakt (contracts.py) constraint `reife` NICHT auf ORDINAL_STAERKE (das ist die
        # Kategorie-ZUORDNUNGS-Staerke, ein anderes Feld). Die harte Invariante ist `reife_score ∈ [0,1]` (unten
        # geprueft); das daraus abgeleitete Stufen-Label wird NICHT gegen das falsche Vokabular geprueft.
        # Realdaten-Befund (2026-08-06, Wächter am echten Produzenten): die alte `reife ∈ ORDINAL_STAERKE`-Prüfung
        # flaggte 30153 valide Fakten (jedes) falsch-positiv = Schein-Test-Riegel (gegen Synthetik-Fakten gebaut).
        for feld, wert in (("reife_score", rscore), ("erwartungstempo", etempo), ("konfidenz", konf)):
            if not _in_einheit(wert):
                verstoesse.append({"tabelle": "facts", "id": i, "feld": feld, "wert": wert, "problem": "∉ [0,1]"})
    if n_doc == 0:
        return _kontrakt_urteil("gelb", "ok", verstoesse, "documents leer (Kaltstart — beobachtend)")
    if not verstoesse:
        return _kontrakt_urteil("gruen", "ok", [], "documents/facts kontrakt-konform")
    # Schwere PROPORTIONAL (Jens 08.08.): der Beobachter „blockiert nie" — vereinzelte malformte Zeilen in einer
    # 100k-Zeilen-Produktions-DB sind eine DATENQUALITAETS-Notiz (gelb), KEIN Betriebs-Kritisch. Erst wenn ein
    # SYSTEMISCHER Anteil bricht (>= rot_anteil), ist es rot. (Frueher: jeder einzelne Verstoss -> rot -> der
    # ganze Leitstand rot wegen 7/100000 = 0,007 %.)
    grund = max(1, n_doc + n_fact_geprueft)
    anteil = len(verstoesse) / grund
    if anteil >= rot_anteil:
        return _kontrakt_urteil("rot", "mensch_tor", verstoesse,
                                f"{len(verstoesse)} Kontrakt-Verstöße von {grund} Zeilen "
                                f"({anteil:.1%}) — systemisch")
    return _kontrakt_urteil("gelb", "datenqualitaet_pruefen", verstoesse,
                            f"{len(verstoesse)} vereinzelte Kontrakt-Verstöße von {grund} Zeilen "
                            f"({anteil:.2%}) — Datenqualität, kein Betriebsfehler")


def _kontrakt_urteil(status, empfehlung, verstoesse, beleg):
    return {"status": status, "kategorie": "technik", "drift_art": "technisch" if status == "rot" else "keine",
            "empfehlung": empfehlung, "verstoesse": verstoesse, "beleg": beleg}


# ------------------------------------------------------------------ #
# Konzept B (Fable-B11): scraper.db-Drive-Stand-Frische — „Stille ≠ Grün" für das Backup
# ------------------------------------------------------------------ #
def drive_stand_frische_pruefung(manifest_timestamp, jetzt, gelb_tage=2.0, rot_tage=7.0):
    """Frische des jüngsten scraper.db-Drive-Stands (Konzept B §8, Fable-B11): ist das Drive-Manifest
    (`scraper_db_manifest.json`, Feld `timestamp` aus `scraper_db_drive.sync_scraper_db`) älter als
    X Tage, ist die reclaim-feste Kopie der Signal-Rohquelle veraltet → gelb/rot im Leitstand (Modul 17).
    REIN (Zeit injizierbar, kein `datetime.now()`); die LIVE-Naht (Manifest von Drive lesen) bleibt
    home/creds-gated. Fail-closed („Stille ≠ Grün"): fehlender Stand (None), unlesbarer oder
    Zukunfts-Zeitstempel (Clock-Skew) → NIE grün — ein Backup, dessen Alter man nicht kennt, ist keins.
    -> einheitliches Evaluator-Dict (status/kategorie/alter_tage/drift_art/empfehlung/beleg, QS-B1)."""
    def _urteil(status, alter_tage, empf, beleg):
        return {"status": status, "kategorie": "technik", "alter_tage": alter_tage,
                "drift_art": "technisch" if status != "gruen" else "keine",
                "empfehlung": empf, "beleg": beleg}
    if not manifest_timestamp:
        return _urteil("rot", None, "reparatur",
                       "kein scraper.db-Stand auf Drive (Sync nie gelaufen / Manifest fehlt)")
    sek = _sekunden_zwischen(manifest_timestamp, jetzt)
    if sek is None:
        return _urteil("rot", None, "reparatur", "Drive-Manifest-Zeitstempel unlesbar")
    if sek < -_SKEW_TOLERANZ_SEK:
        return _urteil("rot", sek / 86400.0, "reparatur",
                       f"Zukunfts-Zeitstempel ({sek:.0f}s — Clock-Skew, Frische nicht verifizierbar)")
    tage = sek / 86400.0
    if tage > rot_tage:
        return _urteil("rot", tage, "reparatur",
                       f"scraper.db-Drive-Stand {tage:.1f} Tage alt > {rot_tage} (Backup veraltet — "
                       f"Heim-Sync tot?)")
    if tage > gelb_tage:
        return _urteil("gelb", tage, "ok", f"scraper.db-Drive-Stand {tage:.1f} Tage alt > {gelb_tage}")
    return _urteil("gruen", tage, "ok", f"scraper.db-Drive-Stand frisch ({tage:.1f} Tage)")


# ------------------------------------------------------------------ #
# Datenpflege W5: Fundamentals-Cache-Frische — „Stille ≠ Grün" für den cache_only-Rechenpfad
# ------------------------------------------------------------------ #
def cache_frische_pruefung(aeltester_stand, jetzt, gelb_tage=120.0, rot_tage=240.0):
    """Frische des Fundamentals-Caches, auf dem `bewertung_reverse_dcf(cache_only=True)` rechnet
    (Feinkonzept Datenpflege §8/W5): `aeltester_stand` = der `diag['aeltester_stand']` des cache_only-
    Laufs (aeltestes `letztes_filing` der VERWENDETEN Symbole, ISO). Ist er aelter als X Tage, ist der
    asynchrone Auffrischer (`datenpflege.tick`) tot/verhungert → die Bewertung rechnet auf veralteter
    Realitaet. Defaults: gelb > 120 Tage (ein Quartal + Filing-Latenz ist normal), rot > 240 (zwei
    verpasste Zyklen). REIN (Zeit injizierbar, kein `datetime.now()`); die LIVE-Naht (diag → Wächter)
    ist home-gated. Fail-closed („Stille ≠ Grün"): fehlender Stand (None — z. B. cache_only-Lauf ohne
    ein einziges nutzbares Symbol), unlesbarer oder Zukunfts-Zeitstempel → NIE grün.
    -> einheitliches Evaluator-Dict (status/kategorie/alter_tage/drift_art/empfehlung/beleg, QS-B1)."""
    def _urteil(status, alter_tage, empf, beleg):
        return {"status": status, "kategorie": "technik", "alter_tage": alter_tage,
                "drift_art": "technisch" if status != "gruen" else "keine",
                "empfehlung": empf, "beleg": beleg}
    if not aeltester_stand:
        return _urteil("rot", None, "reparatur",
                       "kein Cache-Stand (cache_only ohne nutzbares Symbol / Coverage leer — "
                       "leer ist NICHT frisch)")
    sek = _sekunden_zwischen(aeltester_stand, jetzt)
    if sek is None:
        return _urteil("rot", None, "reparatur", "Cache-Stand-Zeitstempel unlesbar")
    if sek < -_SKEW_TOLERANZ_SEK:
        return _urteil("rot", sek / 86400.0, "reparatur",
                       f"Zukunfts-Zeitstempel ({sek:.0f}s — Frische nicht verifizierbar)")
    tage = sek / 86400.0
    if tage > rot_tage:
        return _urteil("rot", tage, "reparatur",
                       f"aeltester Fundamentals-Stand {tage:.0f} Tage alt > {rot_tage:.0f} "
                       f"(Auffrischer tot? Quota dauerhaft erschoepft?)")
    if tage > gelb_tage:
        return _urteil("gelb", tage, "ok",
                       f"aeltester Fundamentals-Stand {tage:.0f} Tage alt > {gelb_tage:.0f}")
    return _urteil("gruen", tage, "ok", f"Fundamentals-Cache frisch (aeltester Stand {tage:.0f} Tage)")


def datenluecken_pruefung(bereitschaft, gelb_anteil=0.05, rot_anteil=0.20):
    """DATENLÜCKEN-Überwachung (Jens 07.08.): der Rechenpfad rechnet cache-only — fehlen dem Cache Symbole,
    rechnet er auf einem LÜCKENHAFTEN Universum (verzerrte/fehlende Folds). Dieser Evaluator urteilt über den
    `daten_bereitschaft`-Report (`retro_voll_run.outcome_eod_bereitschaft`): `{n, n_bereit, n_leer,
    n_delistet_kurz, n_fehlend, prozent}`. **Lücke** = `n_fehlend/n` (die ECHT unaufgelösten, NACHLADBAREN
    Symbole — `n_leer`/`n_delistet_kurz` sind aufgelöst = kein Loch). rot > `rot_anteil`, gelb > `gelb_anteil`.
    **Fail-closed („Stille ≠ Grün"):** kein/leerer Report (`n<=0` oder None) -> `rot` (leer ist NICHT vollständig).
    REIN (kein I/O). -> Evaluator-Dict {status/kategorie/anteil/n_fehlend/drift_art/empfehlung/beleg}."""
    def _urteil(status, anteil, n_fehlend, empf, beleg):
        return {"status": status, "kategorie": "ressource", "anteil": anteil, "n_fehlend": n_fehlend,
                "drift_art": "technisch" if status != "gruen" else "keine", "empfehlung": empf, "beleg": beleg}
    b = bereitschaft if isinstance(bereitschaft, dict) else {}
    if "n_fehlend" not in b:                                # MINOR-1: fehlender Kern-Key = unlesbar, NICHT 0/grün
        return _urteil("rot", None, None, "reparatur", "Bereitschafts-Report ohne n_fehlend (unlesbar)")
    try:
        n = int(b.get("n") or 0)
        n_fehlend = int(b.get("n_fehlend") or 0)
    except (TypeError, ValueError):
        return _urteil("rot", None, None, "reparatur", "Bereitschafts-Report unlesbar (leer ist NICHT grün)")
    if n <= 0:
        return _urteil("rot", None, n_fehlend, "reparatur",
                       "kein Bereitschafts-Report (0 Symbole — leer ist NICHT vollständig)")
    if n_fehlend < 0 or n_fehlend > n:                     # MINOR-1: implausibler Zähler = unlesbar (nie fail-open)
        return _urteil("rot", None, n_fehlend, "reparatur",
                       f"implausibler Lücken-Zähler ({n_fehlend}/{n}) — Report unlesbar")
    anteil = n_fehlend / n
    kern = (f"{n_fehlend}/{n} Outcome-EOD fehlen ({anteil*100:.1f}%) · bereit {int(b.get('n_bereit') or 0)} · "
            f"No-Data {int(b.get('n_leer') or 0)} · delistet-kurz {int(b.get('n_delistet_kurz') or 0)}")
    if anteil > rot_anteil:
        return _urteil("rot", anteil, n_fehlend, "reparatur",
                       f"{kern} — Load-Job/--daten-laden füllen (Rechnung sonst daten-unvollständig)")
    if anteil > gelb_anteil:
        return _urteil("gelb", anteil, n_fehlend, "ok", f"{kern} — spürbare Lücke, Load-Job nachziehen")
    return _urteil("gruen", anteil, n_fehlend, "ok", f"{kern} — Cache deckt das Universum")


def schreibe_datenluecken(conn, bereitschaft, t_ereignis, komponente="daten", metrik="datenluecken",
                          gelb_anteil=0.05, rot_anteil=0.20, cooldown_sek=0):
    """Datenlücken-Zustand in die ops-DB verankern (KEINE INSEL: Evaluator `datenluecken_pruefung` + der
    Standard-Writer `schreibe_gesundheit` + `projiziere_alert` — dieselbe Naht wie jede andere Aufsicht).
    `bereitschaft`: der `daten_bereitschaft`-Report des Rechenlaufs. Der volle Breakdown wandert in `beleg`
    (für die Modul-17-Kachel). -> das Evaluator-Urteil (inkl. push)."""
    u = datenluecken_pruefung(bereitschaft, gelb_anteil=gelb_anteil, rot_anteil=rot_anteil)
    b = bereitschaft if isinstance(bereitschaft, dict) else {}

    def _i(x):                                             # MAJOR-2: fehlertolerant coercen — der Writer darf NIE
        try:                                               # auf genau dem „unlesbar→rot"-Report werfen (sonst
            return int(x)                                  # erreicht das fail-closed-Urteil die DB nie).
        except (TypeError, ValueError):
            return None
    # Bei unlesbarem/leerem Report (anteil is None) KEINEN erfundenen Breakdown schreiben.
    ber = None if u["anteil"] is None else {
        "n": _i(b.get("n")), "n_bereit": _i(b.get("n_bereit")), "n_leer": _i(b.get("n_leer")),
        "n_delistet_kurz": _i(b.get("n_delistet_kurz")), "n_fehlend": _i(b.get("n_fehlend")),
        "prozent": b.get("prozent")}
    beleg = {"text": u["beleg"], "bereitschaft": ber}
    schreibe_gesundheit(conn, komponente, u["kategorie"], metrik, u["status"], t_ereignis,
                        wert_numerisch=(round(u["anteil"] * 100, 1) if u["anteil"] is not None else None),
                        schwelle=round(rot_anteil * 100, 1), drift_art=u["drift_art"],
                        empfehlung=u["empfehlung"], beleg=beleg)
    u["push"] = projiziere_alert(conn, komponente, metrik, u["status"], t_ereignis,
                                 cooldown_sek=cooldown_sek).get("push", False)
    return u


# ------------------------------------------------------------------ #
# §2 Alert-Lifecycle — Zustandsautomat (Dedup am ÜBERGANG; gelb de-eskaliert; Cooldown gegen Flapping)
# ------------------------------------------------------------------ #
def alert_uebergang(alt_zustand, status):
    """Reiner Zustandsautomat. alt_zustand ∈ _ALERT_ZUSTAENDE; status ∈ STATUS. -> (neuer_zustand, push:bool).
    rot: None|geheilt|beobachtend -> aktiv, PUSH (neuer/wiederkehrender/re-verschlechterter Ausfall);
    schon aktiv/quittiert -> Dedup, kein Push. gelb (QS-B2 De-Eskalation): NUR `aktiv` -> `beobachtend`
    (offen, aber runtergestuft, kein Push). **QS-MAJOR-2:** `quittiert` bleibt `quittiert` (ein gelb-Blip
    ist KEINE Heilung — er darf den Mensch-Ack NICHT aufheben; sonst re-pusht ein folgendes rot fälschlich).
    gruen: heilt (kein Push) — erst DAS hebt `quittiert` auf (F112: „bis er heilt + neu ausfällt").
    **Fail-closed (QS-m4):** unbekannter `status` -> ValueError (kein still verschlucktes rot durch Casing)."""
    if status not in STATUS:
        raise ValueError(f"unbekannter status {status!r} (erlaubt: {STATUS})")
    if status == "rot":
        if alt_zustand in (None, "geheilt", "beobachtend"):
            return ("aktiv", True)
        return (alt_zustand, False)
    if status == "gelb":
        if alt_zustand == "aktiv":
            return ("beobachtend", False)
        return (alt_zustand, False)           # quittiert/beobachtend/geheilt/None: gelb ändert nichts
    # gruen
    if alt_zustand in ("aktiv", "quittiert", "beobachtend"):
        return ("geheilt", False)
    return (alt_zustand, False)


# ------------------------------------------------------------------ #
# ops-DB: Schema + Schreiber/Leser
# ------------------------------------------------------------------ #
def schema_anlegen(conn):
    conn.executescript(SCHEMA_DDL)
    conn.commit()


def schreibe_gesundheit(conn, komponente, kategorie, metrik, status, t_ereignis, t_ingest=None,
                        wert_numerisch=None, schwelle=None, drift_art="keine", empfehlung="ok", beleg=None):
    """Ein append-only `system_gesundheit`-Ereignis schreiben (fail-closed auf ALLE Vokabulare, QS-m5)."""
    if (status not in STATUS or kategorie not in KATEGORIEN or drift_art not in DRIFT_ARTEN
            or empfehlung not in EMPFEHLUNGEN):
        raise ValueError(f"ungültiges Vokabular: status={status} kategorie={kategorie} "
                         f"drift_art={drift_art} empfehlung={empfehlung}")
    conn.execute(
        "INSERT INTO system_gesundheit(komponente,t_ereignis,t_ingest,kategorie,metrik,wert_numerisch,"
        "schwelle,status,drift_art,empfehlung,beleg) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (komponente, t_ereignis, t_ingest or t_ereignis, kategorie, metrik, wert_numerisch, schwelle,
         status, drift_art, empfehlung, json.dumps(beleg, ensure_ascii=False) if beleg is not None else None))
    conn.commit()


def _alert_zustand(conn, komponente, metrik):
    row = conn.execute("SELECT zustand, letzte_aenderung FROM alert_zustand WHERE komponente=? AND metrik=?",
                       (komponente, metrik)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def projiziere_alert(conn, komponente, metrik, status, jetzt, cooldown_sek=0):
    """Wendet den Zustandsautomaten auf die Projektion `alert_zustand` an + gibt zurück, ob ein Push feuert.
    **QS-m4 fail-closed** auf `status`. **QS-m3:** `seit` wird beim echten Zustandswechsel MIT gesetzt (nicht
    auf dem Erst-Insert eingefroren). **QS-M5 Flapping-Cooldown:** ein `geheilt→rot`-Re-Push wird unterdrückt,
    wenn der vorige (geheilt-)Zustand jünger als `cooldown_sek` ist (oszillierende Quelle -> kein Push-Spam;
    der Roh-Strom system_gesundheit bekommt das Ereignis trotzdem). -> {'zustand','push'}."""
    if status not in STATUS:
        raise ValueError(f"unbekannter status {status!r}")
    alt, alt_seit = _alert_zustand(conn, komponente, metrik)   # alt_seit = letzte_aenderung; =seit, solange
    neu, push = alert_uebergang(alt, status)                    # kein Intra-Zustand-Update existiert (Claude-Anm.)
    flapping = False
    if push and cooldown_sek and alt == "geheilt" and alt_seit is not None:
        seit_alter = _sekunden_zwischen(alt_seit, jetzt)
        if seit_alter is not None and seit_alter < cooldown_sek:
            # QS-MINOR-3: Flap innerhalb Cooldown -> Push unterdrücken UND im `geheilt`-Zustand BLEIBEN
            # (nicht auf aktiv vorrücken). Bleibt die Quelle rot, pusht der nächste Tick nach Cooldown-Ablauf
            # DOCH (kein permanenter Push-Verlust); der Roh-Strom system_gesundheit trägt jedes Ereignis.
            push, flapping, neu = False, True, alt
    if neu != alt:
        conn.execute(
            "INSERT INTO alert_zustand(komponente,metrik,zustand,seit,letzte_aenderung) VALUES(?,?,?,?,?) "
            "ON CONFLICT(komponente,metrik) DO UPDATE SET zustand=excluded.zustand, "
            "seit=excluded.seit, letzte_aenderung=excluded.letzte_aenderung",
            (komponente, metrik, neu, jetzt, jetzt))
        conn.commit()
    # QS-B4: `flapping_unterdrueckt` macht im Aufrufer-Log unterscheidbar, ob Entwarnung vorlag ODER der
    # Flapping-Filter griff (ein re-eskaliertes rot ist trotz push=False weiter `aktiv`, kein „geheilt").
    return {"zustand": neu, "push": push, "flapping_unterdrueckt": flapping}


def quittiere_alert(conn, komponente, metrik, jetzt):
    """Mensch-Ack (via Modul 17): einen aktiven/beobachtenden Alert auf `quittiert` setzen (kein Push mehr,
    bis er heilt + neu ausfällt). Schließt das `quittiert`-Vokabular (QS-m2)."""
    conn.execute(
        "UPDATE alert_zustand SET zustand='quittiert', letzte_aenderung=? "
        "WHERE komponente=? AND metrik=? AND zustand IN ('aktiv','beobachtend')",
        (jetzt, komponente, metrik))
    conn.commit()


def setze_monitor_heartbeat(conn, loop, tick, status="gruen"):
    """Der Wächter schreibt seinen eigenen Tick (Dead-Man's-Switch-Quelle)."""
    conn.execute(
        "INSERT INTO monitor_heartbeat(loop,letzter_tick,status) VALUES(?,?,?) "
        "ON CONFLICT(loop) DO UPDATE SET letzter_tick=excluded.letzter_tick, status=excluded.status",
        (loop, tick, status))
    conn.commit()


def lies_monitor_heartbeats(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT loop, letzter_tick FROM monitor_heartbeat").fetchall()}


# ------------------------------------------------------------------ #
# Steuer-Audit (Modul-17-Feinkonzept F126): WER/WANN schaltete Scraper/Quelle/Kadenz — beeinflusst jede
# spätere Kalibrierung, also nachvollziehbar (append-only).
# ------------------------------------------------------------------ #
def schreibe_steuer_audit(conn, akteur, aktion, ziel, wert, jetzt):
    """Eine Steuer-Aktion protokollieren (Kill/Quelle-an-aus/Kadenz/Routing). append-only."""
    conn.execute("INSERT INTO steuer_audit(t,akteur,aktion,ziel,wert) VALUES(?,?,?,?,?)",
                 (jetzt, akteur, aktion, ziel, None if wert is None else str(wert)))
    conn.commit()


def lies_steuer_audit(conn, limit=100):
    """-> [{'t','akteur','aktion','ziel','wert'}] jüngste zuerst (für die Steuerungs-Ansicht)."""
    return [{"t": t, "akteur": a, "aktion": ak, "ziel": z, "wert": w}
            for t, a, ak, z, w in conn.execute(
                "SELECT t,akteur,aktion,ziel,wert FROM steuer_audit ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()]


# ------------------------------------------------------------------ #
# F128 — Prozess-Status (Fortschritt + Herzschlag), F131 — Control-Plane (Wunsch), F133 — Lauf-Diagnose
# Die Prozesse MELDEN ihren Fortschritt hierher (Reporter) und LESEN ihren Wunsch (Poller); Modul 17
# projiziert (rechnet nichts). „Stille ≠ Grün": ein erwarteter Prozess ohne Zeile / mit veraltetem
# Herzschlag wird `tot`, nie stumm gesund.
# ------------------------------------------------------------------ #
def melde_status(conn, prozess, jetzt, phase=None, aktuell=None, gesamt=None, gestartet=None,
                 zustand="läuft", beleg=None):
    """Ein Prozess meldet seinen Fortschritt (Upsert je `prozess`). `jetzt` = Herzschlag + `aktualisiert`.
    `gestartet` wird beim ERSTEN Melden auf `jetzt` gesetzt und danach BEIBEHALTEN — AUSSER bei einem
    Phasen-Wechsel (Fable-M5: sonst ETA-Müll aus der Vorphase) oder wenn explizit übergeben. `zustand` ∈
    PROZESS_ZUSTAENDE (fail-closed). ETA wird NICHT gespeichert (mutables Derivat), sondern im Leser berechnet."""
    if zustand not in PROZESS_ZUSTAENDE:
        raise ValueError(f"unbekannter zustand {zustand!r} (erlaubt: {PROZESS_ZUSTAENDE})")
    alt = conn.execute("SELECT phase, gestartet FROM prozess_status WHERE prozess=?", (prozess,)).fetchone()
    if gestartet is None:
        if alt is None:
            gestartet = jetzt                              # erster Kontakt
        elif phase is not None and phase != alt[0]:
            gestartet = jetzt                              # Phasen-Wechsel -> Uhr neu (Fable-M5)
        else:
            gestartet = alt[1] or jetzt                    # laufende Phase: Startzeit beibehalten
    conn.execute(
        "INSERT INTO prozess_status(prozess,phase,aktuell,gesamt,gestartet,aktualisiert,herzschlag,zustand,beleg) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(prozess) DO UPDATE SET phase=excluded.phase, "
        "aktuell=excluded.aktuell, gesamt=excluded.gesamt, gestartet=excluded.gestartet, "
        "aktualisiert=excluded.aktualisiert, herzschlag=excluded.herzschlag, zustand=excluded.zustand, "
        "beleg=excluded.beleg",
        (prozess, phase, aktuell, gesamt, gestartet, jetzt, jetzt, zustand,
         json.dumps(beleg, ensure_ascii=False) if beleg is not None else None))
    conn.commit()


def _eta_sekunden(aktuell, gesamt, gestartet, aktualisiert):
    """ETA-Sekunden aus dem Fortschritt (Fable-M5 fail-closed): nur bei belastbarem, positivem, nicht-über-
    Ziel-Fortschritt UND ≥1s verstrichener Zeit; sonst None (nie erfunden, nie negativ). Kein now()."""
    if aktuell is None or gesamt is None or not gestartet or not aktualisiert:
        return None
    try:
        a, g = float(aktuell), float(gesamt)
    except (TypeError, ValueError):
        return None
    if a <= 0 or g <= 0 or a > g:
        return None
    verstrichen = _sekunden_zwischen(gestartet, aktualisiert)
    if verstrichen is None or verstrichen < 1.0:
        return None
    rate = a / verstrichen                                 # Einheiten pro Sekunde
    if rate <= 0:
        return None
    return max(0.0, (g - a) / rate)


def lies_prozess_status(conn, jetzt, tot_ab_sek, erwartete):
    """-> [{'prozess','phase','aktuell','gesamt','anteil','zustand','angezeigt','ampel','eta_sek','alter_sek',
    'beleg'}] über die PFLICHT-Menge `erwartete` (Fable-B2: ein erwarteter Prozess ohne Zeile ist NICHT
    unsichtbar, sondern `tot`/„nie gemeldet"). `angezeigt` = der effektive Zustand nach Liveness-Prüfung:
    ein LEBENDER Prozess (läuft/pausiert) mit Herzschlag älter als `tot_ab_sek` (oder unlesbar) wird `tot`;
    Terminal-Zustände (gestoppt/fertig/ruht) bleiben (kein Falsch-Rot, Fable-m2). Ampel: tot/fehler→rot,
    pausiert/gestoppt/ruht→gelb, läuft-frisch→gruen, fertig→gruen. „Stille ≠ Grün": None/unlesbar → nie grün."""
    zeilen = {r[0]: r for r in conn.execute(
        "SELECT prozess,phase,aktuell,gesamt,gestartet,aktualisiert,herzschlag,zustand,beleg "
        "FROM prozess_status").fetchall()}
    out = []
    for prozess in sorted(set(erwartete) | set(zeilen)):
        r = zeilen.get(prozess)
        if r is None:
            out.append({"prozess": prozess, "phase": None, "aktuell": None, "gesamt": None, "anteil": None,
                        "zustand": None, "angezeigt": "tot", "ampel": "rot", "eta_sek": None,
                        "alter_sek": None, "beleg": "nie gemeldet (Prozess nie gestartet / vor dem ersten "
                        "Herzschlag gecrasht)"})
            continue
        _p, phase, aktuell, gesamt, gestartet, aktualisiert, herzschlag, zustand, beleg = r
        alter = _sekunden_zwischen(herzschlag, jetzt)
        angezeigt = zustand
        if zustand in _LEBEND_ZUSTAENDE:
            if alter is None or alter > tot_ab_sek or alter < -_SKEW_TOLERANZ_SEK:
                angezeigt = "tot"                          # Herzschlag veraltet/unlesbar/zukünftig -> tot
        anteil = None
        try:
            if aktuell is not None and gesamt not in (None, 0):
                anteil = max(0.0, min(1.0, float(aktuell) / float(gesamt)))
        except (TypeError, ValueError, ZeroDivisionError):
            anteil = None
        try:
            beleg = json.loads(beleg) if beleg is not None else None
        except (ValueError, TypeError):
            pass
        out.append({"prozess": prozess, "phase": phase, "aktuell": aktuell, "gesamt": gesamt,
                    "anteil": anteil, "zustand": zustand, "angezeigt": angezeigt,
                    "ampel": _prozess_ampel(angezeigt), "alter_sek": alter,
                    "eta_sek": _eta_sekunden(aktuell, gesamt, gestartet, aktualisiert), "beleg": beleg})
    return out


def _prozess_ampel(angezeigt):
    if angezeigt in ("tot", "fehler"):
        return "rot"
    if angezeigt in ("pausiert", "gestoppt", "ruht"):
        return "gelb"
    if angezeigt in ("läuft", "fertig"):
        return "gruen"
    return "gelb"                                          # unbekannt/None -> unbestimmt, nie grün


def setze_steuerung(conn, prozess, gewuenscht, gesetzt_von, jetzt):
    """Der Button-Schreiber (F132): den Wunschzustand eines Prozesses setzen + im Steuer-Audit protokollieren
    (F126, WER/WANN). `gewuenscht` ∈ STEUER_WUNSCH (fail-closed)."""
    if gewuenscht not in STEUER_WUNSCH:
        raise ValueError(f"unbekannter Steuer-Wunsch {gewuenscht!r} (erlaubt: {STEUER_WUNSCH})")
    conn.execute(
        "INSERT INTO prozess_steuerung(prozess,gewuenscht,gesetzt_von,gesetzt_am) VALUES(?,?,?,?) "
        "ON CONFLICT(prozess) DO UPDATE SET gewuenscht=excluded.gewuenscht, "
        "gesetzt_von=excluded.gesetzt_von, gesetzt_am=excluded.gesetzt_am",
        (prozess, gewuenscht, gesetzt_von, jetzt))
    schreibe_steuer_audit(conn, gesetzt_von, "steuern", prozess, gewuenscht, jetzt)
    conn.commit()


def lies_steuerung(conn, prozess):
    """Der Poller (F131): den Wunschzustand eines Prozesses lesen. Kein Eintrag -> 'run' (Default: laufen)."""
    row = conn.execute("SELECT gewuenscht FROM prozess_steuerung WHERE prozess=?", (prozess,)).fetchone()
    w = row[0] if row else None
    return w if w in STEUER_WUNSCH else "run"


def lies_alle_steuerung(conn):
    """-> {prozess: gewuenscht} (für die UI-Projektion: Wunsch neben dem Ist)."""
    return {p: (w if w in STEUER_WUNSCH else "run")
            for p, w in conn.execute("SELECT prozess, gewuenscht FROM prozess_steuerung").fetchall()}


def lies_steuerung_detail(conn, prozess):
    """-> (gewuenscht, gesetzt_am) für die Kadenz-/Resume-Logik (ein frischer `run`-Wunsch = Sofort-Start).
    Kein Eintrag -> ('run', None)."""
    row = conn.execute("SELECT gewuenscht, gesetzt_am FROM prozess_steuerung WHERE prozess=?",
                       (prozess,)).fetchone()
    if not row:
        return "run", None
    return (row[0] if row[0] in STEUER_WUNSCH else "run"), row[1]


def status_haken(ops_db, prozess, control_fn=None, jetzt_fn=None, reset_wunsch=False):
    """Convenience für Batch-Runner (retro_heimkorpus, markt_db_aufbau, …): bindet EINEN Lauf an die
    Control-Plane — EINE Definition dieser Naht (kein Doppel in jedem Runner). Öffnet die ops-DB per Pfad.
    -> (melde, wunsch):
      - melde(phase, aktuell, gesamt, zustand, beleg): schreibt `prozess_status` (Modul-17-Prozess-Board:
        Balken + ETA) — der Fortschritt wird sichtbar/überwachbar, nicht nur Console (Governing Guardrail).
      - wunsch(): liest den Steuer-Wunsch ('run'/'pause'/'stop') aus der Control-Plane (Button in Modul 17);
        `control_fn` überschreibt (Test/Injektion).
    FAIL-SAFE: ein ops-DB-Fehler kippt den Rechen-Lauf NIE (die Transparenz-Schicht ist Beobachter, kein Gate).
    Ohne ops_db + control_fn: No-op-melde + immer 'run' (reiner Console-Lauf bleibt möglich).

    **Lebensdauer der Connection (Claude-QS MINOR-2, terminiert):** die geöffnete ops-DB-Connection lebt in den
    Closures und wird beim Unerreichbar-Werden per Refcount geschlossen — für die heutigen MANUELLEN Einmal-
    Läufe (retro_heimkorpus/markt_db_aufbau) genügt das. Ruft ein künftiger IN-PROCESS-`betrieb_supervisor`
    (Schicht O, Dauerschleife) diese Runner je Tick im selben Prozess, braucht `status_haken` einen expliziten
    Closer (Auslöser: die in-process-Verdrahtung der Runner in den Supervisor). Bis dahin bewusst schlank.
    **Stehender Steuer-Wunsch (Claude-QS MINOR-5):** ein aus einem VORIGEN Lauf verbliebenes 'pause'/'stop' in
    `prozess_steuerung` bricht einen frischen Lauf sofort ab, bis im Cockpit wieder 'run' gesetzt ist (geteiltes
    Verhalten aller Runner — das Prozess-Board zeigt den Wunsch neben dem Ist, ist also sichtbar)."""
    import datetime as _dt
    jetzt_fn = jetzt_fn or (lambda: _dt.datetime.now().replace(microsecond=0).isoformat())
    conn = None
    if ops_db:
        try:
            conn = sqlite3.connect(ops_db)
            schema_anlegen(conn)
        except Exception:                                        # noqa: BLE001 — ohne ops-DB weiter (fail-safe)
            conn = None
    if conn is not None and reset_wunsch:
        # MINOR-5-Fix für MANUELLE On-Demand-Läufe: einen aus einem VORIGEN Lauf verbliebenen 'pause'/'stop'
        # beim Start auf 'run' zurücksetzen, damit ein frischer CLI-Lauf nicht sofort abbricht. Stop wirkt so
        # PRO Lauf (stoppt den aktuellen), ein neuer Lauf startet sauber — das richtige Modell für On-Demand.
        try:
            setze_steuerung(conn, prozess, "run", "system", jetzt_fn())
        except Exception:                                        # noqa: BLE001 — fail-safe
            pass

    def melde(phase=None, aktuell=None, gesamt=None, zustand="läuft", beleg=None):
        if conn is None:
            return
        try:
            melde_status(conn, prozess, jetzt_fn(), phase=phase, aktuell=aktuell, gesamt=gesamt,
                         zustand=zustand, beleg=beleg)
        except Exception:                                        # noqa: BLE001 — Melde-Fehler kippt den Lauf nie
            pass

    def wunsch():
        if control_fn is not None:
            try:
                return control_fn()
            except Exception:                                    # noqa: BLE001
                return "run"
        if conn is None:
            return "run"
        try:
            return lies_steuerung(conn, prozess)
        except Exception:                                        # noqa: BLE001
            return "run"

    return melde, wunsch


def schreibe_lauf_diagnose(conn, jetzt, kennz):
    """F133: die Kennzahlen EINES Bewertungs-Laufs append-only ablegen (ops-DB = Heimat append-only, Fable-M8;
    NICHT der DROP+CREATE-Cockpit-Store). `kennz` = dict (n_fakten/n_kategorien/n_bewertung/n_konjunktion/
    n_gap/n_signal/n_forward/bewertung_diag/…). Jüngste zählt; die Historie trägt `t` (Staleness, Fable-M9)."""
    conn.execute("INSERT INTO lauf_diagnose(t,kennz) VALUES(?,?)",
                 (jetzt, json.dumps(kennz, ensure_ascii=False)))
    conn.commit()


def lies_lauf_diagnose(conn):
    """-> (t, kennz_dict) des JÜNGSTEN Laufs, oder (None, None) (fail-closed, nie erfunden)."""
    row = conn.execute("SELECT t, kennz FROM lauf_diagnose ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None, None
    try:
        return row[0], json.loads(row[1]) if row[1] else None
    except (ValueError, TypeError):
        return row[0], None


def schreibe_vorwaerts_verdikt(conn, jetzt, power, gereift):
    """F134/Fable-M6: der persistierte `betrieb_vorwaerts.auswerten`-Ausgang (Power-Verdikt + gereift-Zählung)
    — die Datenquelle der letzten zwei Alpha-Funnel-Stufen (sonst zeigen sie NIE einen Wert). append-only."""
    conn.execute("INSERT INTO vorwaerts_verdikt(t,power,gereift) VALUES(?,?,?)",
                 (jetzt, json.dumps(power, ensure_ascii=False) if power is not None else None,
                  json.dumps(gereift, ensure_ascii=False) if gereift is not None else None))
    conn.commit()


def lies_vorwaerts_verdikt(conn):
    """-> (power_dict, gereift_dict) des JÜNGSTEN Auswerte-Laufs, oder (None, None) (fail-closed)."""
    row = conn.execute("SELECT power, gereift FROM vorwaerts_verdikt ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None, None
    def _j(x):
        try:
            return json.loads(x) if x else None
        except (ValueError, TypeError):
            return None
    return _j(row[0]), _j(row[1])


# ------------------------------------------------------------------ #
# Leser für die Projektion (Modul 17 GUI ruft NUR diese — keine Query-Insel im Assembler, F119)
# ------------------------------------------------------------------ #
def lies_gesundheit_aktuell(conn):
    """Letzter `system_gesundheit`-Zustand JE (komponente, metrik) aus dem append-only Strom (MAX(id) =
    jüngstes Ereignis). -> [{'komponente','metrik','status','kategorie','drift_art','empfehlung','beleg',
    't_ereignis'}] (beleg aus dem JSON dekodiert). Die GUI projiziert das 1:1 — sie fällt kein neues Urteil."""
    rows = conn.execute(
        "SELECT g.komponente, g.metrik, g.status, g.kategorie, g.drift_art, g.empfehlung, g.beleg, g.t_ereignis "
        "FROM system_gesundheit g JOIN (SELECT komponente, metrik, MAX(id) mid FROM system_gesundheit "
        "GROUP BY komponente, metrik) m ON g.id = m.mid ORDER BY g.komponente, g.metrik").fetchall()
    out = []
    for k, me, st, kat, drift, empf, beleg, tev in rows:
        try:
            beleg = json.loads(beleg) if beleg is not None else None
        except (ValueError, TypeError):
            pass                                            # roher Text bleibt roh (fail-safe)
        out.append({"komponente": k, "metrik": me, "status": st, "kategorie": kat, "drift_art": drift,
                    "empfehlung": empf, "beleg": beleg, "t_ereignis": tev})
    return out


def lies_alert_zustaende(conn):
    """Alle `alert_zustand`-Zeilen (offene/quittierte/geheilte Alerts). -> [{'komponente','metrik','zustand',
    'seit','letzte_aenderung'}]. Der GUI-Leser für die Alert-Liste + den Quittieren-Knopf (F4/F121)."""
    return [{"komponente": k, "metrik": me, "zustand": z, "seit": s, "letzte_aenderung": la}
            for k, me, z, s, la in conn.execute(
                "SELECT komponente, metrik, zustand, seit, letzte_aenderung FROM alert_zustand "
                "ORDER BY komponente, metrik").fetchall()]


def gesamt_ampel(conn):
    """Der Worst-of-Rollup über alle aktuellen `system_gesundheit`-Zustände — das AGGREGIERTE Alarm-Urteil
    gehört zu Schicht S (§11), NICHT ins UI (Claude-F6). rot vor gelb vor gruen; **keine Zeile -> `gelb`
    (unbestimmt/Kaltstart, NIE stumm grün — Stille ≠ Grün)**. -> STATUS-String."""
    stati = {r["status"] for r in lies_gesundheit_aktuell(conn)}
    if "rot" in stati:
        return "rot"
    if "gelb" in stati:
        return "gelb"
    if stati == {"gruen"}:
        return "gruen"
    return "gelb"                                           # leer/unbekannt -> unbestimmt, nie grün
