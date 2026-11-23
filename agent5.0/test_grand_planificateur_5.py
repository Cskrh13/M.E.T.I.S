# -*- coding: utf-8 -*-
"""test_grand_planificateur_5.py — le tableau de bord (process racine) lance, compte et retire des instances.

Travaille dans un dossier temporaire (copie des scripts + mini-référentiel), agents lancés avec --sans-llm :
ni Ollama ni donnée réelle. Lancement :  python test_grand_planificateur_5.py      Attendu : OK
"""
import json, shutil, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))
import grand_planificateur as gp

SCRIPTS = ["agent4_2.py", "agent4_2_core.py", "ollama_client.py", "partage.py", "agent_controle.py", "mesure_efficacite.py", "aiguilleur.py", "chercheur.py", "courrier.py", "lis.py", "migration_familles.py", "familles.json", "modele_competences.json"]


def attendre(cond, delai=25):
    t0 = time.time()
    while time.time() - t0 < delai:
        if cond():
            return True
        time.sleep(0.2)
    return False


class TestInstances(unittest.TestCase):
    def setUp(self):
        self.r = Path(tempfile.mkdtemp())
        for n in SCRIPTS:
            shutil.copy(DOSSIER / n, self.r / n)
        with open(DOSSIER / "competences.json", encoding="utf-8") as fh:
            d = json.load(fh)
        d["competences"] = d["competences"][:5]
        (self.r / "agents" / "maths" / "documents").mkdir(parents=True)
        (self.r / "agents" / "maths" / "competences.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        cfg = dict(gp.CONFIG_DEFAUT, actifs=[], max_instances_par_famille=3,
                   options_agent=["--sans-llm", "--attente", "1"], delai_arret_s=20)
        self.chemin = self.r / "grand_planificateur.json"
        self.chemin.write_text(json.dumps(cfg), encoding="utf-8")
        self.sup = gp.Superviseur(self.r, cfg, self.chemin)

    def tearDown(self):
        self.sup.arreter_tout()
        shutil.rmtree(self.r, ignore_errors=True)

    def statut(self):
        return self.sup.statut()

    def config(self):
        with open(self.chemin, encoding="utf-8") as fh:
            return json.load(fh)

    def test_ajout_retrait_et_persistance(self):
        sup = self.sup
        self.assertEqual(sorted(sup.procs), ["aiguilleur", "chercheur", "comp-maths", "controle"])
        self.assertIn("démarré", sup.action("comp-maths", "start"))
        self.assertIn("instance 2", sup.action("comp-maths", "add_instance"))
        self.assertIn("instance 3", sup.action("comp-maths", "add_instance"))
        self.assertIn("maximum atteint", sup.action("comp-maths", "add_instance"))
        self.assertEqual(sorted(sup.procs), ["aiguilleur", "chercheur", "comp-maths", "comp-maths#2", "comp-maths#3", "controle"])
        self.assertTrue(attendre(lambda: (self.r / "agents/maths/etat/instances/2/vivant.json").exists()
                                 and (self.r / "agents/maths/etat/instances/1/vivant.json").exists()
                                 and (self.r / "agents/maths/etat/vivant.json").exists()), "les 3 instances doivent signaler leur vie")
        s = self.statut()
        fam = {f["nom"]: f for f in s["familles"]}["comp-maths"]
        self.assertEqual((fam["instances"], fam["en_marche"], fam["max_instances"]), (3, 3, 3))
        agents = {a["nom"]: a for a in s["agents"]}
        self.assertEqual([agents[n]["numero"] for n in ("comp-maths", "comp-maths#2", "comp-maths#3")], [1, 2, 3])
        self.assertEqual(s["agents_en_marche"], 3)
        # plusieurs processus distincts
        self.assertEqual(len({a["pid"] for a in s["agents"] if a["genre"] == "agent"}), 3)
        # le nombre d'instances est mémorisé pour le prochain lancement
        self.assertEqual(self.config()["instances"], {"comp-maths": 3})
        # retrait : la dernière instance est arrêtée proprement puis disparaît
        self.assertIn("instance 3", sup.action("comp-maths", "remove_instance"))
        self.assertTrue(attendre(lambda: (sup.tick() or True) and "comp-maths#3" not in sup.procs), "instance 3 non retirée")
        self.assertEqual(self.config()["instances"], {"comp-maths": 2})
        self.assertIn("comp-maths", sup.procs)
        sup.action("comp-maths", "remove_instance")
        attendre(lambda: (sup.tick() or True) and "comp-maths#2" not in sup.procs)
        self.assertIn("principale", sup.action("comp-maths", "remove_instance"))
        self.assertEqual(self.config()["instances"], {})
        self.assertIn("inconnue", sup.action("chercheur", "add_instance"))

    def test_relance_retrouve_les_instances(self):
        cfg = dict(self.sup.cfg, instances={"comp-maths": 2}, actifs=["comp-maths"])
        sup2 = gp.Superviseur(self.r, cfg, self.chemin)
        try:
            self.assertEqual(sorted(sup2.procs), ["aiguilleur", "chercheur", "comp-maths", "comp-maths#2", "controle"])
            sup2.demarrer_actifs()
            self.assertTrue(all(sup2.procs[n].vivant for n in ("comp-maths", "comp-maths#2")))
            self.assertFalse(sup2.procs["chercheur"].vivant)
            self.assertTrue(sup2.procs["controle"].vivant, "l'agent de contrôle démarre avec le tableau de bord")
        finally:
            sup2.arreter_tout()

    def test_arret_famille_et_arret_propre(self):
        sup = self.sup
        sup.action("comp-maths", "add_instance")
        sup.action("comp-maths", "start")
        attendre(lambda: all(p.vivant for n, p in sup.procs.items() if n.startswith("comp")))
        self.assertIn("2 instance(s)", sup.action("comp-maths", "stop_famille"))
        self.assertTrue(attendre(lambda: not any(p.vivant for p in sup.procs.values()), 30), "arrêt propre attendu")

    def test_famille_decouverte_apres_migration(self):
        """Sans competences.json le tableau de bord l'explique ; dès qu'il apparaît (migration), la famille est reprise sans relance."""
        r = Path(tempfile.mkdtemp())
        try:
            for n in SCRIPTS:
                shutil.copy(DOSSIER / n, r / n)
            (r / "agents" / "langages" / "documents").mkdir(parents=True)
            cfg = dict(gp.CONFIG_DEFAUT, actifs=[], controle=False)
            sup = gp.Superviseur(r, cfg, r / "gp.json")
            st = sup.statut()
            self.assertEqual(st["familles"], [])
            self.assertEqual(sorted(sup.procs), ["aiguilleur", "chercheur", "controle"])
            self.assertEqual(len(st["diagnostic"]), 1)
            self.assertIn("aiguilleur", st["diagnostic"][0])
            self.assertIn("agents/langages", st["diagnostic"][0])
            (r / "agents" / "langages" / "competences.json").write_text(json.dumps({"competences": []}), encoding="utf-8")
            sup.tick()
            self.assertIn("comp-langages", sup.procs)
            st = sup.statut()
            self.assertEqual([f["nom"] for f in st["familles"]], ["comp-langages"])
            self.assertEqual(st["diagnostic"], [])
            ctl = next(a for a in st["agents"] if a["genre"] == "controle")
            self.assertIn("description", ctl)
            ch = next(a for a in st["agents"] if a["genre"] == "chercheur")
            self.assertIn("COPIE", ch["description"])
        finally:
            sup.arreter_tout()
            shutil.rmtree(r, ignore_errors=True)

    def test_http(self):
        sup = self.sup
        srv = ThreadingHTTPServer(("127.0.0.1", 0), gp.fabriquer_handler(sup))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            page = urllib.request.urlopen(base + "/").read().decode("utf-8")
            self.assertIn("add_instance", page)
            self.assertIn("5.0", page)
            def post(corps):
                req = urllib.request.Request(base + "/api/action", data=json.dumps(corps).encode(), headers={"Content-Type": "application/json"})
                try:
                    return 200, json.loads(urllib.request.urlopen(req).read())
                except urllib.error.HTTPError as e:
                    return e.code, None
            self.assertEqual(post({"token": "faux", "nom": "comp-maths", "action": "add_instance"})[0], 403)
            self.assertEqual(len([n for n in sup.procs if n.startswith("comp")]), 1)
            code, rep = post({"token": sup.token, "nom": "comp-maths", "action": "add_instance"})
            self.assertEqual(code, 200)
            self.assertIn("instance 2", rep["message"])
            st = json.loads(urllib.request.urlopen(base + "/api/status").read())
            self.assertEqual(st["version"], "5.0")
            self.assertEqual({f["nom"]: f["instances"] for f in st["familles"]}["comp-maths"], 2)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
