# -*- coding: utf-8 -*-
"""test_agent5_instances.py — plusieurs instances d'un même agent-compétences, SANS Ollama ni donnée réelle.

Travaille sur une copie réduite de competences.json (dans un dossier temporaire) avec un faux LLM.
  1. fils d'exécution : 3 instances sur le même état (verrou-fichier entre les objets Agent) ;
  2. processus réels : 2 × agent4_2.py --instance k, face à un faux serveur Ollama HTTP.
Dans les deux cas : chaque segment est traité UNE fois, chaque compétence affinée UNE fois, aucun identifiant perdu
ou dupliqué, aucune réservation oubliée, et plusieurs instances ont réellement travaillé.
Lancement :  python test_agent5_instances.py      Attendu : OK
"""
import json, re, shutil, subprocess, sys, tempfile, threading, time, unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))
import agent4_2_core as core
from partage import VerrouFichier

NB_LIGNES = 150
TEXTE = "\n".join("Ligne %02d : l'élève sait expliquer sa démarche numéro %02d avec ses propres mots." % (i, i) for i in range(NB_LIGNES))


def lire(chemin):
    with open(chemin, encoding="utf-8") as fh:
        return json.load(fh)


def mini_referentiel(dest, n=40):
    d = lire(DOSSIER / "competences.json")
    d["competences"] = [dict(c, prerequis_competences=[], competences_suivantes=[]) for c in d["competences"][:n]]
    dest.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return {c["id"] for c in d["competences"]}


def reponse_llm(systeme, prompt, journal, instance):
    """Même logique pour le faux client (fils) et le faux serveur (processus)."""
    time.sleep(0.03)
    if "extraction" in systeme:
        texte = prompt.split("TEXTE :\n", 1)[1]
        nums = re.findall(r"Ligne (\d+)", texte)
        journal.append(("seg", tuple(nums), instance))
        els = []
        for n in nums[:1]:        # une compétence par segment, intitulé distinct d'un segment à l'autre
            mots = "".join(chr(97 + (int(n) * 7 + k * 5 + int(n) // 3) % 26) for k in range(14))
            els.append({"intitule": "Savoir expliquer " + mots, "nature": "competence", "niveau": "CM1", "cycle": None,
                        "domaine": "Mathématiques", "citation": "Ligne %s : l'élève sait expliquer" % n, "certitude": 0.9})
        return {"elements": els}
    m = re.search(r"« (.*?) » \(niveau (.*?), domaine", prompt)
    journal.append(("aff", m.groups() if m else prompt[:60], instance))
    return {"valeurs": {}, "granularite": {"trop_large": False}, "prerequis_suggeres": [], "nouvelles_variables": []}


def verifier_resultat(tc, tmp, comp, avant, journal):
    segs = [e for e in journal if e[0] == "seg"]
    aff = [e for e in journal if e[0] == "aff"]
    doubles = [k for k, v in Counter(e[1] for e in segs).items() if v > 1]
    tc.assertEqual(doubles, [], "segments traités plusieurs fois")
    tc.assertEqual(len(segs), len({e[1] for e in segs}))
    d = lire(comp)
    ids = [c["id"] for c in d["competences"]]
    tc.assertEqual(len(ids), len(set(ids)), "identifiants dupliqués")
    tc.assertTrue(avant <= set(ids), "identifiant perdu")
    nouveaux = [c for c in d["competences"] if c["id"] not in avant]
    doubles_aff = [k for k, v in Counter(e[1] for e in aff).items() if v > 1]
    tc.assertEqual(doubles_aff, [], "compétences affinées plusieurs fois")
    sans_affinage = [c["id"] for c in d["competences"] if c.get("statut") != "rejete" and not c.get("affinage")]
    tc.assertEqual(sans_affinage, [], "compétences restées sans affinage")
    etat = lire(tmp / "etat" / "etat.json")
    f = etat["fichiers"]["essai.txt"]
    tc.assertEqual(f["statut"], "traite")
    tc.assertEqual(f["ajouts"], len(nouveaux))
    # chaque segment fournit exactement un élément : tous les segments sont comptés une fois, aucun n'est perdu
    tc.assertEqual(f["ajouts"] + f["deja_presents"] + f["ecartes"], len(segs), "un segment perdu ou compté deux fois")
    tc.assertEqual(etat["reservations"], {}, "réservation oubliée")
    qui = {e[2] for e in journal}
    tc.assertGreaterEqual(len(qui), 2, "une seule instance a travaillé : %s" % qui)
    return len(segs), len(aff), qui


class TestVerrou(unittest.TestCase):
    def test_exclusion_et_reentrance(self):
        t = Path(tempfile.mkdtemp())
        v1, v2 = VerrouFichier(t / "v.lock"), VerrouFichier(t / "v.lock")
        compteur = {"n": 0, "max": 0}

        def travail():
            for _ in range(15):
                with v1 if threading.current_thread().name.endswith("0") else v2:
                    with (v1 if threading.current_thread().name.endswith("0") else v2):   # réentrant
                        compteur["n"] += 1
                        compteur["max"] = max(compteur["max"], compteur["n"])
                        time.sleep(0.002)
                        compteur["n"] -= 1
        fils = [threading.Thread(target=travail, name="t%d" % i) for i in range(4)]
        [f.start() for f in fils]; [f.join() for f in fils]
        self.assertEqual(compteur["max"], 1)
        self.assertFalse((t / "v.lock").exists())
        shutil.rmtree(t, ignore_errors=True)

    def test_verrou_orphelin_repris(self):
        t = Path(tempfile.mkdtemp())
        (t / "v.lock").write_text("9999 0\n")
        import os
        vieux = time.time() - 500
        os.utime(t / "v.lock", (vieux, vieux))
        with VerrouFichier(t / "v.lock", obsolete_s=60):
            pass
        shutil.rmtree(t, ignore_errors=True)


class TestFils(unittest.TestCase):
    def test_trois_instances(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            comp = tmp / "competences.json"
            avant = mini_referentiel(comp)
            (tmp / "documents").mkdir()
            (tmp / "documents" / "essai.txt").write_text(TEXTE, encoding="utf-8")
            journal, verrou = [], threading.Lock()

            class Faux:
                modele, timeout = "faux", 30
                def __init__(self, k): self.k, self.derniere_stats = k, {}
                def disponible(self): return True
                def demander_json(self, systeme, prompt):
                    tmp_j = []
                    r = reponse_llm(systeme, prompt, tmp_j, self.k)
                    with verrou:
                        journal.extend(tmp_j)
                    return r

            agents = []
            for k in range(3):
                cfg = SimpleNamespace(appels_heure=10**6, attente=1, competences=comp, documents=tmp / "documents",
                                      donnees=tmp / "etat", duree_cycle=60, periode_audit=9999, sans_llm=False,
                                      taille_segment=700, instance=k)
                agents.append(core.Agent(cfg, Faux(k)))
            erreurs = []

            def boucle(a):
                try:
                    while a.cycle():
                        pass
                except Exception as e:
                    erreurs.append(repr(e))
            fils = [threading.Thread(target=boucle, args=(a,)) for a in agents]
            [f.start() for f in fils]; [f.join(120) for f in fils]
            self.assertEqual(erreurs, [])
            ns, na, qui = verifier_resultat(self, tmp, comp, avant, journal)
            print("\n  fils : %d segments, %d affinages, instances actives %s" % (ns, na, sorted(qui)))
            for k in (1, 2):
                self.assertTrue((tmp / "etat" / "instances" / str(k) / "vivant.json").exists())
            self.assertTrue((tmp / "etat" / "vivant.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reprise_apres_plantage(self):
        """Une instance morte en laissant des réservations : au redémarrage elle les libère."""
        tmp = Path(tempfile.mkdtemp())
        try:
            comp = tmp / "competences.json"
            mini_referentiel(comp, 5)
            (tmp / "documents").mkdir()
            cfg = SimpleNamespace(appels_heure=10**6, attente=1, competences=comp, documents=tmp / "documents",
                                  donnees=tmp / "etat", duree_cycle=5, periode_audit=9999, sans_llm=True,
                                  taille_segment=700, instance=1)
            class C: modele, timeout = "faux", 30; derniere_stats = {}
            a = core.Agent(cfg, C())
            with a.transaction():
                a.etat["reservations"]["aff:x"] = {"i": 1, "t": time.time()}
                a.etat["reservations"]["aff:y"] = {"i": 0, "t": time.time()}
            a.liberer_mes_reservations()
            self.assertEqual(list(a.etat["reservations"]), ["aff:y"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_etat_42_relu(self):
        """Un etat.json de la 4.2 (pointeur « suivant ») reste utilisable : les segments déjà faits ne sont pas refaits."""
        etat = core.Agent._normaliser_etat({"fichiers": {"a.txt": {"statut": "en_cours", "segments": [1, 2, 3, 4], "suivant": 3}}})
        self.assertEqual(etat["fichiers"]["a.txt"]["faits"], [0, 1, 2])


class TestProcessus(unittest.TestCase):
    def test_deux_processus_et_faux_ollama(self):
        tmp = Path(tempfile.mkdtemp())
        journal, verrou = [], threading.Lock()
        try:
            comp = tmp / "competences.json"
            avant = mini_referentiel(comp)
            (tmp / "documents").mkdir()
            (tmp / "documents" / "essai.txt").write_text(TEXTE, encoding="utf-8")

            class H(BaseHTTPRequestHandler):
                def log_message(self, *a): pass
                def _rep(self, obj):
                    b = json.dumps(obj).encode()
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
                def do_GET(self): self._rep({"models": [{"name": "faux"}]})
                def do_POST(self):
                    corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    systeme, prompt = corps["messages"][0]["content"], corps["messages"][1]["content"]
                    tmp_j = []
                    r = reponse_llm(systeme, prompt, tmp_j, "serveur")
                    with verrou: journal.extend(tmp_j)
                    self._rep({"message": {"content": json.dumps(r)}})
            srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d" % srv.server_address[1]

            procs = []
            for k in range(2):
                cmd = [sys.executable, str(DOSSIER / "agent4_2.py"), "--competences", str(comp), "--documents", str(tmp / "documents"),
                       "--donnees", str(tmp / "etat"), "--instance", str(k), "--modele", "faux", "--ollama-url", url,
                       "--duree-cycle", "60", "--attente", "1", "--une-fois", "--periode-audit", "9999", "--taille-segment", "700"]
                procs.append(subprocess.Popen(cmd, cwd=str(DOSSIER), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8"))
            sorties = [p.communicate(timeout=180)[0] for p in procs]
            srv.shutdown()
            for p, o in zip(procs, sorties):
                self.assertEqual(p.returncode, 0, o[-1500:])
            # l'identité de l'instance est tracée dans la provenance des compétences créées
            d = lire(comp)
            segs = [e for e in journal if e[0] == "seg"]
            self.assertEqual(len(segs), len({e[1] for e in segs}), "segment traité deux fois")
            ids = [c["id"] for c in d["competences"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(avant <= set(ids))
            nouveaux = [c for c in d["competences"] if c["id"] not in avant]
            aff = [e[1] for e in journal if e[0] == "aff"]
            self.assertEqual(len(aff), len(set(aff)), "compétence affinée deux fois")
            self.assertEqual([c["id"] for c in d["competences"] if c.get("statut") != "rejete" and not c.get("affinage")], [])
            etat = lire(tmp / "etat" / "etat.json")
            f = etat["fichiers"]["essai.txt"]
            self.assertEqual(f["statut"], "traite")
            self.assertEqual(f["ajouts"], len(nouveaux))
            self.assertEqual(f["ajouts"] + f["deja_presents"] + f["ecartes"], len(segs), "un segment perdu ou compté deux fois")
            self.assertEqual(etat["reservations"], {})
            self.assertTrue((tmp / "etat" / "vivant.json").exists())
            self.assertTrue((tmp / "etat" / "instances" / "1" / "vivant.json").exists())
            tous_agents = {c.get("provenance", {}).get("agent") for c in nouveaux} | {c.get("affinage", {}).get("agent") for c in d["competences"] if c.get("affinage")}
            print("\n  processus : %d segments, %d affinages ; agents ayant écrit : %s" % (len(segs), len(aff), sorted(a for a in tous_agents if a)))
            self.assertGreaterEqual(len({a for a in tous_agents if a}), 2, "une seule instance a travaillé")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
