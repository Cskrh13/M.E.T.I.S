# -*- coding: utf-8 -*-
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import lis

class T(unittest.TestCase):
    def test_verbes(self):
        self.assertEqual(lis.operation("Comparer des nombres")[0], "comparer")
        self.assertEqual(lis.operation("Savoir comparer deux fractions")[0], "comparer")
        self.assertEqual(lis.operation("En fin d'année : copier trois phrases")[0], "appliquer")
        self.assertEqual(lis.operation("Prendre conscience des lettres muettes")[0], "comprendre")
        self.assertEqual(lis.operation("Zorglubber les trucs")[0], None)
    def test_niveaux_et_familles(self):
        table, defaut = lis.charger_familles(Path(__file__).parent / "familles.json")
        c = {"id": "x", "intitule": "Calculer une somme", "discipline": "Mathématiques", "niveau": "CM1-CM2", "domaine_id": "d", "prerequis_competences": ["y"]}
        r = lis.convertir(c, table, defaut)
        self.assertEqual((r["fam"], r["niv"], r["op"], r["id"]), ("mathematiques", 7.5, "appliquer", "x"))
        c2 = dict(c, discipline=None, niveau="Cycle 3")
        r2 = lis.convertir(c2, table, defaut)
        self.assertEqual((r2["fam"], r2["niv"]), ("transversal", None))
    def test_lecture_seule(self):
        t = Path(tempfile.mkdtemp()); p = t / "c.json"
        p.write_text(json.dumps({"competences": [{"id": "a", "intitule": "Lire un mot", "discipline": "Français", "niveau": "CP", "prerequis_competences": []}]}), encoding="utf-8")
        avant = p.read_bytes(); lis.main(["--competences", str(p), "--sortie", str(t / "o")])
        self.assertEqual(avant, p.read_bytes())
        self.assertEqual(len((t / "o" / "lis.jsonl").read_text(encoding="utf-8").splitlines()), 1)
if __name__ == "__main__": unittest.main(verbosity=2)
