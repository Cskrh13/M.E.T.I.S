# -*- coding: utf-8 -*-
"""test_agent4_2_parallele.py — vérifie le fonctionnement PARALLÈLE de l'agent 4.2 SANS Ollama.

Il travaille sur une COPIE temporaire de competences.json (vos fichiers ne sont jamais touchés) avec un
faux LLM (réponses fabriquées, 0,4 s par appel). Lancement :
    python test_agent4_2_parallele.py
Résultat attendu : « TOUS LES TESTS PASSENT ».
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))
import ollama_client
import agent4_2_core as core

DELAI_FAUX = 0.4
appels = []
reserves = []
_orig_reserver = core.Biome.reserver_affinage


def _reserver_trace(self):
    r = _orig_reserver(self)
    if r:
        reserves.append(r["id"])
    return r


core.Biome.reserver_affinage = _reserver_trace


def faux_json(self, systeme, prompt):
    time.sleep(DELAI_FAUX)
    appels.append((threading.current_thread().name, time.time(), prompt))
    if "TEXTE :" in prompt:
        lignes = [l.strip() for l in prompt.split("TEXTE :\n", 1)[1].splitlines() if len(l.strip()) > 40]
        def mots(l):      # intitulé bien distinct d'une ligne à l'autre (sinon vu comme doublon)
            n = int(l.split()[1]); return "".join(chr(97 + (n * 7 + k * 5 + n // 3) % 26) for k in range(14))
        els = [{"intitule": "Savoir maitriser " + mots(l), "nature": "competence", "cycle": None, "niveau": "CM1",
                "domaine": "Mathématiques", "theme": None, "portee": "scolaire", "citation": l[:45], "certitude": 0.9}
               for l in lignes[:2]]
        return {"elements": els}
    return {"valeurs": {"nature_savoir": {"valeur": "savoir_faire", "certitude": 0.7},
                        "operation_cognitive": {"valeur": "comprendre", "certitude": 0.6}},
            "granularite": {"trop_large": False, "decoupage_suggere": [], "justification": ""},
            "prerequis_suggeres": [], "nouvelles_variables": []}


def lancer(simultanes, extracteurs=1, reviseurs=1, duree=12, texte=True):
    tmp = Path(tempfile.mkdtemp(prefix="test_agent_"))
    comp = tmp / "competences.json"
    shutil.copy2(DOSSIER / "competences.json", comp)
    docs = tmp / "documents"
    docs.mkdir()
    if texte:
        (docs / "essai.txt").write_text("\n".join(
            "Ligne %d : l'élève sait résoudre des problèmes de partage équitable avec des nombres entiers." % i
            for i in range(120)), encoding="utf-8")
    cfg = SimpleNamespace(documents=docs, competences=comp, donnees=tmp / "etat", modele="faux", taille_segment=700,
                          attente=2, duree_cycle=5, appels_heure=100000, periode_audit=3, sans_llm=False,
                          extracteurs=extracteurs, reviseurs=reviseurs)
    arret = threading.Event()
    client = ollama_client.OllamaClient(modele="faux")
    client.disponible = lambda: True
    passerelle = core.PasserelleLLM(client, simultanes, 100000, arret)
    biome = core.Biome(cfg, passerelle, arret)
    travailleurs = core.creer_travailleurs(biome, cfg)
    appels.clear()
    reserves.clear()
    t0 = time.time()
    for t in travailleurs:
        t.start()
    pic = 0
    while time.time() - t0 < duree:
        time.sleep(0.2)
        pic = max(pic, passerelle.stats()["en_cours"])
    arret.set()
    for t in travailleurs:
        t.join(10)
    biome.commit(forcer=True)
    return biome, passerelle, tmp, comp, pic, len(appels)


def verifier(cond, msg):
    print(("  OK   " if cond else "  ÉCHEC ") + msg)
    if not cond:
        verifier.echecs += 1


verifier.echecs = 0
core.OllamaClient = ollama_client.OllamaClient
ollama_client.OllamaClient.demander_json = faux_json

print("Test 1 : 1 appel LLM simultané (référence)")
b1, p1, tmp1, comp1, pic1, n1 = lancer(1)
print("   appels : %d, pic simultané : %d" % (n1, pic1))
print("Test 2 : 2 agents LLM, 2 appels simultanés")
b2, p2, tmp2, comp2, pic2, n2 = lancer(2)
print("   appels : %d, pic simultané : %d" % (n2, pic2))
verifier(pic1 == 1, "avec 1 créneau, jamais plus d'un appel à la fois")
verifier(pic2 == 2, "avec 2 créneaux, deux agents appellent réellement en même temps")
verifier(n2 > 1.5 * n1, "le débit augmente quand le LLM le permet (%d → %d appels)" % (n1, n2))

d = json.loads(comp2.read_text(encoding="utf-8"))
orig = json.loads((DOSSIER / "competences.json").read_text(encoding="utf-8-sig"))
ids = [c["id"] for c in d["competences"]]
nouv = [c for c in d["competences"] if c["id"].startswith("auto_")]
verifier(len(ids) == len(set(ids)), "aucun identifiant dupliqué dans competences.json (%d compétences)" % len(ids))
verifier(set(c["id"] for c in orig["competences"]) <= set(ids), "aucune compétence existante supprimée")
verifier(all(c.get("statut") == "propose" and c["provenance"].get("citation") for c in nouv),
         "toute nouvelle compétence est « propose » avec citation")
etat = json.loads((tmp2 / "etat" / "etat.json").read_text(encoding="utf-8"))
f = etat["fichiers"]["essai.txt"]
verifier(f["statut"] in ("traite", "en_cours") and f.get("sautes", []) == [], "document analysé sans segment perdu")
nb_aff = sum(1 for c in d["competences"] if c.get("affinage"))
nb_aff0 = sum(1 for c in orig["competences"] if c.get("affinage"))
verifier(nb_aff - nb_aff0 >= 5, "le réviseur a affiné %d compétences pendant que l'extracteur lisait" % (nb_aff - nb_aff0))
nouv_run = [c for c in nouv if c["id"] not in {x["id"] for x in orig["competences"]}]
verifier(len(nouv_run) >= 5, "l'extracteur a créé %d nouvelles compétences pendant ce temps" % len(nouv_run))
agents = json.loads((tmp2 / "etat" / "biome" / "agents.json").read_text(encoding="utf-8"))["agents"]
verifier({a["role"] for a in agents} == {"extracteur", "reviseur", "auditeur", "greffier"}, "4 agents dans le biome")
viv = json.loads((tmp2 / "etat" / "vivant.json").read_text(encoding="utf-8"))
verifier("agents" in viv and viv["llm"]["simultanes_autorises"] == 2, "signe de vie par agent écrit pour le tableau de bord")
verifier((tmp2 / "etat" / "constats.json").exists(), "l'auditeur a produit un audit sans LLM")
props = [json.loads(l) for l in (tmp2 / "etat" / "propositions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
verifier(len({p["id"] for p in props}) == len(props), "propositions sans doublon d'identifiant")
verifier(any(p.get("agent") for p in props), "chaque proposition garde l'agent auteur")

print("Test 3 : 2 extracteurs + 2 réviseurs ne se marchent pas dessus")
b3, p3, tmp3, comp3, pic3, n3 = lancer(3, extracteurs=2, reviseurs=2, duree=10)
d3 = json.loads(comp3.read_text(encoding="utf-8"))
ids3 = [c["id"] for c in d3["competences"]]
verifier(len(ids3) == len(set(ids3)), "pas de doublon d'id avec 2 extracteurs (%d compétences)" % len(ids3))
f3 = json.loads((tmp3 / "etat" / "etat.json").read_text(encoding="utf-8"))["fichiers"]["essai.txt"]
segs = [p.split("TEXTE :\n", 1)[1] for (_, _, p) in appels if "TEXTE :" in p]
verifier(len(segs) == len(set(segs)) and len(segs) >= 10, "aucun segment envoyé deux fois au LLM (%d segments, 2 extracteurs)" % len(segs))
verifier(len(reserves) == len(set(reserves)) and len(reserves) >= 10,
         "aucune compétence réservée deux fois (%d affinages, 2 réviseurs)" % len(reserves))
auteurs = {c["affinage"]["agent"] for c in d3["competences"] if c.get("affinage")}
verifier({"agent-0003", "agent-0004"} <= auteurs, "les deux réviseurs ont chacun travaillé (%s)" % sorted(auteurs))

print("Test 4 : reprise après arrêt (rien n'est refait, rien n'est perdu)")
cfg_dir = tmp2
b4 = core.Biome(SimpleNamespace(documents=tmp2 / "documents", competences=comp2, donnees=tmp2 / "etat", modele="faux",
                                taille_segment=700, attente=2, duree_cycle=5, appels_heure=100000, periode_audit=3,
                                sans_llm=False, extracteurs=1, reviseurs=1), p2, threading.Event())
verifier(len(b4.ref.liste) == len(d["competences"]), "competences.json relu à l'identique après redémarrage")
verifier(len(b4.agents) == 4 and b4.agents[0]["id"] == "agent-0001", "les agents et leur histoire sont conservés")

for t in (tmp1, tmp2, tmp3):
    shutil.rmtree(t, ignore_errors=True)
print()
print("TOUS LES TESTS PASSENT" if not verifier.echecs else "%d ÉCHEC(S)" % verifier.echecs)
sys.exit(1 if verifier.echecs else 0)
