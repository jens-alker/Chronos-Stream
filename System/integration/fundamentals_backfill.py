"""
fundamentals_backfill.py — die Fundamentaldaten der bereits KATEGORISIERTEN, aber noch NICHT gecachten
Symbole über ALLE Regionen (US + EU + Asien) nachziehen (Jens 29.07.: „es fehlen noch Fundamentaldaten für
die ersten ~9000 Unternehmen USA" + „Europa und Asien nicht vergessen, auch alles vorhanden").

**Der Befund (Coverage-Voll-Restore):** EU (12 340) und Asien (4 850) sind **100 % gecacht**; NUR die
US-Map (`retro_kat_map_gic_breit.json`, 12 079) hat die Lücke — **2 425 gecacht (20 %), ~9 654 fehlen**. Die
frühen US-Symbole wurden vor dem Voll-Dump-Cache-Schema klassifiziert, also existiert die Kategorie, aber der
Fundamentals-Dump (Cash_Flow quarterly + Shares + Kennzahlen) fehlt — ohne ihn hat die Analyse-Schicht für
diese Namen keine vollständigen Fundamentaldaten. Der Backfill läuft **region-vollständig** (US+EU+Asien) —
offen ist faktisch US, aber künftige EU/Asien-Löcher werden mitgezogen.

**KEINE INSEL:** dies baut KEINE zweite Fundamentals-Beschaffung. Es nutzt exakt die geprüfte Naht —
`_fetch_full_fundamentals` (der robuste Voll-Dump-Fetcher mit Tageslimit-Abbruch/Backoff/No-Data-Marker aus
`retro_kat_map_breit`) hinter `fundamentals_cache.hole` (cache-first, geteilt mit Klassifikation UND Modul 9).
Ein hier gezogenes Symbol kostet Modul 9 KEINEN zweiten Call. Der Backfill ist nur der **Treiber**, der den
gemappten-aber-nicht-gecachten Rest durch dieselbe Naht schiebt.

**Betrieb (wie der Klassifikations-Grind):** gedeckelt je Lauf, wiederaufnehmbar (überspringt `ist_gecacht`),
fail-fast bei Tageslimit (`TageslimitErreicht` → sauber abbrechen statt durch alle Rest-Symbole zu spinnen),
Drive-persistent (Restore vor dem Lauf + Sync-Callback in der Batch-Kadenz). Über die Tage füllt der
EOD-Scheduler so den ganzen Rückstand — genau wie der Klassifikations-Batch selbst.

Reiner Kern (`zu_backfillende_symbole`) offline testbar; der Fetch-Loop ist an `$EODHD_API_KEY` gated.
Nur Standardbibliothek.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = os.path.dirname(_HERE)
for _p in (os.path.join(_SYS, "connectors"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ------------------------------------------------------------------ #
# Reiner Kern (offline testbar)
# ------------------------------------------------------------------ #
def zu_backfillende_symbole(kat_map, ist_gecacht_fn):
    """REIN: die Symbole aus der Kategorie-Map, für die NOCH KEIN Fundamentals-Dump gecacht ist.
    `kat_map`: {symbol_id: kategorie} (die geladene Map). `ist_gecacht_fn(symbol_id) -> bool` (i. d. R.
    `fundamentals_cache.ist_gecacht`). -> sortierte Liste der offenen Symbol-IDs (deterministisch =
    reproduzierbare Batch-Reihenfolge über Läufe; ein bereits gecachtes (auch No-Data-{}) Symbol wird NIE
    erneut gezogen). Ein leerer/kaputter Map-Eintrag (kein Symbol) wird still übersprungen."""
    return sorted(sid for sid in kat_map if sid and not ist_gecacht_fn(sid))


def lade_kat_map(pfad):
    """Eine Regions-Klassifikations-Map laden -> {symbol_id: kategorie}. Erwartet das `{"map": {...}, ...}`-
    Format (retro_kat_map_gic_*.json). Fehlt die Datei/der `map`-Schlüssel -> {} (leerer Rückstand)."""
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    m = d.get("map") if isinstance(d, dict) else None
    return m if isinstance(m, dict) else {}


def region_pfade():
    """Die drei survivorship-freien Regions-Maps (US + EU + Asien) — Jens 29.07.: „Europa und Asien nicht
    vergessen, auch alles vorhanden". Symbol-IDs sind börsen-suffigiert (.US/.LSE/.TSE…) → kollisionsfrei
    über Regionen. -> {region: pfad}."""
    from retro_kat_map_breit import _GIC_BREIT_PFAD, _GIC_EU_PFAD, _GIC_ASIA_PFAD
    return {"us": _GIC_BREIT_PFAD, "eu": _GIC_EU_PFAD, "asien": _GIC_ASIA_PFAD}


def lade_universum(regionen=None):
    """Die gemappten Symbole ALLER (oder gewählter) Regionen zu EINER Map vereinigen. `regionen`: Teilmenge
    von {'us','eu','asien'} (None = alle drei). Fehlende Regions-Dateien werden still übersprungen (leer).
    -> {symbol_id: kategorie} über alle Regionen (börsen-suffigiert → keine Kollision)."""
    pfade = region_pfade()
    gewaehlt = regionen or list(pfade)
    gesamt = {}
    for r in gewaehlt:
        gesamt.update(lade_kat_map(pfade[r]))
    return gesamt


# ------------------------------------------------------------------ #
# Fetch-Loop (gated) — gedeckelt, wiederaufnehmbar, fail-fast bei Tageslimit
# ------------------------------------------------------------------ #
def backfill(symbole, hole_fn, ist_gecacht_fn, tageslimit_exc, max_pro_lauf=None,
             batch_groesse=50, nach_batch=None, control_fn=None, melde_fn=None):
    """Den offenen Rückstand durch die geteilte Cache-Naht schieben. `symbole`: offene Symbol-IDs (aus
    `zu_backfillende_symbole`). `hole_fn(symbol_id)`: zieht+cacht den Voll-Dump (i. d. R. eine Lambda über
    `fundamentals_cache.hole(sid, _fetch_full_fundamentals)`); DARF `tageslimit_exc` werfen (Quota) ODER
    einen transienten Fehler. `ist_gecacht_fn`: Doppel-Skip (falls Restore zwischenzeitlich griff).
    `tageslimit_exc`: die Exception-Klasse für „Tageslimit erschöpft" → fail-fast (sauberer Abbruch, KEIN
    Weiter-Spinnen). `max_pro_lauf`: Deckel je Lauf (None = kein Deckel; der Tageslimit-Abbruch deckelt
    ohnehin). `batch_groesse`: nach je N erfolgreichen Zügen `nach_batch()` (Drive-Sync). `nach_batch`:
    optionaler Callback (z. B. Drive-Sync).
    -> Bericht-dict {n_offen_start, n_gezogen, n_no_data, n_transient, tageslimit_erreicht, n_rest}."""
    offen_start = len(symbole)
    gezogen = no_data = transient = seit_sync = 0
    tageslimit = False
    verarbeitet = 0
    abgebrochen = False
    for sid in symbole:
        if max_pro_lauf is not None and verarbeitet >= max_pro_lauf:
            break
        # F131 (Fable-M10): verlustfreier Abbruch per Control-Plane — der Cache IST der Zustand (idempotent,
        # der nächste Lauf macht bei den noch-nicht-gecachten weiter), also ist ein Break jederzeit verlustfrei.
        if control_fn is not None and control_fn() != "run":
            abgebrochen = True
            break
        if melde_fn is not None and verarbeitet and verarbeitet % batch_groesse == 0:
            melde_fn(phase="backfill", aktuell=verarbeitet, gesamt=offen_start)
        if ist_gecacht_fn(sid):                          # Restore hat es zwischenzeitlich geliefert
            continue
        try:
            data = hole_fn(sid)
        except tageslimit_exc:
            tageslimit = True
            break                                        # Quota erschöpft → sauber abbrechen (Scheduler nimmt wieder auf)
        except Exception:                                # noqa: BLE001 — transient/Auth/Netz → nicht abhaken, retry nächster Lauf
            transient += 1
            continue
        verarbeitet += 1
        # {} = No-Data-Marker (Symbol ohne Fundamentaldatensatz, häufig delistet) — gecacht, nie wieder abgerufen.
        if isinstance(data, dict) and not data:
            no_data += 1
        else:
            gezogen += 1
        seit_sync += 1
        if nach_batch and seit_sync >= batch_groesse:
            nach_batch()
            seit_sync = 0
    if nach_batch and seit_sync:
        nach_batch()                                     # Schluss-Sync (auch nach Tageslimit-Abbruch)
    # Rest: alles, was jetzt noch nicht gecacht ist (deterministisch nachzählbar).
    rest = sum(1 for sid in symbole if not ist_gecacht_fn(sid))
    return {
        "n_offen_start": offen_start,
        "n_gezogen": gezogen,
        "n_no_data": no_data,
        "n_transient": transient,
        "tageslimit_erreicht": tageslimit,
        "abgebrochen": abgebrochen,
        "n_rest": rest,
    }


def lauf(pfad=None, api_token=None, max_pro_lauf=None, batch_groesse=50, drive=True, regionen=None,
         control_fn=None, melde_fn=None):
    """LIVE (gated an $EODHD_API_KEY): den Fundamentals-Rückstand über ALLE Regionen (US + EU + Asien)
    nachziehen. Restauriert den Cache aus der Drive-DB (damit bereits gezogene Symbole nicht doppelt Quota
    kosten), zieht den offenen Rest durch die geteilte `fundamentals_cache`-Naht (Modul-9-tauglich), synct in
    der Batch-Kadenz nach Drive. `pfad`: EINE Map explizit (überschreibt `regionen`, für gezielte/Test-Läufe);
    sonst `regionen` (Teilmenge {'us','eu','asien'}, None = alle drei). EU/Asien sind aktuell 100 % gecacht →
    der offene Rest ist faktisch US, aber der Lauf ist region-vollständig (künftige Löcher werden mitgezogen).
    -> Bericht-dict."""
    import fundamentals_cache
    from retro_kat_map_breit import _fetch_full_fundamentals, TageslimitErreicht

    kat_map = lade_kat_map(pfad) if pfad else lade_universum(regionen)

    # Drive: Restore vor dem Lauf (bereits auf Drive gesicherte Dumps nicht neu ziehen) + Sync-Callback.
    nach_batch = None
    if drive:
        try:
            import fundamentals_drive
            import gdrive
            st = gdrive.preflight()
            if st.get("ok"):
                n = fundamentals_drive.sync_restore(gdrive.access_token())
                print(f"  💾 Restore: {n} Buckets aus der Drive-DB in den lokalen Cache geholt.")

                def nach_batch():
                    try:
                        m = fundamentals_drive.sync_hoch(gdrive.access_token())
                        if m:
                            print(f"  💾 Drive-Sync: {m} Bucket(s) aktualisiert.")
                    except Exception as e:               # noqa: BLE001 — Sync-Fehler kippt den Backfill nie
                        print(f"  ⚠ Drive-Sync übersprungen: {e}")
            else:
                fehler = st.get("fehler") or ""
                if "Fehlende OAuth-Credentials" not in fehler:
                    print(f"  ⚠ Drive AUS (Preflight '{st.get('schritt')}'): {fehler}")
        except Exception as e:                            # noqa: BLE001
            print(f"  ⚠ Drive-Setup übersprungen: {e}")

    offen = zu_backfillende_symbole(kat_map, fundamentals_cache.ist_gecacht)
    print(f"  Universum (US+EU+Asien): {len(kat_map)} gemappt | offen (nicht gecacht): {len(offen)}"
          f"{f' | Deckel/Lauf: {max_pro_lauf}' if max_pro_lauf else ''}")

    def hole_fn(sid):
        return fundamentals_cache.hole(
            sid, lambda s: _fetch_full_fundamentals(s, api_token=api_token))

    bericht = backfill(offen, hole_fn, fundamentals_cache.ist_gecacht, TageslimitErreicht,
                       max_pro_lauf=max_pro_lauf, batch_groesse=batch_groesse, nach_batch=nach_batch,
                       control_fn=control_fn, melde_fn=melde_fn)
    print(f"  gezogen: {bericht['n_gezogen']} | No-Data: {bericht['n_no_data']} | "
          f"transient: {bericht['n_transient']} | Rest: {bericht['n_rest']}")
    if bericht["tageslimit_erreicht"]:
        print("  ⏸  EODHD-TAGESLIMIT erreicht → sauber abgebrochen. Rest nach Quota-Reset (Scheduler).")
    elif bericht["n_rest"] == 0:
        print("  ✅ Fundamentals-Rückstand vollständig gecacht (n_rest=0).")
    return bericht


def main():
    print("=== Fundamentals-Backfill (gemappte-aber-nicht-gecachte Symbole, US + EU + Asien) ===")
    if "--lauf" in sys.argv and os.environ.get("EODHD_API_KEY"):
        max_pro = None
        drive = "--no-drive" not in sys.argv
        regionen = None
        for a in sys.argv:
            if a.startswith("--max="):
                max_pro = int(a.split("=", 1)[1])
            elif a.startswith("--region="):
                regionen = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
        lauf(max_pro_lauf=max_pro, drive=drive, regionen=regionen)
    else:
        grund = "kein --lauf" if "--lauf" not in sys.argv else "kein $EODHD_API_KEY"
        print(f"  Kein Live-Lauf ({grund}). Reiner Kern offline getestet (test_fundamentals_backfill.py).")
        # Diagnose je Region: Rückstand ohne Abruf zählen (nur Map + lokaler Cache-Bestand).
        try:
            import fundamentals_cache
            for r, pf in region_pfade().items():
                kat_map = lade_kat_map(pf)
                offen = zu_backfillende_symbole(kat_map, fundamentals_cache.ist_gecacht)
                print(f"  [{r:5}] {len(kat_map):6} gemappt, {len(offen):6} noch nicht lokal gecacht.")
        except Exception as e:                            # noqa: BLE001
            print(f"  (Diagnose übersprungen: {e})")


if __name__ == "__main__":
    main()
