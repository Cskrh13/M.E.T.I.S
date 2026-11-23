# -*- coding: utf-8 -*-
"""Tests E0 (faux LLM, copie temporaire : aucune donnée réelle touchée)."""
import json, shutil, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent))
import agent4_2_core as core
from ollama_client import OllamaClient, LLMError

PHRASE = "L'élève sait compter de dix en dix jusqu'à cent sans erreur."
SRC = Path(__file__).parent / "competences.json"

class FauxLLM:
    modele = "faux"; timeout = 30
    def __init__(self): self.derniere_stats = {}; self.appels = 0
    def disponible(self): return True
    def demander_json(self, systeme, prompt):
        self.appels += 1
        self.derniere_stats = {"jetons_prompt": 500, "jetons_reponse": 80, "duree_prompt_s": 2.0,
                               "duree_generation_s": 5.0, "duree_totale_s": 8.0}
        if "extraction" in systeme:
            return {"elements": [{"nature": "competence", "intitule": "Compter de dix en dix jusqu'à cent",
                                  "citation": PHRASE, "niveau": "CP", "cycle": "Cycle 2", "certitude": 0.9}]}
        return {"valeurs": {}, "granularite": {"trop_large": False}, "prerequis_suggeres": [], "nouvelles_variables": []}

class TestE0(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        (self.t / "documents").mkdir()
        (self.t / "documents" / "demo.txt").write_text("Page de démonstration. " + PHRASE + " Fin.", encoding="utf-8")
        shutil.copy(SRC, self.t / "competences.json")
        self.cfg = SimpleNamespace(appels_heure=1000, attente=1, competences=self.t / "competences.json",
                                   documents=self.t / "documents", donnees=self.t / "etat", duree_cycle=40,
                                   periode_audit=9999, sans_llm=False, taille_segment=2000)
        self.avant = {c["id"] for c in json.load(open(SRC, encoding="utf-8"))["competences"]}
    def tearDown(self): shutil.rmtree(self.t, ignore_errors=True)

    def test_journal_lisible_et_compteur(self):
        a = core.Agent(self.cfg, FauxLLM())
        a.cycle()
        log = (self.t / "etat" / "journal_lisible.log").read_text(encoding="utf-8")
        self.assertIn("segment", log); self.assertIn("proposée(s)", log)
        self.assertIn("jetons : 500 lus", log); self.assertIn("affinage", log)
        self.assertIn("bilan du cycle", log); self.assertIn("il reste", log)
        viv = json.load(open(self.t / "etat" / "vivant.json", encoding="utf-8"))
        self.assertGreaterEqual(viv["resume"]["jour"]["actions"], 2)
        self.assertIn("total", viv["resume"]["restant"])
        self.assertEqual(viv["resume"]["stats_llm"]["jetons_prompt"], 500)

    def test_aucune_perte(self):
        a = core.Agent(self.cfg, FauxLLM()); a.cycle()
        apres = {c["id"] for c in json.load(open(self.t / "competences.json", encoding="utf-8"))["competences"]}
        self.assertTrue(self.avant <= apres)            # aucun id supprimé
        self.assertEqual(len(apres), len(set(apres)))   # aucun doublon d'id

    def test_echec_journalise(self):
        class Casse(FauxLLM):
            def demander_json(self, s, p): raise LLMError("panne simulée")
        a = core.Agent(self.cfg, Casse()); a.cycle()
        log = (self.t / "etat" / "journal_lisible.log").read_text(encoding="utf-8")
        self.assertIn("ÉCHEC", log); self.assertIn("panne simulée", log)

    def test_stats_ollama_client(self):
        c = OllamaClient()
        c._post = lambda ch, charge, to: {"message": {"content": "{\"a\":1}"}, "prompt_eval_count": 12,
                                         "eval_count": 7, "total_duration": 3_000_000_000,
                                         "prompt_eval_duration": 1_000_000_000, "eval_duration": 2_000_000_000}
        self.assertEqual(c.demander_json("s", "p"), {"a": 1})
        self.assertEqual(c.derniere_stats["jetons_prompt"], 12)
        self.assertEqual(c.derniere_stats["duree_generation_s"], 2.0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
