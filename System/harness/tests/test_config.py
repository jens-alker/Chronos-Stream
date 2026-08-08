"""test_config.py — die ENV-gesteuerte Injektion (Cloud-Fakes ↔ lokaler Deploy). Der lokale Deploy ist laut
Design „nur ENV-Umstellung" — dieser Test pinnt, dass `MTF_LLM=ollama`/`MTF_STORE=sqlite` code-frei auflösen
(kein NotImplemented mehr) und die Defaults cloud-sicher (mock/mem) bleiben. Netzfrei (nur Konstruktion)."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARNESS = os.path.dirname(_HERE)
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

import config                                                          # noqa: E402


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("MTF_LLM", "MTF_STORE", "MTF_DB", "MTF_OLLAMA_MODEL")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_cloud_sicher(self):
        os.environ.pop("MTF_LLM", None)
        os.environ.pop("MTF_STORE", None)
        self.assertEqual(type(config.get_llm()).__name__, "MockLLM")       # Default = Cloud-Fake
        self.assertEqual(type(config.get_store()).__name__, "MemStore")

    def test_ollama_loest_code_frei_auf(self):
        # Lokaler Deploy = nur ENV-Umstellung: MTF_LLM=ollama konstruiert den echten Adapter (kein
        # NotImplementedError mehr). Netzfrei — der Aufruf würde erst bei fehlendem Ollama werfen.
        os.environ["MTF_LLM"] = "ollama"
        os.environ["MTF_OLLAMA_MODEL"] = "qwen3:30b"
        llm = config.get_llm()
        self.assertEqual(type(llm).__name__, "OpenAICompatLLM")
        self.assertTrue(hasattr(llm, "kategorisiere"))
        self.assertIn("qwen3:30b", llm.model)

    def test_sqlite_store_lokaler_deploy(self):
        os.environ["MTF_STORE"] = "sqlite"
        os.environ["MTF_DB"] = os.path.join(tempfile.mkdtemp(), "s.db")
        self.assertEqual(type(config.get_store()).__name__, "SqliteStore")

    def test_unbekanntes_llm_wirft(self):
        os.environ["MTF_LLM"] = "gpt5"
        with self.assertRaises(ValueError):
            config.get_llm()


if __name__ == "__main__":
    unittest.main(verbosity=2)
