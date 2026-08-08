"""
gdrive.py — Direkte Google-Drive-REST-Anbindung (OAuth, autonom, ohne MCP/Agent).

Jens (26.07.): die Fundamentals-DB liegt in Google Drive (führende Ablage). Der Python-Grind spricht Drive
DIREKT über die REST-API an (nicht über die agenten-only MCP) → echtes CRUD inkl. **Löschen**, resumable
Upload, läuft autonom auch in Routine-Sessions. Auth: OAuth-User-Credentials (Consumer-@gmail.com; ein
Service-Account hätte dort kein Speicherkontingent). Access-Tokens werden aus dem Refresh-Token gemintet.

Credentials aus der Umgebung (wie EODHD/Gemini):
  GOOGLE_OAUTH_CLIENT_ID · GOOGLE_OAUTH_CLIENT_SECRET · GOOGLE_OAUTH_REFRESH_TOKEN

**Health-Check (Jens: Orchestrierung muss überwachen können, ob's funktioniert):** `preflight()` prüft die
ganze Kette fail-loud — Env-Credentials da? Token-Refresh? Drive-API erreichbar? DB-Ordner zugreifbar? — und
liefert einen klaren Status, damit der Grind NIE still Daten nach nirgends schreibt. Nur Standardbibliothek + curl.

**Restore-Fix (2026-07-28):** `datei_lesen` reicht einen transienten Leer-Download (curl-Timeout `rc=28`)
NICHT mehr still als `b""` durch (das tauchte downstream als „korruptes Manifest / Restore fehlgeschlagen"
auf, obwohl das Manifest intakt war) → rc-Prüfung + Backoff-Retry + fail-loud. `liste_ordner` paginiert über
nextPageToken (kein stiller 1000-Datei-Cap, sobald ein Shard je Bucket den Ordner füllt).
"""
import json
import os
import subprocess

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
_DB_ORDNER_NAME = "makro_fundamentals_db"


class DriveFehler(RuntimeError):
    """Harter Drive-/Auth-Fehler — fail-loud (der Grind darf nicht still ohne Ablage weiterlaufen)."""


def _creds(override=None):
    """OAuth-Credentials aus der Umgebung (oder `override`-dict für Tests). Fehlt eines → DriveFehler."""
    q = override or {}
    cid = q.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    sec = q.get("client_secret") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    ref = q.get("refresh_token") or os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    fehlt = [n for n, v in (("GOOGLE_OAUTH_CLIENT_ID", cid), ("GOOGLE_OAUTH_CLIENT_SECRET", sec),
                            ("GOOGLE_OAUTH_REFRESH_TOKEN", ref)) if not v]
    if fehlt:
        raise DriveFehler(f"Fehlende OAuth-Credentials in der Umgebung: {', '.join(fehlt)}")
    return cid, sec, ref


def _curl(args, timeout=30):
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout)] + args,
                         capture_output=True, text=True, timeout=timeout + 10)
    return out.stdout, out.returncode


def access_token(override=None):
    """Frisches Access-Token aus dem Refresh-Token (autonom, kein Login). -> str. Fail-loud."""
    import urllib.parse
    cid, sec, ref = _creds(override)
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": sec,
                                   "refresh_token": ref, "grant_type": "refresh_token"})
    body, rc = _curl(["-X", "POST", _TOKEN_URL, "-H", "Content-Type: application/x-www-form-urlencoded", "-d", data])
    if rc != 0:
        raise DriveFehler(f"Token-Endpoint nicht erreichbar (rc={rc})")
    try:
        r = json.loads(body)
    except json.JSONDecodeError:
        raise DriveFehler(f"Token-Antwort kein JSON: {body[:120]}")
    if "access_token" not in r:
        raise DriveFehler(f"Token-Refresh fehlgeschlagen: {r.get('error')} {r.get('error_description','')}")
    return r["access_token"]


def _auth(at):
    return ["-H", f"Authorization: Bearer {at}"]


def _json(body):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise DriveFehler(f"Drive-Antwort kein JSON: {body[:160]}")


def _pruefe_api(d):
    """Eine Drive-Antwort auf den 'API disabled'-Fehler prüfen (fail-loud mit klarer Meldung)."""
    if isinstance(d, dict) and "error" in d:
        err = d["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        if reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used" in err.get("message", ""):
            raise DriveFehler("Google Drive API ist im Projekt NICHT aktiviert (oder falsches Projekt). "
                              "Aktivieren + einige Minuten warten.")
        raise DriveFehler(f"Drive-API-Fehler {err.get('code')}: {err.get('message','')[:160]}")
    return d


# ------------------------------------------------------------------ CRUD

def ordner_finden_oder_anlegen(at, name=_DB_ORDNER_NAME):
    """Den DB-Ordner (von der App erstellt, drive.file) finden oder anlegen. -> folder_id."""
    import urllib.parse
    q = urllib.parse.quote(f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false")
    body, _ = _curl([f"{_DRIVE}/files?q={q}&fields=files(id,name)"] + _auth(at))
    d = _pruefe_api(_json(body))
    if d.get("files"):
        return d["files"][0]["id"]
    meta = json.dumps({"name": name, "mimeType": "application/vnd.google-apps.folder"})
    body, _ = _curl(["-X", "POST", f"{_DRIVE}/files", "-H", "Content-Type: application/json", "-d", meta] + _auth(at))
    return _pruefe_api(_json(body))["id"]


def datei_anlegen(at, name, inhalt_bytes, parent_id, mime="application/gzip"):
    """Datei (bytes) in den Ordner hochladen (multipart). -> file_id. Für Shards/Manifest der DB."""
    import tempfile
    grenze = "mtfboundary7c1"
    meta = json.dumps({"name": name, "parents": [parent_id]})
    kopf = (f"--{grenze}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{grenze}\r\nContent-Type: {mime}\r\n\r\n").encode()
    fuss = f"\r\n--{grenze}--".encode()
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(kopf + inhalt_bytes + fuss)
        pfad = f.name
    try:
        body, _ = _curl(["-X", "POST", f"{_UPLOAD}/files?uploadType=multipart",
                         "-H", f"Content-Type: multipart/related; boundary={grenze}",
                         "--data-binary", f"@{pfad}"] + _auth(at))
    finally:
        os.unlink(pfad)
    return _pruefe_api(_json(body))["id"]


def datei_lesen(at, file_id, versuche=4):
    """Roh-Inhalt (bytes) einer Datei. Für Restore der Shards/Manifeste. **Fail-loud + Retry (Fix
    2026-07-28):** `curl -o` schreibt bei einem transienten Blip (Timeout `rc=28`, live beobachtet) 0
    Bytes in die Tempdatei; das ALTE `datei_lesen` ignorierte den curl-Returncode und gab diese Leere
    kommentarlos zurück → downstream `json.loads(b"")` = „Expecting value: line 1 column 1 (char 0)"
    (der Grind-Restore-Fehler: das Manifest war intakt, nur der Download blitzte weg). Jetzt: rc prüfen,
    Leerinhalt als Fehlschlag werten (unsere Objekte sind nie 0 Bytes — Manifest ≥ '{}', Shard = gzip),
    mit Backoff neu versuchen (Downloads sind idempotent), sonst DriveFehler → der Aufrufer bricht sauber
    ab, statt still Nichts zu verarbeiten. Ein echtes Drive-Fehler-JSON (404 o. ä.) wirft SOFORT (kein Retry)."""
    import tempfile
    import time
    letzter = ""
    for i in range(versuche):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            pfad = f.name
        try:
            _, rc = _curl(["-o", pfad, f"{_DRIVE}/files/{file_id}?alt=media"] + _auth(at))
            with open(pfad, "rb") as fr:
                roh = fr.read()
        finally:
            os.unlink(pfad)
        if roh[:1] == b"{" and b'"error"' in roh[:200]:      # Fehler-JSON statt Binärinhalt → permanent, kein Retry
            _pruefe_api(_json(roh.decode("utf-8", "replace")))
        if rc == 0 and roh:                                   # echter, nicht-leerer Inhalt
            return roh
        letzter = f"rc={rc}, bytes={len(roh)}"
        if i < versuche - 1:
            time.sleep(2 ** i)                                # 1s, 2s, 4s — transienter Netz-Blip abklingen lassen
    raise DriveFehler(f"Download von {file_id} fehlgeschlagen nach {versuche} Versuchen ({letzter})")


def datei_loeschen(at, file_id):
    """Datei löschen (das kann die MCP nicht). -> True bei HTTP 204."""
    out = subprocess.run(["curl", "-sS", "--max-time", "30", "-o", "/dev/null", "-w", "%{http_code}",
                          "-X", "DELETE", f"{_DRIVE}/files/{file_id}"] + _auth(at), capture_output=True, text=True)
    return out.stdout.strip() == "204"


def liste_ordner(at, parent_id, name_praefix=None):
    """Dateien im Ordner {name -> id}. Optional auf `name_praefix` gefiltert (z. B. 'manifest__').
    **Paginiert über nextPageToken (Fix 2026-07-28):** sobald der DB-Ordner >1000 Dateien hat (ein Shard
    je Bucket + Manifest — aktuell 918, knapp am Limit), schnitt die alte Single-Page-Abfrage still ab →
    `_neuestes_manifest`/Restore fänden Shards oder das Manifest nicht mehr (silent cap, verboten)."""
    import urllib.parse
    q = urllib.parse.quote(f"'{parent_id}' in parents and trashed=false")
    out = {}
    seite = ""
    while True:
        extra = f"&pageToken={urllib.parse.quote(seite)}" if seite else ""
        body, _ = _curl([f"{_DRIVE}/files?q={q}&fields=nextPageToken,files(id,name)&pageSize=1000{extra}"] + _auth(at))
        d = _pruefe_api(_json(body))
        for f in d.get("files", []):
            if name_praefix is None or f["name"].startswith(name_praefix):
                out[f["name"]] = f["id"]
        seite = d.get("nextPageToken") or ""
        if not seite:
            break
    return out


# ------------------------------------------------------------------ Health-Check

def preflight(override=None):
    """Fail-loud-Kette (Jens: die Orchestrierung muss überwachen können, ob's funktioniert):
    Env-Credentials → Token-Refresh → Drive-API erreichbar → DB-Ordner zugreifbar. -> Status-dict
    {ok: bool, schritt, konto, ordner_id, quota, fehler}. Wirft NICHT — meldet strukturiert, damit der
    Grind vor dem Schreiben entscheiden kann (kein stiller Datenverlust)."""
    st = {"ok": False, "schritt": None, "konto": None, "ordner_id": None, "quota": None, "fehler": None}
    try:
        st["schritt"] = "credentials"
        _creds(override)
        st["schritt"] = "token_refresh"
        at = access_token(override)
        st["schritt"] = "drive_about"
        body, _ = _curl([f"{_DRIVE}/about?fields=user(emailAddress),storageQuota(limit,usage)"] + _auth(at))
        about = _pruefe_api(_json(body))
        st["konto"] = about.get("user", {}).get("emailAddress")
        q = about.get("storageQuota", {})
        if q.get("limit"):
            st["quota"] = f"{int(q.get('usage', 0)) / 1e9:.2f}/{int(q['limit']) / 1e9:.1f} GB"
        st["schritt"] = "db_ordner"
        st["ordner_id"] = ordner_finden_oder_anlegen(at)
        st["ok"] = True
        st["schritt"] = "fertig"
    except DriveFehler as e:
        st["fehler"] = str(e)
    return st
