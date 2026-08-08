"""
test_schluessel.py — die EINE Key-Quelle (config.txt -> Umgebungsvariablen).

Deckt: Sync mit anbieter_registry (Ensemble-Env-Namen), Auflösung inline + Datei,
no-overwrite (OS schlägt Datei), fail-soft bei fehlender Datei, config-Parsing.

Ausführen:  python3 System/harness/tests/test_schluessel.py
"""
import os
import sys
import tempfile
import unittest

_H = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _H not in sys.path:
    sys.path.insert(0, _H)

import schluessel as s                                                    # noqa: E402
import anbieter_registry as reg                                           # noqa: E402


class TestSchluessel(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_sync_mit_anbieter_registry(self):
        # Jeder Ensemble-Anbieter mit key_env MUSS in ENV_MAP stehen — sonst
        # bekommt ein Router-Anbieter seinen Key aus config.txt nie.
        noetig = {a["key_env"] for a in reg.ANBIETER.values() if a.get("key_env")}
        fehlt = noetig - set(s.ENV_MAP)
        self.assertEqual(fehlt, set(), f"nicht in ENV_MAP: {fehlt}")

    def test_aufloesen_inline_und_datei(self):
        d = tempfile.mkdtemp()
        kf = os.path.join(d, "groq.txt")
        with open(kf, "w") as f:
            f.write("  GKEY123 \n")                      # Whitespace wird getrimmt
        cfg = {"groq_api_key_file": kf, "gemini_api_key": "GEMINLINE"}
        got = s.aufloesen(cfg)
        self.assertEqual(got["GROQ_API_KEY"], "GKEY123")
        self.assertEqual(got["GEMINI_API_KEY"], "GEMINLINE")

    def test_lade_ins_environ_und_no_overwrite(self):
        cfg = {"groq_api_key": "AUS_CONFIG"}
        os.environ.pop("GROQ_API_KEY", None)
        self.assertEqual(s.lade_ins_environ(cfg), ["GROQ_API_KEY"])
        self.assertEqual(os.environ["GROQ_API_KEY"], "AUS_CONFIG")
        # zweiter Lauf: schon gesetzt -> NICHT überschreiben (OS/Env schlägt Datei)
        os.environ["GROQ_API_KEY"] = "AUS_OS"
        self.assertEqual(s.lade_ins_environ(cfg), [])
        self.assertEqual(os.environ["GROQ_API_KEY"], "AUS_OS")

    def test_fehlende_datei_faellt_nicht(self):
        cfg = {"mistral_api_key_file": "/gibt/es/nicht.txt"}
        # fail-soft: keine Exception, Key einfach nicht gesetzt
        got = s.lade_ins_environ(cfg)
        self.assertNotIn("MISTRAL_API_KEY", got)

    def test_finde_config_liste(self):
        # finde_config liefert einen Pfad (existiert einer der Kandidaten nicht,
        # den System/Config-Default) — und akzeptiert die Projektwurzel-Lage.
        p = s.finde_config()
        self.assertTrue(p.endswith(os.path.join("Config", "config.txt")))

    def test_lade_config_parst(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.txt")
        with open(p, "w") as f:
            f.write("# Kommentar\ncontact_email = a@b.de\nGEMINI_API_KEY = X = Y\n")
        cfg = s.lade_config(p)
        self.assertEqual(cfg["contact_email"], "a@b.de")
        self.assertEqual(cfg["gemini_api_key"], "X = Y")     # nur erstes '=' trennt


if __name__ == "__main__":
    unittest.main(verbosity=2)
