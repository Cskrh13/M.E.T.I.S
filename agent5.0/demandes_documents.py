# -*- coding: utf-8 -*-
"""demandes_documents.py — l'agent-compétences repère ce qui MANQUE à sa famille et demande le document au chercheur (sans LLM).

Un manque est déterministe : 
  - une discipline de la famille (familles.json) n'a AUCUNE compétence dans competences.json  -> « programme <discipline> » ;
  - une discipline bien couverte (>= 20 compétences) n'a rien dans l'un des cycles 1, 2, 3     -> « programme <discipline> cycle n ».
Garde-fous : seulement quand l'agent n'a plus aucun segment à lire, seulement l'instance principale, au plus 2 demandes en attente,
un sujet n'est redemandé qu'après un délai (24 h sans réponse, 7 jours si réponse ou si rien trouvé), une vérification toutes les 10 min.
La demande est un message « demande_document » dans le courrier du chercheur, qui ne télécharge que depuis les sites officiels
autorisés (recuperateur.py) ; sa réponse revient dans le courrier de la famille et est lue ici.
"""
import collections, json, os, sys, time, unicodedata
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import courrier

INTERVALLE_S, MAX_ATTENTE, MIN_COMP = 600, 2, 20
DELAIS_H = {"sans_reponse": 24, "aucun": 168, "ok": 168, "refuse": 168}
CYCLES = ("Cycle 1", "Cycle 2", "Cycle 3")

def norm(t):
    t = unicodedata.normalize("NFD", str(t or "").lower().replace("’", "'"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn")

def detecter(competences, disciplines):
    """[(requete, raison)] — les disciplines entièrement absentes d'abord."""
    par = collections.defaultdict(collections.Counter)
    for c in competences:
        if c.get("statut") != "rejete": par[norm(c.get("discipline"))][c.get("cycle")] += 1
    absentes, cycles = [], []
    for d in disciplines:
        cy = par.get(norm(d))
        if not cy: absentes.append(("programme %s" % d, "aucune compétence en %s" % d))
        elif sum(cy.values()) >= MIN_COMP:
            cycles += [("programme %s %s" % (d, c.lower()), "%s : rien au %s alors que les autres cycles sont couverts" % (d, c)) for c in CYCLES if not cy.get(c)]
    return absentes + cycles

def _ecrire(p, d):
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh: json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def cycle(agent, maintenant=None):
    """Appelé par l'agent à chaque cycle. Renvoie la liste des requêtes déposées (vide le plus souvent)."""
    if agent.instance != 0 or getattr(agent.cfg, "sans_demandes", False): return []
    fam_dir = Path(agent.d).parent; racine = fam_dir.parent.parent; fam = fam_dir.name
    if fam_dir.parent.name != "agents" or not (racine / "chercheur").is_dir(): return []   # agent lancé hors du Grand Planificateur
    t = maintenant if maintenant is not None else time.time()
    p = Path(agent.d) / "demandes_documents.json"
    try:
        with open(p, encoding="utf-8") as fh: reg = json.load(fh)
    except Exception: reg = {"derniere_verif": 0, "demandes": {}}
    dem = reg["demandes"]
    ok, _ = courrier.lire_entrants(fam_dir)                                                  # réponses du chercheur
    ids = {v["id"]: k for k, v in dem.items()}
    for chemin, m in ok:
        if m.get("type") == "reponse" and m.get("re") in ids:
            note = (m.get("contenu") or {}).get("note") or ""
            v = dem[ids[m["re"]]]; v["issue"] = "aucun" if note.startswith("aucun") else "refuse" if "refuse" in note and "telecharge" not in note else "ok"
            v["note"] = note[:200]; courrier.marquer_traite(fam_dir, chemin, m, "lu par l'agent")
            agent.ecrire_lisible("RÉPONSE du chercheur à « %s » : %s" % (v["requete"], note[:160]))
    for v in dem.values():                                                                   # demande restée sans réponse
        if not v.get("issue") and t - v["date"] > 2 * 3600: v["issue"] = "sans_reponse"
    deposees = []
    if t - reg.get("derniere_verif", 0) >= INTERVALLE_S and agent.segments_restants() == 0:
        reg["derniere_verif"] = t
        en_attente = sum(1 for v in dem.values() if not v.get("issue"))
        try:
            with open(racine / "familles.json", encoding="utf-8") as fh: disciplines = json.load(fh)["familles"].get(fam, [])
        except Exception: disciplines = []
        for requete, raison in detecter(agent.ref.liste, disciplines):
            if en_attente >= MAX_ATTENTE: break
            v = dem.get(norm(requete))
            if v and (not v.get("issue") or t - v["date"] < DELAIS_H.get(v["issue"], 24) * 3600): continue
            msg = courrier.message("comp-" + fam, "chercheur", "demande_document", {"requete": requete, "raison": raison}, priorite=3)
            courrier.deposer(racine / "chercheur", msg)
            dem[norm(requete)] = {"id": msg["id"], "requete": requete, "raison": raison, "date": t, "issue": None}
            en_attente += 1; deposees.append(requete)
            agent.journal("demande_document", requete=requete, raison=raison)
            agent.ecrire_lisible("DEMANDE au chercheur : « %s » (%s)" % (requete, raison)); print("[DEMANDE] %s — %s" % (requete, raison), flush=True)
    _ecrire(p, reg)
    return deposees
