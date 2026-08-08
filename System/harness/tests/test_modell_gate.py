"""test_modell_gate — die prozess-übergreifende GPU-Modell-Sperre (Jens 08.08.): gegenseitiger Ausschluss,
Timeout, stale-Bruch (Halter gecrasht), Status. Deterministisch, injizierte Zeit/Warte-Fn, kein echtes Ollama."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARNESS = os.path.dirname(_HERE)
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

import modell_gate as M                                                # noqa: E402


class TestModellGate(unittest.TestCase):
    def setUp(self):
        self.p = os.path.join(tempfile.mkdtemp(), "gpu.lock")

    def test_gegenseitiger_ausschluss(self):
        # Hält Prozess A die Sperre, bekommt B sie NICHT (Timeout 0) -> GpuBelegt.
        a = M.gpu_lock("qwen3:30b", pfad=self.p)
        a.__enter__()
        try:
            with self.assertRaises(M.GpuBelegt):
                with M.gpu_lock("nomic", pfad=self.p, timeout=0.0):
                    pass
        finally:
            a.__exit__()
        # nach Freigabe geht es wieder
        with M.gpu_lock("nomic", pfad=self.p, timeout=0.0):
            pass

    def test_freigabe_entfernt_lockfile(self):
        with M.gpu_lock("nomic", pfad=self.p):
            self.assertTrue(os.path.exists(self.p))
        self.assertFalse(os.path.exists(self.p))                       # sauber freigegeben

    def test_stale_wird_gebrochen(self):
        # Ein verwaistes Lockfile (Halter gecrasht) älter als stale_sek wird gebrochen -> neuer Halter kommt rein.
        with open(self.p, "w") as f:
            f.write("999999 qwen3 0")
        os.utime(self.p, (0, 0))                                       # uralt (1970) -> stale
        with M.gpu_lock("nomic", pfad=self.p, stale_sek=60, timeout=1.0):
            pass                                                       # kein GpuBelegt -> stale gebrochen

    def test_timeout_wartet_und_wirft(self):
        # Frisches (nicht-stale) fremdes Lockfile -> Timeout -> GpuBelegt (mit injizierter Zeit/Warte).
        with open(self.p, "w") as f:
            f.write("999999 qwen3 0")
        t = [0.0]
        with self.assertRaises(M.GpuBelegt):
            M.gpu_lock("nomic", pfad=self.p, stale_sek=1e9, timeout=0.5,
                       warte_fn=lambda s: t.__setitem__(0, t[0] + s),
                       jetzt_fn=lambda: t[0]).__enter__()

    def test_token_schuetzt_fremdes_lock(self):
        # Fable-M1: wurde mein Lock (fälschlich) gebrochen und ein anderer hält jetzt, darf mein __exit__
        # NICHT das fremde Lock löschen.
        a = M.gpu_lock("qwen3", pfad=self.p)
        a.__enter__()
        with open(self.p, "w") as f:                                   # simuliere: gebrochen, Fremd-Halter B
            f.write("FREMD-TOKEN 12345 nomic")
        a.__exit__()
        self.assertTrue(os.path.exists(self.p))                        # fremdes Lock unangetastet
        with open(self.p) as f:
            self.assertIn("FREMD-TOKEN", f.read())

    def test_status(self):
        self.assertFalse(M.status(pfad=self.p)["belegt"])              # kein Lockfile -> frei
        with M.gpu_lock("nomic", pfad=self.p):
            st = M.status(pfad=self.p)
            self.assertTrue(st["belegt"])
            self.assertEqual(st["modell"], "nomic")
            self.assertEqual(st["pid"], os.getpid())
        self.assertFalse(M.status(pfad=self.p)["belegt"])              # nach Freigabe frei


if __name__ == "__main__":
    unittest.main(verbosity=2)
