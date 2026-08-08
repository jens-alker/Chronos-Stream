"""
test_gdrive.py — Offline-Tests der Drive-REST-Anbindung (reine Logik, kein Netz).

Prüft: fehlende Credentials → fail-loud; der „API deaktiviert"-Detektor; normale Antwort passiert durch;
sowie die zwei Restore-Fixe (2026-07-28): `datei_lesen` reicht einen transienten Leer-Download NICHT mehr
still durch (fail-loud + Retry), `liste_ordner` paginiert über nextPageToken (kein silent cap bei >1000).
Der Live-CRUD (access_token/preflight/datei_*) braucht Netz + aktive Drive-API und ist hier nicht Teil des Tests.
Ausführen:  python3 System/connectors/tests/test_gdrive.py
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.dirname(_HERE)
if _CONNECTORS not in sys.path:
    sys.path.insert(0, _CONNECTORS)

import gdrive


class TestGdriveKern(unittest.TestCase):
    def test_creds_fehlend_fail_loud(self):
        # ohne Override + ohne Env → benennt die fehlenden Variablen
        alt = {k: os.environ.pop(k, None) for k in
               ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")}
        try:
            with self.assertRaises(gdrive.DriveFehler) as ctx:
                gdrive._creds()
            self.assertIn("GOOGLE_OAUTH_CLIENT_ID", str(ctx.exception))
        finally:
            for k, v in alt.items():
                if v is not None:
                    os.environ[k] = v

    def test_creds_override(self):
        c = gdrive._creds({"client_id": "a", "client_secret": "b", "refresh_token": "c"})
        self.assertEqual(c, ("a", "b", "c"))

    def test_pruefe_api_disabled(self):
        # der echte EODHD-… nein: der echte Drive-„API disabled"-Fehlerkörper → klare DriveFehler-Meldung
        d = {"error": {"code": 403, "message": "Google Drive API has not been used in project 123 before",
                       "errors": [{"reason": "accessNotConfigured"}]}}
        with self.assertRaises(gdrive.DriveFehler) as ctx:
            gdrive._pruefe_api(d)
        self.assertIn("nicht aktiviert", str(ctx.exception).lower())

    def test_pruefe_api_generischer_fehler(self):
        d = {"error": {"code": 404, "message": "File not found", "errors": [{"reason": "notFound"}]}}
        with self.assertRaises(gdrive.DriveFehler):
            gdrive._pruefe_api(d)

    def test_pruefe_api_ok_passiert_durch(self):
        d = {"files": [{"id": "x", "name": "y"}]}
        self.assertIs(gdrive._pruefe_api(d), d)

    def test_preflight_meldet_strukturiert_ohne_creds(self):
        # preflight wirft NICHT, sondern liefert Status (fail-loud strukturiert)
        alt = {k: os.environ.pop(k, None) for k in
               ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")}
        try:
            st = gdrive.preflight()
            self.assertFalse(st["ok"])
            self.assertEqual(st["schritt"], "credentials")
            self.assertIn("GOOGLE_OAUTH", st["fehler"])
        finally:
            for k, v in alt.items():
                if v is not None:
                    os.environ[k] = v


def _write_o(args, inhalt):
    """Simuliert `curl -o <pfad>`: schreibt `inhalt` (bytes) an den -o-Pfad (wie das echte curl)."""
    pfad = args[args.index("-o") + 1]
    with open(pfad, "wb") as f:
        f.write(inhalt)


class TestDateiLesenFailLoud(unittest.TestCase):
    """Der Restore-Bug 2026-07-28: ein transienter Leer-Download wurde still als korruptes JSON durchgereicht."""

    def setUp(self):
        import time
        self._curl = gdrive._curl
        self._time_sleep = time.sleep
        time.sleep = lambda *a, **k: None                      # datei_lesen macht `import time` → dasselbe Modul

    def tearDown(self):
        import time
        gdrive._curl = self._curl
        time.sleep = self._time_sleep

    def test_transienter_blip_dann_erfolg(self):
        # erste 2 Downloads: rc=28 + Leerdatei (Timeout); dritter: echter Inhalt → Retry liefert ihn
        rufe = {"n": 0}

        def fake(args, timeout=30):
            rufe["n"] += 1
            if rufe["n"] < 3:
                _write_o(args, b"")                            # Blip: 0 Bytes
                return "", 28
            _write_o(args, b'{"02": "abc"}')
            return "", 0

        gdrive._curl = fake
        roh = gdrive.datei_lesen("at", "fid")
        self.assertEqual(roh, b'{"02": "abc"}')
        self.assertEqual(rufe["n"], 3)                         # hat wirklich wiederholt

    def test_dauerhaft_leer_faellt_fail_loud(self):
        # bleibt der Download leer → DriveFehler (NICHT stiller b"" → korruptes JSON downstream)
        def fake(args, timeout=30):
            _write_o(args, b"")
            return "", 28

        gdrive._curl = fake
        with self.assertRaises(gdrive.DriveFehler) as ctx:
            gdrive.datei_lesen("at", "fid", versuche=3)
        self.assertIn("fehlgeschlagen", str(ctx.exception).lower())

    def test_leerinhalt_rc0_zaehlt_als_fehlschlag(self):
        # rc=0, aber 0 Bytes (unsere Objekte sind nie leer) → Fehlschlag, kein stilles b""
        def fake(args, timeout=30):
            _write_o(args, b"")
            return "", 0

        gdrive._curl = fake
        with self.assertRaises(gdrive.DriveFehler):
            gdrive.datei_lesen("at", "fid", versuche=2)

    def test_fehler_json_wirft_sofort_ohne_retry(self):
        # ein echtes Drive-Fehler-JSON (404) ist permanent → SOFORT werfen, nicht 4× wiederholen
        rufe = {"n": 0}

        def fake(args, timeout=30):
            rufe["n"] += 1
            _write_o(args, b'{"error": {"code": 404, "message": "File not found", '
                           b'"errors": [{"reason": "notFound"}]}}')
            return "", 0

        gdrive._curl = fake
        with self.assertRaises(gdrive.DriveFehler):
            gdrive.datei_lesen("at", "fid")
        self.assertEqual(rufe["n"], 1)                         # kein Retry auf permanenten Fehler


class TestListeOrdnerPaginiert(unittest.TestCase):
    """Fix 2026-07-28: >1000 Dateien im DB-Ordner dürfen NICHT still abgeschnitten werden (silent cap)."""

    def setUp(self):
        self._curl = gdrive._curl

    def tearDown(self):
        gdrive._curl = self._curl

    def test_folgt_nextpagetoken(self):
        import json as _json_mod
        seiten = [
            {"nextPageToken": "TOK2", "files": [{"id": "1", "name": "AA__x.json.gz"},
                                                {"id": "2", "name": "manifest__5.json"}]},
            {"files": [{"id": "3", "name": "BB__y.json.gz"}]},                 # letzte Seite: kein Token
        ]
        rufe = {"n": 0}

        def fake(args, timeout=30):
            url = args[0]
            i = 1 if "pageToken=TOK2" in url else 0
            rufe["n"] += 1
            return _json_mod.dumps(seiten[i]), 0

        gdrive._curl = fake
        out = gdrive.liste_ordner("at", "parent")
        self.assertEqual(rufe["n"], 2)                         # beide Seiten geholt
        self.assertEqual(set(out), {"AA__x.json.gz", "manifest__5.json", "BB__y.json.gz"})

    def test_praefix_filter_ueber_seiten(self):
        import json as _json_mod
        seiten = [
            {"nextPageToken": "T2", "files": [{"id": "1", "name": "manifest__1.json"},
                                              {"id": "2", "name": "AA__x.json.gz"}]},
            {"files": [{"id": "3", "name": "manifest__2.json"}]},
        ]

        def fake(args, timeout=30):
            i = 1 if "pageToken=T2" in args[0] else 0
            return _json_mod.dumps(seiten[i]), 0

        gdrive._curl = fake
        out = gdrive.liste_ordner("at", "parent", name_praefix="manifest__")
        self.assertEqual(set(out), {"manifest__1.json", "manifest__2.json"})   # Shard rausgefiltert, beide Seiten


if __name__ == "__main__":
    unittest.main()
