# -*- coding: utf-8 -*-
"""Tests E3 : courrier + chercheur, sans LLM, sur dossiers temporaires et documents de démonstration."""
import json, shutil, sys, tempfile, unittest
from pathlib import Path
here = Path(__file__).parent
sys.path.insert(0, str(here))
for cand in (here.parent / "agent4_2_E0", here.parent):          # agent4_2_core.py (lecture des documents)
    if (cand / "agent4_2_core.py").exists(): sys.path.insert(1, str(cand)); break
import courrier, chercheur

MATHS = ("Programme de mathématiques du cycle 3. Mathématiques : nombres et calcul. En mathématiques, l'élève calcule "
         "avec des fractions et des nombres décimaux. Mathématiques, grandeurs et mesures. Mathématiques et résolution de problèmes. " * 3)
LANGAGES = ("Français : lecture et compréhension. Le français, langage oral et écriture. Culture de l'écrit : la fluence de lecture "
            "se travaille par des relectures répétées d'un texte connu. Français, lecture à voix haute. Langage oral. " * 3)
FLOU = "Document administratif sans rapport avec les disciplines. Calendrier, réunions, sorties. " * 5

class T(unittest.TestCase):
    def setUp(self):
        self.r = Path(tempfile.mkdtemp()); (self.r / "documents").mkdir()
        (self.r / "documents" / "maths.txt").write_text(MATHS, encoding="utf-8")
        (self.r / "documents" / "francais.txt").write_text(LANGAGES, encoding="utf-8")
        (self.r / "documents" / "flou.txt").write_text(FLOU, encoding="utf-8")
        self.c = chercheur.Chercheur(self.r); self.res = self.c.mettre_a_jour()
    def tearDown(self): shutil.rmtree(self.r, ignore_errors=True)

    def test_routage_copie_sans_deplacer(self):
        self.assertEqual(self.c.index["documents"]["maths.txt"]["famille"], "mathematiques")
        self.assertEqual(self.c.index["documents"]["francais.txt"]["famille"], "langages")
        self.assertTrue((self.r / "agents" / "mathematiques" / "documents" / "maths.txt").exists())
        self.assertTrue((self.r / "documents" / "maths.txt").exists())            # source intacte
        self.assertEqual((self.r / "documents" / "maths.txt").read_text(encoding="utf-8"), MATHS)

    def test_ambigu_va_dans_a_router(self):
        self.assertIsNone(self.c.index["documents"]["flou.txt"]["famille"])
        self.assertIn("flou.txt", (self.r / "chercheur" / "a_router" / "a_router.csv").read_text(encoding="utf-8-sig"))
        self.assertFalse((self.r / "agents" / "transversal" / "documents" / "flou.txt").exists())

    def test_requete_reponse(self):
        req = courrier.message("comp-langages", "chercheur", "requete",
              {"competence": "x", "question_type": "preciser_variable", "variable": "charge_langagiere",
               "mots_cles": ["fluence", "relectures"], "portee": {"familles": ["langages"]}, "max_passages": 2})
        courrier.deposer(self.r / "chercheur", req)
        self.assertEqual(self.c.traiter_courrier(), 1)
        ok, _ = courrier.lire_entrants(self.r / "agents" / "langages")
        self.assertEqual(len(ok), 1); rep = ok[0][1]
        self.assertEqual(rep["re"], req["id"]); self.assertEqual(rep["type"], "reponse")
        self.assertTrue(rep["contenu"]["passages"]); self.assertLessEqual(len(rep["contenu"]["passages"]), 2)
        p = rep["contenu"]["passages"][0]
        self.assertEqual(p["fichier"], "francais.txt"); self.assertLessEqual(len(p["texte"]), 600)
        self.assertIn("fluence", p["texte"].lower())
        self.assertEqual(courrier.lire_entrants(self.r / "chercheur")[0], [])       # requête déplacée vers traite

    def test_aucun_passage_inventé(self):
        req = courrier.message("comp-langages", "chercheur", "requete", {"mots_cles": ["zorglub"], "max_passages": 3})
        courrier.deposer(self.r / "chercheur", req); self.c.traiter_courrier()
        rep = courrier.lire_entrants(self.r / "agents" / "langages")[0][0][1]
        self.assertEqual(rep["contenu"]["passages"], []); self.assertTrue(rep["contenu"]["note"])

    def test_idempotence_et_atomicite(self):
        req = courrier.message("comp-langages", "chercheur", "requete", {"mots_cles": ["fluence"]})
        f = courrier.deposer(self.r / "chercheur", req); self.c.traiter_courrier()
        courrier.deposer(self.r / "chercheur", req)                                   # même id redéposé (reprise après arrêt)
        self.assertEqual(self.c.traiter_courrier(), 0)                                # pas de second traitement
        entrant = self.r / "chercheur" / "courrier" / "entrant"
        (entrant / "a_moitie.json.tmp").write_text('{"id": "x"', encoding="utf-8")   # fichier non terminé
        (entrant / "casse.json").write_text("{pas du json", encoding="utf-8")
        ok, abimes = courrier.lire_entrants(self.r / "chercheur")
        self.assertEqual(ok, []); self.assertEqual(len(abimes), 1)                   # .tmp ignoré, cassé signalé, rien supprimé
        self.assertTrue((entrant / "casse.json").exists())

    def test_mise_a_jour_incrementale(self):
        r2 = self.c.mettre_a_jour(); self.assertFalse(any(r2.values()))              # rien n'a changé
        (self.r / "documents" / "flou.txt").unlink(); r3 = self.c.mettre_a_jour()
        self.assertEqual(r3["retires"], ["flou.txt"])
        self.assertFalse([p for p in self.c.index["passages"] if p["fichier"] == "flou.txt"])

if __name__ == "__main__": unittest.main(verbosity=2)
