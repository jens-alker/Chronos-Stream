"""
arxiv_fetch.py — Konnektor: arXiv-API (echte Paper) -> Modul-2-Input (facts).

Die Paper-Quelle des Sammlers, live abgerufen (HTTPS über den Proxy) — echte Rohdokumente statt
Fixtures, falls keine scraper.db vorliegt. Atom-Feed -> Modul-2-`facts`-Eingang: Titel+Abstract
als Text, Quellentyp `paper` (untere Sprosse der Reifegradleiter), `published` -> t_event/
t_disclosed, Abrufzeit -> t_ingest.

Nur Standardbibliothek. Erfordert Netzzugang zu export.arxiv.org (HTTPS; Host muss in der
Egress-Allowlist stehen).
"""
import datetime
import re
import subprocess
import urllib.parse

_BASE = "https://export.arxiv.org/api/query"


def _heute():
    return datetime.date.today().isoformat()


def fetch(query, max_results=5, timeout=30, sort_by=None, sort_order="descending"):
    """arXiv-Atom-Feed abrufen. query: arXiv-Suchsyntax (z. B. 'all:transformer AND all:grid').
    `sort_by='submittedDate'` + `sort_order='descending'` liefert die NEUESTEN zuerst — für den
    Forward-Sammellauf ZWINGEND (Default-Relevanzsortierung gibt alte Paper -> Vintage-Leck, wenn das
    Frontier-Ensemble sie liest)."""
    q = urllib.parse.quote(query)      # korrekte URL-Kodierung (auch Phrasen/Operatoren)
    url = f"{_BASE}?search_query={q}&start=0&max_results={int(max_results)}"
    if sort_by:
        url += f"&sortBy={urllib.parse.quote(sort_by)}&sortOrder={urllib.parse.quote(sort_order)}"
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                         capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"arXiv nicht erreichbar (Egress-Allowlist?): rc={out.returncode} "
                           f"{out.stderr[:200]}")
    return out.stdout


def zu_facts(atom_xml, t_ingest=None):
    """arXiv-Atom -> Modul-2-`facts`-Eingang (je Paper eine Zeile)."""
    t_ingest = t_ingest or _heute()
    out = []
    for i, e in enumerate(re.findall(r"<entry>(.*?)</entry>", atom_xml, re.S)):
        titel = re.search(r"<title>(.*?)</title>", e, re.S)
        summ = re.search(r"<summary>(.*?)</summary>", e, re.S)
        pub = re.search(r"<published>(.*?)</published>", e, re.S)
        titel_s = " ".join(titel.group(1).split()) if titel else ""
        summ_s = " ".join(summ.group(1).split()) if summ else ""
        datum = (pub.group(1)[:10] if pub else t_ingest)
        out.append({
            "fact_id": f"arxiv{i}",
            "subjekt": f"{titel_s}. {summ_s}"[:2000], "beziehung": "", "objekt": "",
            "quellentyp": "paper", "rolle": "technologie",
            "t_event": datum, "t_disclosed": datum, "t_ingest": t_ingest,
        })
    return out


def lade_paper(query, max_results=5, t_ingest=None):
    """Bequem: fetch + zu_facts in einem Schritt."""
    return zu_facts(fetch(query, max_results=max_results), t_ingest=t_ingest)


def zu_dokumente(atom_xml):
    """arXiv-Atom -> ROH-DOKUMENTE im scraper.db-`documents`-Schema (title/text/url/published_at getrennt,
    NICHT zu `subjekt` verschmolzen). Das ist die Sammellauf-Naht: kompatibel zum Heim-Produzenten, damit
    die Cloud-Sammlung mit der lokalen scraper.db mergebar bleibt (UNIQUE(title, published_at)).
    -> Liste [{source_type:'arxiv', title, text, url, published_at}]. Einträge ohne Datum/Titel entfallen
    (published_at ist NOT NULL, fail-closed)."""
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", atom_xml, re.S):
        titel = re.search(r"<title>(.*?)</title>", e, re.S)
        summ = re.search(r"<summary>(.*?)</summary>", e, re.S)
        pub = re.search(r"<published>(.*?)</published>", e, re.S)
        idm = re.search(r"<id>(.*?)</id>", e, re.S)
        titel_s = " ".join(titel.group(1).split()) if titel else ""
        summ_s = " ".join(summ.group(1).split()) if summ else ""
        datum = pub.group(1)[:10] if pub else None
        if not titel_s or not datum:
            continue
        out.append({
            "source_type": "arxiv", "title": titel_s, "text": summ_s,
            "url": (idm.group(1).strip() if idm else None), "published_at": datum,
        })
    return out


def sammle_dokumente(query, max_results=25, timeout=30):
    """Bequem: fetch (NEUESTE zuerst, submittedDate absteigend) + zu_dokumente — aktuelle arXiv-Roh-
    dokumente fürs kompatible, leckfreie Forward-Einsammeln."""
    return zu_dokumente(fetch(query, max_results=max_results, timeout=timeout,
                              sort_by="submittedDate", sort_order="descending"))
