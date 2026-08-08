"""
hf_embedding.py — Cloud-Embedding über die HuggingFace-Inference-API (für das semantische Like-Dedup
der Cloud-Sammlung, Jens 30.07.). Spiegelt die Heim-Logik (`documents.dup_of` via lokale nomic-Embeddings),
nur mit einem API-Modell, weil im Cloud-Container kein Ollama läuft.

Modell: `sentence-transformers/all-MiniLM-L6-v2` (384-dim, klein/schnell, free-tier). HTTPS via `curl`
durch den vorkonfigurierten Proxy (wie die übrigen Konnektoren). Key: `$HUGGING_FACE_API_KEY`
(casing-robust). Nur Standardbibliothek.

Reiner Batch-Embedder + Kosinus-Helfer; die Dedup-LOGIK (Blocking, Schwelle, dup_of setzen) lebt in
`sammler_db.markiere_near_dups` (dort, wo die Dokument-Sammlung liegt — keine Insel).
"""
import json
import os
import subprocess
import tempfile

_MODELL = "sentence-transformers/all-MiniLM-L6-v2"
_URL = f"https://router.huggingface.co/hf-inference/models/{_MODELL}/pipeline/feature-extraction"


def _key(override=None):
    if override:
        return override
    for name in ("HUGGING_FACE_API_KEY", "HF_API_KEY", "HUGGINGFACE_API_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    ziel = "hugging_face_api_key"
    for k, v in os.environ.items():
        if k.lower() == ziel and v:
            return v
    raise RuntimeError("Kein HuggingFace-Key ($HUGGING_FACE_API_KEY).")


def _mean_pool(matrix):
    """Token-Level-Embeddings [[float]] -> EIN Satz-Vektor (Mittel über die Token-Achse)."""
    n = len(matrix)
    dim = len(matrix[0])
    return [sum(row[i] for row in matrix) / n for i in range(dim)]


def _als_satzvektor(eintrag):
    """Ein HF-Antwort-Element -> flacher Satz-Vektor (float-Liste). Claude-QS B2: der
    `feature-extraction`-Endpoint liefert je nach Modell/Pooling ENTWEDER [float,…] (gepoolt) ODER
    [[float,…],…] (Token-Level, 3-fach genestet) — der alte `isinstance(data[0], list)`-Check ließ
    Token-Level durch, `kosinus` machte dann `zip` über Listen-Elemente = TypeError (Dedup fiel still
    aus). Jetzt: Token-Level wird gemittelt, flach durchgereicht, alles andere fail-loud."""
    if not isinstance(eintrag, list) or not eintrag:
        raise RuntimeError(f"HF-Embedding: leeres/kein Listen-Element: {str(eintrag)[:120]}")
    if isinstance(eintrag[0], (int, float)):
        return [float(x) for x in eintrag]                         # bereits gepoolt: [float,…]
    if isinstance(eintrag[0], list) and eintrag[0] and isinstance(eintrag[0][0], (int, float)):
        return _mean_pool(eintrag)                                 # Token-Level [[float,…],…] -> Mittel
    raise RuntimeError(f"HF-Embedding: unerwartetes Element-Schema: {str(eintrag)[:120]}")


def embed(texte, key=None, timeout=60):
    """Liste von Texten -> Liste von Embedding-Vektoren (float-Listen, gepoolt). Batch in EINEM Call.
    Wirft bei Fehler/unerwartetem Schema (fail-loud, kein stiller 0-Vektor -> sonst würde alles als
    „identisch" dedupliziert). Token-Level-Ausgaben werden gemittelt (Claude-QS B2)."""
    if not texte:
        return []
    body = {"inputs": [str(t or "")[:2000] for t in texte]}
    n_erwartet = len(body["inputs"])
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    try:
        json.dump(body, tmp); tmp.close()
        cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", "POST", _URL,
               "-H", f"Authorization: Bearer {_key(key)}", "-H", "Content-Type: application/json",
               "--data", "@" + tmp.name]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    finally:
        os.unlink(tmp.name)
    if out.returncode != 0:
        raise RuntimeError(f"HF-Embedding curl rc={out.returncode}: {out.stderr[:200]}")
    data = json.loads(out.stdout)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"HF-Embedding: {str(data['error'])[:200]}")
    if not (isinstance(data, list) and data):
        raise RuntimeError(f"HF-Embedding: unerwartetes Schema: {str(data)[:200]}")
    # Claude-QS B2: ein EINZELNER flacher Vektor [float,…] (Batch 1 / manche Server) -> als ein Element
    # heben, bevor je Input gepoolt wird. Danach: Anzahl muss zur Eingabe passen (fail-loud gegen
    # verschluckte/zusätzliche Zeilen — sonst würde ein Doc gegen den falschen Vektor verglichen).
    if isinstance(data[0], (int, float)):
        data = [data]
    vecs = [_als_satzvektor(e) for e in data]
    if len(vecs) != n_erwartet:
        raise RuntimeError(f"HF-Embedding: {len(vecs)} Vektoren für {n_erwartet} Eingaben (Schema-Drift).")
    return vecs


# Kosinus = die EINE geteilte Definition (dedup_kern) — kein zweiter Cosinus (Jens 30.07., keine Insel).
# Re-Export, damit bestehende `from hf_embedding import kosinus`-Aufrufer byte-identisch weiterlaufen.
from dedup_kern import kosinus  # noqa: E402,F401


def embed_fn(key=None):
    """Bequemer, gebundener Embedder (ein Argument) für `sammler_db.markiere_near_dups`."""
    def fn(texte):
        return embed(texte, key=key)
    return fn
