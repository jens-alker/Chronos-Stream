"""Test entschlacke_drive: nach dem DB-Umstieg (Jens 07.08.) plant das Skript das Löschen der OBSOLETEN
gzip-Shards/-Manifeste + überschriebener DB-Versionen — und behält die jüngste `markt_cache__<n>.db` +
unbekannte Fremddateien. Gegen die echte `fundamentals_drive`-DB-Namenskonvention, Drive gemockt (kein Netz)."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = os.path.dirname(_HERE)
for _p in (_SYS, os.path.join(_SYS, "connectors")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entschlacke_drive as E        # noqa: E402
import gdrive                        # noqa: E402


class TestEntschlackung(unittest.TestCase):
    def setUp(self):
        # Ein Drive-Zustand: zwei DB-Versionen (die alte ist Ballast) + Alt-Shards/-Manifeste beider
        # Namespaces (obsoletes Schema) + eine unbekannte Fremddatei (nie anfassen).
        self.dateien = {
            "markt_cache__1.db": "id_db1",                 # überschriebene Alt-DB-Version -> löschen
            "markt_cache__2.db": "id_db2",                 # jüngste DB -> behalten
            "AA__oldhash0.json.gz": "id_sh1",              # Alt-Shard (Fundamentals) -> löschen
            "eod_BB__eodh.json.gz": "id_sh2",              # Alt-Shard (EOD) -> löschen
            "manifest__1.json": "id_man1",                 # Alt-Manifest -> löschen
            "eod_manifest__3.json": "id_man2",             # Alt-EOD-Manifest -> löschen
            "irgendwas_fremdes.txt": "id_fremd",           # unbekannt -> behalten
        }
        self._orig = gdrive.liste_ordner
        gdrive.liste_ordner = lambda at, pid, name_praefix=None: dict(self.dateien)

    def tearDown(self):
        gdrive.liste_ordner = self._orig

    def test_alt_db_und_alt_artefakte_werden_geplant(self):
        namen = {n for n, _ in E.plane_entschlackung("at", "ordner")["loeschen"]}
        self.assertEqual(namen, {"markt_cache__1.db", "AA__oldhash0.json.gz",
                                 "eod_BB__eodh.json.gz", "manifest__1.json", "eod_manifest__3.json"})

    def test_juengste_db_und_fremddatei_bleiben(self):
        namen = {n for n, _ in E.plane_entschlackung("at", "ordner")["loeschen"]}
        self.assertNotIn("markt_cache__2.db", namen)       # jüngste DB -> behalten
        self.assertNotIn("irgendwas_fremdes.txt", namen)   # unbekannte Fremddatei -> nie anfassen

    def test_nur_eine_db_nichts_zu_loeschen(self):
        self.dateien = {"markt_cache__5.db": "id_db"}
        plan = E.plane_entschlackung("at", "ordner")
        self.assertEqual(plan["loeschen"], [])
        self.assertEqual(plan["behalten"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
