# -*- coding: utf-8 -*-
"""Demandes de documents par l'agent : détection des manques, garde-fous, boucle complète avec chercheur et faux site officiel."""
import json, shutil, sys, tempfile, threading, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
ICI = Path(__file__).parent; sys.path.insert(0, str(ICI))
import chercheur, courrier, demandes_documents as dd
from test_recuperateur import H

def comp(disc, cycle, i): return {"id": "%s%s%d" % (disc, cycle, i), "discipline": disc, "cycle": cycle}

class FauxAgent:
    def __init__(self, d, comps, restants=0, instance=0):
        self.d, self.instance, self.cfg, self.ref, self._r = d, instance, NS(), NS(liste=comps), restants; self.lisible, self.jr = [], []
    def segments_restants(self): return self._r
    def journal(self, ev, **kw): self.jr.append((ev, kw))
    def ecrire_lisible(self, t): self.lisible.append(t)

class T(unittest.TestCase):
    def setUp(self):
        self.r = Path(tempfile.mkdtemp()); shutil.copy(ICI / "familles.json", self.r / "familles.json")
        (self.r / "chercheur").mkdir(); (self.r / "agents" / "langages" / "etat").mkdir(parents=True)
        self.d = self.r / "agents" / "langages" / "etat"
    def tearDown(self): shutil.rmtree(self.r, ignore_errors=True)
    def entrants(self): return sorted(json.load(open(p, encoding="utf-8"))["contenu"]["requete"] for p in (self.r / "chercheur" / "courrier" / "entrant").glob("*.json"))

    def test_detection(self):
        comps = [comp("Mathématiques", "Cycle 2", i) for i in range(15)] + [comp("Mathématiques", "Cycle 3", i) for i in range(15)]
        self.assertEqual([q for q, _ in dd.detecter(comps, ["Mathématiques", "EPS"])], ["programme EPS", "programme Mathématiques cycle 1"])
        self.assertEqual(dd.detecter(comps[:5], ["Mathématiques"]), [])          # trop peu de compétences : pas de conclusion sur les cycles

    def test_garde_fous(self):
        a = FauxAgent(self.d, [])
        self.assertEqual(len(dd.cycle(a, 1000)), 2); self.assertEqual(len(self.entrants()), 2)       # 2 en attente au maximum
        self.assertEqual(dd.cycle(a, 1000 + 700), [])                                                  # 2 en attente : on n'en ajoute pas
        self.assertEqual(dd.cycle(a, 1000 + 100), [])                                                  # vérification trop rapprochée
        self.assertEqual(dd.cycle(FauxAgent(self.d, [], restants=3), 99999), [])                       # l'agent a encore des segments à lire
        self.assertEqual(dd.cycle(FauxAgent(self.d, [], instance=1), 99999), [])                       # instance secondaire
        self.assertTrue(any("DEMANDE" in t for t in a.lisible))
        # les demandes restées sans réponse expirent (2 h) puis les suivantes partent ; les premières ne sont pas redemandées avant 24 h
        n = dd.cycle(a, 1000 + 3 * 3600); self.assertEqual(len(n), 2); self.assertEqual(len(set(n) | set(self.entrants())), 4)

    def test_hors_grand_planificateur(self):
        iso = Path(tempfile.mkdtemp()); (iso / "etat").mkdir()
        try: self.assertEqual(dd.cycle(FauxAgent(iso / "etat", []), 1000), [])
        finally: shutil.rmtree(iso, ignore_errors=True)

    def test_boucle_complete(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cfg = json.load(open(ICI / "sources_officielles.json", encoding="utf-8"))
            cfg.update(hotes=["127.0.0.1"], pages_de_depart=["http://127.0.0.1:%d/" % srv.server_port], autoriser_http_local=True, pause_s=0)
            json.dump(cfg, open(self.r / "sources_officielles.json", "w", encoding="utf-8"))
            comps = [comp("Français", "Cycle 2", i) for i in range(10)]            # Français présent mais faible : seules les disciplines absentes sont demandées
            a = FauxAgent(self.d, comps)
            dd.cycle(a, 1000)
            self.assertEqual(chercheur.Chercheur(self.r).traiter_courrier(), 2)       # le chercheur traite les 2 demandes (rien de pertinent sur ce faux site)
            dd.cycle(a, 1001)                                                         # l'agent lit les réponses
            reg = json.load(open(self.d / "demandes_documents.json", encoding="utf-8"))["demandes"]
            self.assertTrue(all(v["issue"] == "aucun" for v in reg.values()), reg)    # « aucun PDF correspondant » : pas de redemande avant 7 jours
            suite = dd.cycle(a, 1000 + 1200); self.assertEqual(len(suite), 2)         # mais d'autres disciplines manquantes peuvent partir
            self.assertFalse(set(suite) & {v["requete"] for v in reg.values()})
        finally: srv.shutdown()

if __name__ == "__main__": unittest.main(verbosity=2)
