# -*- coding: utf-8 -*-
"""migration_familles.py — E4 : découpe competences.json en un fichier par famille (selon familles.json).

Usage :
  python migration_familles.py                 -> SIMULATION : rapport seul, rien n'est écrit
  python migration_familles.py --appliquer     -> écrit agents/<famille>/competences.json (après sauvegarde)
Garanties :
 - le competences.json d'origine n'est JAMAIS modifié ni supprimé (il est copié dans sauvegardes/) ;
 - refus d'écraser un fichier de famille déjà existant (le travail des agents serait perdu) ;
 - contrôle après écriture : mêmes identifiants, mêmes compétences à l'identique, mêmes prérequis, aucun doublon ;
 - écrit consolide/index_ids.json (identifiant -> famille), nécessaire aux prérequis entre familles.
"""
import argparse, datetime, json, os, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import lis

def ecrire_atomique(chemin, donnees):
    chemin = Path(chemin); chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(chemin) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(donnees, fh, ensure_ascii=False, indent=2)
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, chemin)

def decouper(donnees, table, defaut):
    comps = donnees["competences"]
    groupes = {}
    for c in comps:
        groupes.setdefault(table.get(lis.norm(c.get("discipline")), defaut), []).append(c)
    sorties = {}
    for fam, liste in groupes.items():
        ids_dom = {c.get("domaine_id") for c in liste}
        doms = [d for d in (donnees.get("domaines") or []) if not isinstance(d, dict) or d.get("id") in ids_dom]
        sortie = {k: v for k, v in donnees.items() if k not in ("competences", "domaines")}
        meta = dict(sortie.get("metadata") or {}); meta["famille"] = fam; meta["issu_de"] = "competences.json (migration E4)"
        sortie["metadata"] = meta; sortie["domaines"] = doms; sortie["competences"] = liste
        sorties[fam] = sortie
    return sorties

def verifier(original, chemins):
    """Relit les fichiers écrits et compare à l'original. Renvoie la liste des problèmes (vide = tout est identique)."""
    pb = []
    orig = {c["id"]: c for c in original["competences"]}
    vus = {}
    for fam, p in chemins.items():
        d = json.load(open(p, encoding="utf-8"))
        for c in d["competences"]:
            if c["id"] in vus: pb.append("identifiant en double : %s (%s et %s)" % (c["id"], vus[c["id"]], fam))
            vus[c["id"]] = fam
            if c["id"] not in orig: pb.append("identifiant inconnu : %s" % c["id"])
            elif c != orig[c["id"]]: pb.append("compétence modifiée : %s" % c["id"])
    manquants = set(orig) - set(vus)
    if manquants: pb.append("%d compétence(s) perdue(s), ex. %s" % (len(manquants), sorted(manquants)[0]))
    if len(vus) != len(orig): pb.append("effectif différent : %d au lieu de %d" % (len(vus), len(orig)))
    for c in orig.values():
        for p in c.get("prerequis_competences") or []:
            if p not in vus: pb.append("prérequis non résolu : %s -> %s" % (c["id"], p))
    return pb

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--competences", type=Path, default=None)
    ap.add_argument("--racine", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--appliquer", action="store_true")
    a = ap.parse_args(argv)
    src = a.competences or (a.racine / "competences.json")
    if not src.exists():
        print("competences.json introuvable : %s" % src); return 1
    donnees = json.load(open(src, encoding="utf-8"))
    table, defaut = lis.charger_familles(a.racine / "familles.json")
    sorties = decouper(donnees, table, defaut)
    index = {c["id"]: fam for fam, s in sorties.items() for c in s["competences"]}
    croises = sum(1 for fam, s in sorties.items() for c in s["competences"] for p in c.get("prerequis_competences") or [] if index.get(p) not in (None, fam))
    total = sum(len(c.get("prerequis_competences") or []) for c in donnees["competences"])
    lignes = ["Migration par familles — %s" % ("APPLIQUÉE" if a.appliquer else "SIMULATION (rien n'est écrit)"), ""]
    lignes += ["  %-14s %5d compétences" % (f, len(s["competences"])) for f, s in sorted(sorties.items())]
    lignes += ["", "  Prérequis entre deux familles : %d sur %d" % (croises, total)]
    if not a.appliquer:
        print("\n".join(lignes)); print("\nPour écrire les fichiers : python migration_familles.py --appliquer"); return 0
    cibles = {f: a.racine / "agents" / f / "competences.json" for f in sorties}
    deja = [str(p) for p in cibles.values() if p.exists()]
    if deja:
        print("REFUS : ces fichiers existent déjà (le travail des agents serait écrasé) :\n  " + "\n  ".join(deja)); return 2
    sauv = a.racine / "sauvegardes" / ("competences_avant_migration_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    sauv.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, sauv)
    for f, s in sorties.items():
        ecrire_atomique(cibles[f], s)
        for sous in ("documents", "etat"): (cibles[f].parent / sous).mkdir(exist_ok=True)
    ecrire_atomique(a.racine / "consolide" / "index_ids.json", index)
    pb = verifier(donnees, cibles)
    lignes += ["", "Sauvegarde de l'original : %s" % sauv, "Contrôle d'intégrité : " + ("OK — tout est identique à l'original" if not pb else "%d PROBLÈME(S)" % len(pb))]
    lignes += ["  - " + x for x in pb[:20]]
    (a.racine / "rapport_migration.txt").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    print("\n".join(lignes))
    return 0 if not pb else 3

if __name__ == "__main__":
    sys.exit(main())
