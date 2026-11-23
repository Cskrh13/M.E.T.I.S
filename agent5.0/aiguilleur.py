# -*- coding: utf-8 -*-
"""aiguilleur.py — agent SANS LLM : fait démarrer les agents-compétences et leur affecte les documents.

Ce qu'il fait, en boucle :
 1. AMORCER : crée agents/<famille>/competences.json quand une famille en a besoin (un document lui est affecté,
    des documents l'attendent déjà dans agents/<famille>/documents, ou competences.json de la racine contient des compétences
    de cette famille). Point de départ : la partie de competences.json de la racine, sinon modele_competences.json (vide) :
    l'agent construit alors les compétences à partir des documents. Un fichier existant n'est jamais touché.
 2. ROUTER : chaque document déposé dans documents/ (et indexé par le chercheur) est COPIÉ dans agents/<famille>/documents/
    de TOUTES les familles concernées (score >= seuil et >= 30 % du meilleur) ; aucun signal -> famille par défaut.
 3. ARCHIVER : quand tous les agents concernés ont traité le document, il est DÉPLACÉ dans documents/_traites/ (jamais supprimé).
Le Grand Planificateur démarre ensuite tout seul les agents des familles créées.
Usage :  python aiguilleur.py --racine . [--boucle] [--garder-documents]
"""
import argparse, json, os, shutil, sys, time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

EXT = {".pdf", ".txt", ".md", ".docx", ".odt"}
SEUIL, RATIO = 3, 0.3

def lire(p, defaut):
    try:
        with open(p, encoding="utf-8-sig") as fh: return json.load(fh)
    except Exception: return defaut

def ecrire(p, d):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, p)

class Aiguilleur:
    def __init__(self, racine, archiver=True):
        self.r, self.archiver = Path(racine), archiver
        self.me = self.r / "aiguilleur"; self.me.mkdir(parents=True, exist_ok=True)
        self.p_reg, self.p_etat, self.p_log = self.me / "registre.json", self.me / "etat.json", self.me / "journal.log"
        self.reg = lire(self.p_reg, {})
        self.recents = lire(self.p_etat, {}).get("recents", [])

    def journal(self, msg):
        ligne = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        with open(self.p_log, "a", encoding="utf-8") as fh: fh.write(ligne + "\n")
        self.recents = (self.recents + [ligne])[-12:]; print(ligne, flush=True)

    def familles(self):
        f = lire(self.r / "familles.json", {})
        return f.get("familles") or {}, f.get("famille_par_defaut") or "transversal"

    def squelette(self, fam):
        """Compétences de la famille tirées de competences.json (racine) si elles existent, sinon le modèle vide."""
        src = self.r / "competences.json"
        if src.exists():
            try:
                import lis, migration_familles as mig
                table, defaut = lis.charger_familles(self.r / "familles.json")
                s = mig.decouper(lire(src, {"competences": []}), table, defaut).get(fam)
                if s: return s, len(s["competences"])
            except Exception as e:
                self.journal("lecture de competences.json impossible (%r) : modèle vide utilisé" % e)
        for m in (self.r / "modele_competences.json", Path(__file__).with_name("modele_competences.json")):
            if m.exists():
                d = lire(m, None)
                if d and "competences" in d: break
        else:
            d = {"metadata": {"titre": "Référentiel vide"}, "domaines": [], "competences": []}
        d["metadata"] = dict(d.get("metadata") or {}, famille=fam, issu_de="modele vide (aiguilleur)")
        return d, 0

    def amorcer(self, fam, raison):
        cible = self.r / "agents" / fam / "competences.json"
        if cible.exists(): return False
        d, n = self.squelette(fam)
        ecrire(cible, d)
        for sous in ("documents", "etat"): (cible.parent / sous).mkdir(exist_ok=True)
        self.journal("agent « %s » créé (%s ; %d compétence(s) de départ)" % (fam, raison, n))
        return True

    @staticmethod
    def choisir(scores, defaut):
        if not scores: return [defaut], "aucun indice : famille par défaut"
        best = max(scores.values())
        if best < SEUIL: return [defaut], "peu d'indices (score %d) : famille par défaut" % best
        fams = [f for f, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s >= SEUIL and s >= RATIO * best]
        return fams, "scores " + ", ".join("%s %d" % (f, scores[f]) for f in fams)

    def passe(self):
        dep, (table, defaut) = self.r / "documents", self.familles()
        idx = (lire(self.r / "chercheur" / "index" / "index.json", {}) or {}).get("documents", {})
        attente = 0
        for p in sorted(dep.iterdir()) if dep.exists() else []:
            if not p.is_file() or p.suffix.lower() not in EXT: continue
            info, reg = idx.get(p.name), self.reg.get(p.name)
            if reg and reg.get("archive"): reg = None                    # redéposé après archivage : à retraiter
            if not info: attente += 1; continue                          # pas encore indexé par le chercheur
            if reg and reg.get("sha") == info["empreinte"]: continue
            fams, raison = self.choisir(info.get("scores") or {}, defaut)
            for f in fams:
                self.amorcer(f, "document « %s »" % p.name)
                cible = self.r / "agents" / f / "documents"; cible.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, cible / p.name)
            self.reg[p.name] = {"sha": info["empreinte"], "familles": fams, "archive": False, "date": datetime.now().isoformat(timespec="seconds")}
            self.journal("« %s » → %s (%s)" % (p.name, ", ".join(fams), raison))
        for fam in table:                                                # documents déjà posés à la main dans une famille
            d = self.r / "agents" / fam / "documents"
            if d.is_dir() and any(x.suffix.lower() in EXT for x in d.iterdir() if x.is_file()): self.amorcer(fam, "documents déjà présents")
        for nom, reg in list(self.reg.items()):
            if not self.archiver or reg.get("archive") or not (dep / nom).exists(): continue
            faits = []
            for f in reg["familles"]:
                e = lire(self.r / "agents" / f / "etat" / "etat.json", {}).get("fichiers", {}).get(nom) or {}
                faits.append(e.get("statut") == "traite" and e.get("sha256") == reg["sha"])
            if faits and all(faits):
                arch = dep / "_traites"; arch.mkdir(exist_ok=True)
                dest = arch / nom
                if dest.exists(): dest = arch / ("%s_%s%s" % (Path(nom).stem, datetime.now().strftime("%Y%m%d%H%M%S"), Path(nom).suffix))
                shutil.move(str(dep / nom), str(dest)); reg["archive"] = True
                self.journal("« %s » traité par %s → archivé dans documents/_traites/" % (nom, ", ".join(reg["familles"])))
        ecrire(self.p_reg, self.reg)
        ecrire(self.p_etat, {"statut": "en veille" if not attente else "en attente du chercheur (%d document(s) à indexer)" % attente,
                             "date": datetime.now().astimezone().isoformat(timespec="seconds"), "en_attente": attente, "recents": self.recents})

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--racine", default="."); ap.add_argument("--boucle", action="store_true"); ap.add_argument("--garder-documents", action="store_true")
    a = ap.parse_args(argv); ai = Aiguilleur(a.racine, archiver=not a.garder_documents)
    while True:
        if (ai.me / "arret.txt").exists(): break
        try: ai.passe()
        except Exception as e: ai.journal("erreur : %r" % e)
        if not a.boucle: break
        time.sleep(5)
    return 0

if __name__ == "__main__":
    sys.exit(main())
