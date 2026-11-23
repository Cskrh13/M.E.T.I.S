# -*- coding: utf-8 -*-
"""Aiguilleur : sans competences.json à la racine, des documents déposés créent et alimentent les bons agents. Aucun LLM."""
import json, shutil, sys, tempfile, unittest
from pathlib import Path
ICI = Path(__file__).parent; sys.path.insert(0, str(ICI))
import aiguilleur, chercheur

MATHS = "Mathématiques. Nombres et calcul : addition, soustraction, multiplication, nombres entiers, mathématiques et problèmes de mathématiques. " * 4
FR = "Français. Lecture, écriture, grammaire, orthographe : le français et la culture de l’écrit, langage oral, écriture. " * 4
MIXTE = MATHS * 3 + FR
VIDE = "Un texte sans aucun rapport avec les disciplines scolaires."

class T(unittest.TestCase):
    def setUp(self):
        self.r = Path(tempfile.mkdtemp())
        for f in ("familles.json", "modele_competences.json"): shutil.copy(ICI / f, self.r / f)
        (self.r / "documents").mkdir()
        for n, t in (("maths.txt", MATHS), ("francais.txt", FR), ("mixte.txt", MIXTE), ("vide.txt", VIDE)):
            (self.r / "documents" / n).write_text(t, encoding="utf-8")
        chercheur.Chercheur(self.r, distribuer_actif=False).mettre_a_jour()
    def tearDown(self): shutil.rmtree(self.r, ignore_errors=True)

    def test_chaine_complete(self):
        self.assertFalse((self.r / "competences.json").exists())
        ai = aiguilleur.Aiguilleur(self.r); ai.passe()
        ag = self.r / "agents"
        self.assertTrue((ag / "mathematiques" / "documents" / "maths.txt").exists())
        self.assertTrue((ag / "langages" / "documents" / "francais.txt").exists())
        self.assertTrue((ag / "mathematiques" / "documents" / "mixte.txt").exists() and (ag / "langages" / "documents" / "mixte.txt").exists())
        self.assertTrue((ag / "transversal" / "documents" / "vide.txt").exists())              # aucun indice : famille par défaut
        for f in ("mathematiques", "langages", "transversal"):
            d = json.load(open(ag / f / "competences.json", encoding="utf-8"))
            self.assertEqual(d["competences"], []); self.assertIn("_MODE_EMPLOI", d); self.assertEqual(d["metadata"]["famille"], f)
            from agent4_2_core import Referentiel; Referentiel(d)                                 # le noyau accepte le fichier
        self.assertFalse((ag / "monde").exists())                                              # pas de famille inutile
        ai.passe(); self.assertEqual(len(list((ag / "mathematiques" / "documents").iterdir())), 2)   # idempotent
        # un fichier existant n'est jamais écrasé
        p = ag / "mathematiques" / "competences.json"; d = json.load(open(p)); d["competences"].append({"id": "x"}); json.dump(d, open(p, "w"))
        aiguilleur.Aiguilleur(self.r).passe(); self.assertEqual(len(json.load(open(p))["competences"]), 1)

    def test_archivage_quand_tout_est_traite(self):
        ai = aiguilleur.Aiguilleur(self.r); ai.passe(); sha = ai.reg["mixte.txt"]["sha"]
        for f in ("mathematiques",):                                                          # un seul des deux agents a fini
            (self.r / "agents" / f / "etat").mkdir(exist_ok=True)
            json.dump({"fichiers": {"mixte.txt": {"statut": "traite", "sha256": sha}}}, open(self.r / "agents" / f / "etat" / "etat.json", "w"))
        ai.passe(); self.assertTrue((self.r / "documents" / "mixte.txt").exists())
        json.dump({"fichiers": {"mixte.txt": {"statut": "traite", "sha256": sha}}}, open(self.r / "agents" / "langages" / "etat" / "etat.json", "w"))
        ai.passe()
        self.assertFalse((self.r / "documents" / "mixte.txt").exists()); self.assertTrue((self.r / "documents" / "_traites" / "mixte.txt").exists())
        self.assertTrue((self.r / "agents" / "langages" / "documents" / "mixte.txt").exists())   # la copie de travail reste

    def test_nouvelle_famille_ajoutee_a_chaud(self):
        import grand_planificateur as g
        s = g.Superviseur(self.r, dict(g.CONFIG_DEFAUT, actifs=[], aiguilleur=False), self.r / "gp.json")
        print(s.action("Sciences du vivant", "ajouter_famille", {"disciplines": "Vivant, Sciences et technologie"}))
        self.assertIn("sciences_du_vivant", json.load(open(self.r / "familles.json", encoding="utf-8"))["familles"])
        (self.r / "documents" / "bio.txt").write_text("Sciences et technologie. Le vivant, le vivant et les sciences et technologie. " * 6, encoding="utf-8")
        chercheur.Chercheur(self.r, distribuer_actif=False).mettre_a_jour(); aiguilleur.Aiguilleur(self.r).passe()
        self.assertTrue((self.r / "agents" / "sciences_du_vivant" / "competences.json").exists())

if __name__ == "__main__": unittest.main(verbosity=2)
