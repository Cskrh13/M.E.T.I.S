# -*- coding: utf-8 -*-
"""test_controle.py — agent de contrôle et mesure d'efficacité, SANS Ollama ni donnée réelle.

Données synthétiques à débit connu : on vérifie que la mesure retrouve les bons chiffres et les bons verdicts
(perte nette, gain faible, saturation du LLM, tout va bien, données insuffisantes), que la chauffe et les trous
sont écartés, et que la chaîne complète (agents -> taches.jsonl -> agent_controle -> tableau de bord) fonctionne.
Lancement :  python test_controle.py      Attendu : OK
"""
import json, shutil, subprocess, sys, tempfile, threading, time, unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))
import mesure_efficacite as me
import agent4_2_core as core

T0 = 1_700_000_000.0


def simuler(phases, t0=T0, type_="affinage", pas=30, taches=None, ech=None):
    """phases : [(N, minutes, débit/h, durée tâche, durée appel LLM)] -> (tâches, échantillons, instant de fin)."""
    taches = [] if taches is None else taches
    ech = [] if ech is None else ech
    t = t0
    for n, minutes, debit, duree, llm in phases:
        fin = t + minutes * 60
        x = t
        while x < fin:
            ech.append({"t": x, "vivantes": n, "actives": n})
            x += pas
        nb = int(debit * minutes / 60)
        for i in range(nb):
            taches.append({"t": t + (i + 0.5) * (fin - t) / nb, "instance": i % n, "type": type_, "duree_s": duree,
                           "llm_s": llm, "attente_verrou_s": 0.1, "ok": True})
        t = fin
    return taches, ech, t


def palier(res, n, typ="affinage"):
    return next(p for p in res["paliers"] if p["n"] == n)["types"][typ]


class TestAnalyse(unittest.TestCase):
    def test_perte_nette_a_trois(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 110, 22, 20), (3, 30, 95, 30, 28)])
        r = me.analyser(ta, ech, fin + 5)
        self.assertEqual(r["verdict"], "perte")
        self.assertEqual(r["n_actuel"], 3)
        v = r["par_type"]["affinage"]
        self.assertEqual((v["reference_n"], v["n_optimal"]), (1, 2))
        self.assertAlmostEqual(palier(r, 1)["debit_h"], 60, delta=2)
        self.assertAlmostEqual(palier(r, 2)["acceleration"], 110 / 60, delta=0.1)
        self.assertAlmostEqual(palier(r, 2)["efficacite"], 110 / 60 / 2, delta=0.04)
        self.assertLess(palier(r, 3)["gain_vs_precedent"], -0.05)
        self.assertIn("perte_nette", [a["code"] for a in palier(r, 3)["alertes"]])
        self.assertIn("Repassez à 2", r["recommandation"])
        self.assertIn("Perte", r["message"])

    def test_saturation_llm(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 62, 40, 36)])
        r = me.analyser(ta, ech, fin + 5)
        codes = [a["code"] for a in palier(r, 2)["alertes"]]
        self.assertIn("gain_faible", codes)
        self.assertIn("saturation_llm", codes)
        self.assertEqual(r["verdict"], "a_surveiller")
        self.assertIn("OLLAMA_NUM_PARALLEL", json.dumps(r, ensure_ascii=False))
        self.assertEqual(r["par_type"]["affinage"]["n_optimal"], 1)

    def test_tout_va_bien(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 115, 21, 19), (3, 30, 160, 22, 20)])
        r = me.analyser(ta, ech, fin + 5)
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["par_type"]["affinage"]["n_optimal"], 3)
        self.assertAlmostEqual(palier(r, 3)["efficacite"], (160 / 60) / 3, delta=0.05)
        self.assertEqual(palier(r, 3)["alertes"], [])

    def test_donnees_insuffisantes(self):
        ta, ech, fin = simuler([(2, 30, 80, 20, 18)])
        r = me.analyser(ta, ech, fin + 5)
        self.assertEqual(r["verdict"], "insuffisant")
        self.assertIn("une seule valeur", r["message"])
        ta, ech, fin = simuler([(1, 5, 60, 20, 18), (2, 5, 100, 20, 18)])        # trop court
        r = me.analyser(ta, ech, fin + 5)
        self.assertEqual(r["verdict"], "insuffisant")
        r = me.analyser([], [], T0)
        self.assertEqual(r["verdict"], "insuffisant")

    def test_mesure_en_cours_apres_changement(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 110, 22, 20), (3, 3, 95, 30, 28)])
        r = me.analyser(ta, ech, fin + 5)
        self.assertEqual(r["verdict"], "en_cours")          # N=3 vient de commencer : on ne conclut pas
        self.assertIn("N=3", r["message"])

    def test_chauffe_ecartee(self):
        """Une rafale de tâches juste après un changement de N ne gonfle pas le débit."""
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 110, 22, 20)])
        debut2 = T0 + 30 * 60
        ta += [{"t": debut2 + 1 + i, "instance": 0, "type": "affinage", "duree_s": 5, "llm_s": 5, "attente_verrou_s": 0, "ok": True} for i in range(80)]
        r = me.analyser(ta, ech, fin + 5)
        self.assertAlmostEqual(palier(r, 2)["debit_h"], 110, delta=6)

    def test_trou_ignore(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18)])
        ta2, ech2, fin2 = simuler([(2, 30, 110, 22, 20)], t0=fin + 25 * 60)     # contrôle arrêté 25 min entre les deux
        ta3 = ta + [{"t": fin + 600, "instance": 0, "type": "affinage", "duree_s": 9, "llm_s": 9, "attente_verrou_s": 0, "ok": True}] * 30 + ta2
        r = me.analyser(ta3, ech + ech2, fin2 + 5)
        self.assertAlmostEqual(palier(r, 1)["debit_h"], 60, delta=4)         # les 30 tâches du trou ne comptent pas
        self.assertEqual(palier(r, 1)["taches"], palier(r, 1)["taches"])

    def test_types_separes(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 115, 21, 19)])
        tb, _, _ = simuler([(1, 30, 40, 90, 88), (2, 30, 41, 95, 93)], type_="segment", taches=[], ech=[])
        r = me.analyser(ta + tb, ech, fin + 5)
        self.assertEqual(set(r["par_type"]), {"affinage", "segment"})
        self.assertAlmostEqual(palier(r, 1, "segment")["debit_h"], 40, delta=5)
        self.assertEqual(r["type_dominant"], "affinage")
        self.assertEqual(r["par_type"]["segment"]["n_optimal"], 1)           # les segments n'ont pas accéléré, les affinages si

    def test_erreurs_et_verrou(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 115, 21, 19)])
        for t in ta:
            if t["t"] > T0 + 30 * 60 and int(t["t"]) % 3 == 0:
                t["ok"] = False
            if t["t"] > T0 + 30 * 60:
                t["attente_verrou_s"] = 2.0
        r = me.analyser(ta, ech, fin + 5)
        codes = [a["code"] for a in palier(r, 2)["alertes"]]
        self.assertIn("verrou", codes)
        self.assertIn("erreurs_en_hausse", codes)

    def test_rapport_markdown(self):
        ta, ech, fin = simuler([(1, 30, 60, 20, 18), (2, 30, 110, 22, 20), (3, 30, 95, 30, 28)])
        md = me.rapport_markdown(me.analyser(ta, ech, fin + 5), "maths")
        for mot in ("Contrôle d'efficacité — maths", "Verdict : perte", "| 3 |", "Repassez à 2", "non contrôlée"):
            self.assertIn(mot, md)


class TestReleves(unittest.TestCase):
    def ecrire_vivant(self, d, statut, age_s):
        d.mkdir(parents=True, exist_ok=True)
        date = (datetime.now().astimezone() - timedelta(seconds=age_s)).isoformat(timespec="seconds")
        (d / "vivant.json").write_text(json.dumps({"date": date, "statut": statut}), encoding="utf-8")

    def test_instances_actives(self):
        t = Path(tempfile.mkdtemp())
        try:
            self.ecrire_vivant(t, "travail : affinage x", 10)                                   # active
            self.ecrire_vivant(t / "instances" / "1", "travail terminé pour ce cycle", 5)       # active
            self.ecrire_vivant(t / "instances" / "2", "attente (tâches en cours chez les autres instances)", 5)   # vivante, inactive
            self.ecrire_vivant(t / "instances" / "3", "travail : y", 2000)                      # morte (signe trop ancien)
            self.ecrire_vivant(t / "instances" / "4", "travail : z", 5)
            (t / "instances" / "4" / "arret.txt").write_text("stop")                            # arrêt demandé
            e = me.echantillonner(t)
            self.assertEqual((e["vivantes"], e["actives"]), (3, 2))
        finally:
            shutil.rmtree(t, ignore_errors=True)

    def test_reconstitution_depuis_taches(self):
        ta, _, fin = simuler([(1, 10, 60, 20, 18), (3, 10, 150, 20, 18)])
        ech = me.echantillons_depuis_taches(ta)
        ns = {e["actives"] for e in ech}
        self.assertEqual(max(ns), 3)
        self.assertIn(1, ns)
        self.assertTrue(all(e["reconstitue"] for e in ech))


class TestChaine(unittest.TestCase):
    def test_agents_ecrivent_taches_jsonl(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with open(DOSSIER / "competences.json", encoding="utf-8") as fh:
                d = json.load(fh)
            d["competences"] = [dict(c, prerequis_competences=[], competences_suivantes=[]) for c in d["competences"][:12]]
            comp = tmp / "competences.json"
            comp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            (tmp / "documents").mkdir()

            class Faux:
                modele, timeout, derniere_stats = "faux", 30, {}
                def disponible(self): return True
                def demander_json(self, systeme, prompt):
                    time.sleep(0.02)
                    return {"valeurs": {}, "granularite": {"trop_large": False}, "prerequis_suggeres": [], "nouvelles_variables": []}
            agents = [core.Agent(SimpleNamespace(appels_heure=10**6, attente=1, competences=comp, documents=tmp / "documents",
                                                 donnees=tmp / "etat", duree_cycle=30, periode_audit=9999, sans_llm=False,
                                                 taille_segment=700, instance=k), Faux()) for k in range(2)]
            fils = [threading.Thread(target=lambda a=a: [None for _ in iter(a.cycle, False)]) for a in agents]
            [f.start() for f in fils]; [f.join(60) for f in fils]
            lignes = me.lire_jsonl(tmp / "etat" / "taches.jsonl")
            self.assertEqual(len(lignes), 12)
            for l in lignes:
                self.assertEqual(l["type"], "affinage")
                self.assertTrue(l["ok"])
                self.assertGreater(l["duree_s"], 0)
                self.assertGreater(l["llm_s"], 0)
                self.assertIn("attente_verrou_s", l)
            self.assertEqual({l["instance"] for l in lignes}, {0, 1})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_agent_controle_une_fois_et_tableau_de_bord(self):
        r = Path(tempfile.mkdtemp())
        try:
            for n in ("agent4_2.py", "agent4_2_core.py", "ollama_client.py", "partage.py", "agent_controle.py", "mesure_efficacite.py"):
                shutil.copy(DOSSIER / n, r / n)
            etat = r / "agents" / "maths" / "etat"
            (etat / "controle").mkdir(parents=True)
            (r / "agents" / "maths" / "documents").mkdir()
            (r / "agents" / "maths" / "competences.json").write_text(json.dumps({"competences": []}), encoding="utf-8")
            now = time.time()
            ta, ech, fin = simuler([(1, 20, 60, 20, 18), (2, 20, 110, 22, 20), (3, 20, 95, 30, 28)], t0=now - 61 * 60)
            (etat / "taches.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ta), encoding="utf-8")
            (etat / "controle" / "echantillons.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ech), encoding="utf-8")
            sortie = subprocess.run([sys.executable, str(r / "agent_controle.py"), "--racine", str(r), "--une-fois"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(r))
            self.assertEqual(sortie.returncode, 0, sortie.stderr)
            self.assertIn("PERTE", sortie.stdout)
            with open(etat / "controle" / "efficacite.json", encoding="utf-8") as fh:
                e = json.load(fh)
            self.assertEqual(e["verdict"], "perte")
            self.assertTrue((etat / "controle" / "rapport_efficacite.md").exists())
            # le tableau de bord affiche le verdict
            import grand_planificateur as gp
            cfg = dict(gp.CONFIG_DEFAUT, actifs=[])
            sup = gp.Superviseur(r, cfg, r / "gp.json")
            st = sup.statut()
            fam = {f["nom"]: f for f in st["familles"]}
            self.assertEqual(fam["comp-maths"]["efficacite"]["verdict"], "perte")
            eff = sup.efficacite("comp-maths")
            self.assertEqual(eff["verdict"], "perte")
            self.assertEqual([l["n"] for l in eff["lignes"]], [1, 2, 3])
            self.assertIn("controle", {a["nom"] for a in st["agents"]})
            self.assertIn("v-perte", gp.PAGE)
        finally:
            shutil.rmtree(r, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
