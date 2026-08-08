#!/bin/bash
# watchdog_batch.sh — autonome Überwachung des EOD-Grinds. Zwei Phasen, EINE geteilte EODHD-Tagesquota:
#   PHASE 1 (Priorität): Fundamentals-Backfill der gemappten-aber-nicht-gecachten US-Symbole
#     (Jens 29.07.: „es fehlen noch Fundamentaldaten für die ersten ~9000 Unternehmen USA"). Entsperrt
#     die nachgelagerte Analyse. Läuft ZUERST, bis der US-Rückstand geleert ist (n_rest=0).
#   PHASE 2: der Klassifikations-Voll-Batch (Mehr-Kategorien-Power gegen F86) mit der Rest-Quota.
# Beide teilen die Quota und brechen bei Tageslimit sauber ab → sequenziell, kein Verschnitt. Der Cache/
# die Map sind git- bzw. Drive-persistent → reclaim-fest; der Grind nimmt nach Reset wieder auf.
# Alle 5 min: (1) EODHD-Quota prüfen; (2) die aktive Phase sicherstellen/resumen; (3) Stall erkennen
# (Fortschritt eingefroren) -> kill+restart; (4) Fortschritt committen+pushen.
# Gestartet vom stündlichen Self-Trigger (überlebt so einen Container-Reclaim). Idempotent.
set -u
REPO=/home/user/Alpha-Analyzer
PERS="$REPO/System/integration/retro_kat_map_persistent.json"
LOG="$REPO/System/integration/.watchdog.log"     # gitignored
BATCHLOG="$REPO/System/integration/.batch.log"   # gitignored (Klassifikation)
BFLOG="$REPO/System/integration/.backfill.log"   # gitignored (Fundamentals-Backfill)
cd "$REPO" || exit 1

# Nur EIN Watchdog: PID-Lockfile (übersteht Reclaim, da /tmp ephemer -> Lock weg = frei).
LOCK=/tmp/mtf_watchdog.pid
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "$(date -u) watchdog läuft bereits (pid $(cat "$LOCK")) — Ende" >> "$LOG"; exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

quota_ok() {   # 0 = Quota da, 1 = erschöpft/Fehler. BILLIG: EOD-Call = 1 API-Call (nicht 10 wie Fundamentals).
  local r                                         # Tageslimit ist global -> EOD-Sonde testet Erschöpfung.
  r=$(timeout 45 python3 -c "
import sys; sys.path[:0]=['System/connectors']
from eodhd_prices import fetch_eod
try:
    rows=fetch_eod('AAPL.US', from_date='2024-01-02', to_date='2024-01-05')
    print('OK' if rows else 'EMPTY')
except Exception as e:
    print('EXHAUSTED' if 'exceed' in str(e).lower() else 'ERR')
" 2>/dev/null | tail -1)
  [ "$r" = "OK" ] || [ "$r" = "EMPTY" ]
}

commit_progress() {
  python3 -c "import json; json.load(open('$PERS'))" 2>/dev/null || return
  git -C "$REPO" add "$PERS" 2>/dev/null
  git -C "$REPO" commit -q -m "EOD-Grind: Watchdog-Fortschritt [auto]

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VV2NXeGeTGo9VouDSbDoXU" 2>/dev/null && git -C "$REPO" push -q 2>/dev/null
}

# --- Phase-Marker + Fortschrittszähler ------------------------------------------------------------
n_checked() { python3 -c "import json;print(len(json.load(open('$PERS'))['checked']))" 2>/dev/null || echo 0; }
n_cached()  { python3 -c "import sys;sys.path[:0]=['System/connectors'];import fundamentals_cache as c;print(c.bestand()[0])" 2>/dev/null || echo 0; }
backfill_done() { grep -q "n_rest=0" "$BFLOG" 2>/dev/null; }   # ✅-Print = US-Rückstand geleert

start_batch() {
  nohup python3 System/integration/retro_kat_map_breit.py --voll --gic-direkt --region=all --commit=1000 >> "$BATCHLOG" 2>&1 &
  echo "$(date -u) klassifikation gestartet pid=$!" >> "$LOG"
}
start_backfill() {
  nohup python3 System/integration/fundamentals_backfill.py --lauf >> "$BFLOG" 2>&1 &
  echo "$(date -u) fundamentals-backfill gestartet pid=$!" >> "$LOG"
}

LAST=""
while true; do
  if backfill_done && grep -q "| offen: 0 " "$BATCHLOG" 2>/dev/null; then
    echo "$(date -u) Backfill + Klassifikation fertig — Watchdog Ende" >> "$LOG"; commit_progress; break
  fi
  if quota_ok; then
    if ! backfill_done; then
      # PHASE 1: Fundamentals-Backfill hat Vorrang. Klassifikation ruhen lassen (geteilte Quota).
      pkill -9 -f 'retro_kat_map_breit.py --voll' 2>/dev/null
      CUR="$(n_cached)"
      if ! pgrep -f 'fundamentals_backfill.py --lauf' >/dev/null; then
        echo "$(date -u) quota OK, backfill nicht aktiv -> start (cached=$CUR)" >> "$LOG"; start_backfill
      elif [ "$CUR" = "$LAST" ]; then                # läuft, aber kein Fortschritt = Stall
        echo "$(date -u) BACKFILL-STALL (cached=$CUR eingefroren) -> kill+restart" >> "$LOG"
        pkill -9 -f 'fundamentals_backfill.py --lauf'; sleep 3; start_backfill
      else
        echo "$(date -u) backfill läuft, cached=$CUR" >> "$LOG"
      fi
      LAST="$CUR"
    else
      # PHASE 2: Klassifikations-Voll-Batch mit der Rest-Quota.
      CUR="$(n_checked)"
      if ! pgrep -f 'retro_kat_map_breit.py --voll' >/dev/null; then
        echo "$(date -u) quota OK, klassifikation nicht aktiv -> start (checked=$CUR)" >> "$LOG"; start_batch
      elif [ "$CUR" = "$LAST" ]; then
        echo "$(date -u) KLASSIFIKATIONS-STALL (checked=$CUR eingefroren) -> kill+restart" >> "$LOG"
        pkill -9 -f 'retro_kat_map_breit.py --voll'; sleep 3; start_batch
      else
        echo "$(date -u) klassifikation läuft, checked=$CUR" >> "$LOG"
      fi
      LAST="$CUR"
    fi
    commit_progress
  else
    echo "$(date -u) quota erschöpft — warte" >> "$LOG"
    pkill -9 -f 'fundamentals_backfill.py --lauf' 2>/dev/null   # nicht sinnlos gegen die Wand spinnen
    pkill -9 -f 'retro_kat_map_breit.py --voll' 2>/dev/null
  fi
  sleep 300
done
