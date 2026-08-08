"""
modell_gate.py — prozess-ÜBERGREIFENDE GPU-Modell-Sperre (Jens 08.08.).

Auf der 20-GB-Heim-Karte passt NICHT beides gleichzeitig in den VRAM: das Extraktions-Modell `qwen3:30b`
(Sammler/1c, ~19 GB) und `nomic-embed-text` (Embeddings/Dedup/V1-Kategorisierer). Laufen zwei Ollama-Aufrufe
GLEICHZEITIG (z. B. der Sammler-Prozess 1c UND ein Pipeline-Prozess mit nomic), lädt Ollama beide Modelle →
OOM/Absturz. Der Sammler hat schon eine IN-PROCESS-Sperre (`LLM_GATE`, Semaphore(1)); diese Datei deckt die
PROZESS-GRENZE: eine dateibasierte, atomare, stale-feste Sperre, die ALLE Prozesse serialisiert — nur EIN
Ollama-Modellaufruf gleichzeitig, systemweit.

Nutzung (Kontext-Manager, pro Aufruf — kurze Haltezeit):
    from modell_gate import gpu_lock
    with gpu_lock("nomic"):        # blockiert, solange ein anderer Prozess die GPU nutzt
        ... ein Ollama-Aufruf ...

Mechanik: atomares `O_CREAT|O_EXCL`-Lockfile ist der Schiedsrichter. Ein verwaistes Lockfile (Halter gecrasht)
wird nach `stale_sek` gebrochen (mtime-basiert, cross-platform, kein PID-Signal nötig). Nur Standardbibliothek.
`gpu_lock` ist bewusst PRO-AUFRUF gedacht (Haltezeit = ein Modellaufruf, Sekunden bis ~90 s Kaltstart) — nie
über einen ganzen Batch halten (das würde den anderen Modell-Nutzer aushungern; die Batches alternieren).
"""
import os
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# EIN Lockfile fuer die ganze Maschine (per Env ueberschreibbar; Default neben dem Harness).
_DEFAULT_PFAD = os.environ.get("MTF_MODELL_LOCK") or os.path.join(_HERE, ".gpu_modell.lock")
# stale_sek: NUR echte Crashes brechen die Sperre. Ein LEBENDER Halter frischt die mtime per Heartbeat auf
# (alle stale_sek/3), also ist stale_sek NICHT mehr an die Aufrufdauer gebunden (Fable-B1). 180 s > 3×Heartbeat.
_STALE_SEK = float(os.environ.get("MTF_MODELL_LOCK_STALE", "180"))


class GpuBelegt(RuntimeError):
    """Die GPU-Modell-Sperre war über das Timeout hinweg von einem anderen Modell-Nutzer belegt."""


class gpu_lock:
    """Kontext-Manager: hält die prozess-übergreifende GPU-Modell-Sperre für EINEN Modellaufruf.
    `modell`: nur Protokoll. `timeout`: max. Wartezeit (Default 420 s > der längste 1c-Call ~300 s, damit ein
    Wartender NICHT vorzeitig aufgibt). `warte_fn`/`jetzt_fn`: injizierbar (Test). Wirft `GpuBelegt` bei Timeout.

    **Fable-B1/M1 gehärtet:** (1) HEARTBEAT — ein Daemon-Thread frischt die mtime auf, solange gehalten → ein
    LEBENDER Halter wird nie als stale gebrochen (nur echte Crashes, deren Heartbeat mitstirbt). (2) BESITZ-
    TOKEN — nur der Ersteller entfernt sein Lockfile (kein Löschen eines fremden, frisch gebrochenen Locks).
    (3) STALE-BRUCH per atomarem `os.replace` (Rename) statt `remove` → genau EIN Breaker gewinnt (kein TOCTOU)."""

    def __init__(self, modell="?", timeout=420.0, stale_sek=None, pfad=None,
                 warte_fn=time.sleep, jetzt_fn=time.time):
        self.modell = modell or "?"
        self.timeout = float(timeout)
        self.stale_sek = _STALE_SEK if stale_sek is None else float(stale_sek)
        self.pfad = pfad or _DEFAULT_PFAD
        self._warte = warte_fn
        self._jetzt = jetzt_fn
        self._besitzt = False
        self._token = None
        self._hb_stop = None
        self._hb_thread = None

    def _versuche_anlegen(self):
        try:
            fd = os.open(self.pfad, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, f"{self._token} {os.getpid()} {self.modell}".encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _heartbeat(self):
        intervall = max(1.0, self.stale_sek / 3.0)
        while not self._hb_stop.wait(intervall):
            try:
                os.utime(self.pfad, None)                       # mtime auf jetzt -> nicht stale, solange ich lebe
            except OSError:
                break                                           # Lock weg (gebrochen) -> Heartbeat beenden

    def __enter__(self):
        self._token = f"{os.getpid()}-{os.urandom(6).hex()}"   # eindeutig, nicht zeit-abhaengig
        ende = self._jetzt() + self.timeout
        while True:
            if self._versuche_anlegen():
                self._besitzt = True
                self._hb_stop = threading.Event()
                self._hb_thread = threading.Thread(target=self._heartbeat, daemon=True)
                self._hb_thread.start()
                return self
            try:
                alter = self._jetzt() - os.path.getmtime(self.pfad)
            except OSError:
                alter = 0.0                                     # Datei gerade weg -> gleich neu versuchen
            if alter > self.stale_sek:
                # verwaist (Halter gecrasht, kein Heartbeat mehr): ATOMAR beiseite-rename -> genau EINER gewinnt.
                beiseite = f"{self.pfad}.stale.{os.getpid()}.{os.urandom(3).hex()}"
                try:
                    os.replace(self.pfad, beiseite)
                    try:
                        os.remove(beiseite)
                    except OSError:
                        pass
                except OSError:
                    pass                                        # ein anderer hat schon gebrochen -> weiter
                continue
            if self._jetzt() >= ende:
                raise GpuBelegt(f"GPU-Modell-Sperre belegt (Timeout {self.timeout:.0f}s, "
                                f"anderer Modell-Nutzer aktiv) — {self.pfad}")
            self._warte(0.1)

    def __exit__(self, *_a):
        if self._hb_stop is not None:
            self._hb_stop.set()                                # Heartbeat stoppen
        if self._besitzt:
            try:                                               # NUR mein eigenes Lock entfernen (Token-Match)
                with open(self.pfad, encoding="utf-8") as f:
                    tok = (f.read().split() or [""])[0]
                if tok == self._token:
                    os.remove(self.pfad)
                # sonst: mein Lock wurde (fälschlich) gebrochen -> das FREMDE nicht anfassen
            except OSError:
                pass
            self._besitzt = False
        return False


def status(pfad=None, jetzt_fn=time.time):
    """Wer hält die GPU gerade? -> {'belegt':bool, 'modell':str|None, 'pid':int|None, 'alter_sek':float|None}.
    Read-only (für die Ollama-Status-Anzeige). Ein Lockfile älter als `_STALE_SEK` gilt als verwaist -> frei."""
    p = pfad or _DEFAULT_PFAD
    try:
        with open(p, encoding="utf-8") as f:
            teile = f.read().split()
        alter = jetzt_fn() - os.path.getmtime(p)
    except OSError:
        return {"belegt": False, "modell": None, "pid": None, "alter_sek": None}
    if alter > _STALE_SEK:
        return {"belegt": False, "modell": None, "pid": None, "alter_sek": alter}
    # Dateiformat: "<token> <pid> <modell>" (token = pid-hex, s. gpu_lock).
    pid = int(teile[1]) if len(teile) > 1 and teile[1].isdigit() else None
    modell = teile[2] if len(teile) > 2 else None
    return {"belegt": True, "modell": modell, "pid": pid, "alter_sek": alter}
