# -*- coding: utf-8 -*-
"""agent_controle.py — Grand Planificateur 5.0 : agent de CONTRÔLE de l'efficacité des instances. Sans LLM.

Rôle : vérifier en continu que le nombre d'instances d'agent-compétences ne fait pas PERDRE en efficacité.
  - toutes les 30 s : relève, pour chaque famille, combien d'instances vivent et travaillent réellement
    (agents\\<famille>\\etat\\controle\\echantillons.jsonl) ;
  - toutes les 5 min : reprend la logique de mesure_efficacite.py (débit par nombre d'instances, accélération,
    efficacité, saturation du LLM, attente du verrou) et écrit agents\\<famille>\\etat\\controle\\efficacite.json et
    rapport_efficacite.md ; le tableau de bord affiche le verdict.
Il ne décide rien à votre place : il ne démarre ni n'arrête aucune instance, il avertit et recommande.
Il ne MODIFIE ni competences.json ni les fichiers d'état des agents : il ne lit que leurs signes de vie et taches.jsonl.

Usage :  python agent_controle.py [--racine .] [--periode 30] [--analyse 300] [--fenetre-h 24] [--une-fois]
Arrêt propre : Ctrl+C, SIGTERM, ou fichier controle\\arret.txt à la racine.
"""
import argparse
import json
import os
import signal
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
if str(DOSSIER) not in sys.path:
    sys.path.insert(0, str(DOSSIER))
import mesure_efficacite as me

ARRET = False


def ecrire_atomique(chemin, texte):
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=chemin.name, suffix=".tmp", dir=str(chemin.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texte)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, chemin)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def familles(racine):
    """{nom_famille: dossier d'état partagé} pour chaque agents/<famille>/competences.json."""
    out = {}
    base = Path(racine) / "agents"
    for d in sorted(base.iterdir()) if base.exists() else []:
        if (d / "competences.json").exists():
            out[d.name] = d / "etat"
    return out


def seuil_age(etat, maintenant):
    """Un signe de vie plus vieux que cela = instance morte. Au moins 4 min, ou 3 tâches typiques (appels longs sur CPU)."""
    durees = [t["duree_s"] for t in me.lire_jsonl(etat / "taches.jsonl", maintenant - 3600) if t.get("ok", True) and t.get("duree_s")]
    return max(240.0, 3.0 * statistics.median(durees)) if durees else 240.0


def echantillonner_famille(etat, maintenant=None):
    maintenant = maintenant or time.time()
    ech = me.echantillonner(etat, maintenant, seuil_age(etat, maintenant))
    if ech["vivantes"] or (etat / "controle" / "echantillons.jsonl").exists():     # inutile de tracer une famille jamais lancée
        chemin = etat / "controle" / "echantillons.jsonl"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        with chemin.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ech) + "\n")
    return ech


def elaguer(etat, jours=7):
    """Garde 7 jours d'échantillons et de tâches (les fichiers ne grossissent pas sans fin)."""
    limite = time.time() - jours * 86400
    for nom in (etat / "controle" / "echantillons.jsonl", etat / "taches.jsonl"):
        if nom.exists() and nom.stat().st_size > 2_000_000:
            gardes = me.lire_jsonl(nom, limite, max_octets=10**9)
            ecrire_atomique(nom, "".join(json.dumps(x) + "\n" for x in gardes))


def analyser_famille(nom, etat, fenetre_h, params=None):
    res = me.mesurer_famille(etat, fenetre_h, params=params)
    ecrire_atomique(etat / "controle" / "efficacite.json", json.dumps(res, ensure_ascii=False, indent=2))
    ecrire_atomique(etat / "controle" / "rapport_efficacite.md", me.rapport_markdown(res, nom))
    return res


def demander_arret(*_):
    global ARRET
    ARRET = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grand Planificateur — agent de contrôle de l'efficacité")
    ap.add_argument("--racine", type=Path, default=DOSSIER)
    ap.add_argument("--periode", type=int, default=30, help="Secondes entre deux relevés du nombre d'instances")
    ap.add_argument("--analyse", type=int, default=300, help="Secondes entre deux analyses")
    ap.add_argument("--fenetre-h", type=float, default=24, help="Fenêtre d'analyse en heures")
    ap.add_argument("--chauffe-s", type=int, default=me.PARAMS["chauffe_s"], help="Secondes ignorées après chaque changement du nombre d'instances")
    ap.add_argument("--min-taches", type=int, default=me.PARAMS["min_taches"], help="Tâches minimum par palier pour conclure")
    ap.add_argument("--min-minutes", type=int, default=me.PARAMS["min_minutes"], help="Minutes minimum par palier pour conclure")
    ap.add_argument("--seuil-gain", type=float, default=me.PARAMS["seuil_gain"], help="Gain minimum d'une instance en plus (0.10 = +10 %%)")
    ap.add_argument("--une-fois", action="store_true", help="Un relevé + une analyse, puis arrêt")
    a = ap.parse_args(argv)
    params = {"chauffe_s": a.chauffe_s, "min_taches": a.min_taches, "min_minutes": a.min_minutes, "seuil_gain": a.seuil_gain}
    pc = a.racine / "controle"
    pc.mkdir(parents=True, exist_ok=True)
    (pc / "arret.txt").unlink(missing_ok=True)
    signal.signal(signal.SIGINT, demander_arret)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, demander_arret)
    print("Agent de contrôle (5.0) — relevé toutes les %d s, analyse toutes les %d s. Aucune action sur les agents." % (a.periode, a.analyse))
    derniers, derniere_analyse, dernier_elagage = {}, 0.0, 0.0
    while not ARRET and not (pc / "arret.txt").exists():
        maintenant = time.time()
        fams = familles(a.racine)
        for nom, etat in fams.items():
            try:
                echantillonner_famille(etat, maintenant)
            except Exception as e:
                print("[ERREUR] relevé %s : %r" % (nom, e))
        if maintenant - derniere_analyse >= a.analyse or a.une_fois:
            for nom, etat in fams.items():
                try:
                    res = analyser_famille(nom, etat, a.fenetre_h, params)
                    if derniers.get(nom) != res["verdict"]:
                        print("[%s] %s : %s — %s" % (time.strftime("%H:%M:%S"), nom, res["verdict"].upper(), res["message"]))
                        if res["verdict"] in ("perte", "a_surveiller"):
                            print("    → %s" % res["recommandation"])
                        derniers[nom] = res["verdict"]
                except Exception as e:
                    print("[ERREUR] analyse %s : %r" % (nom, e))
            derniere_analyse = maintenant
        if maintenant - dernier_elagage > 3600:
            for etat in fams.values():
                try:
                    elaguer(etat)
                except Exception:
                    pass
            dernier_elagage = maintenant
        try:
            ecrire_atomique(pc / "etat.json", json.dumps({
                "date": datetime.now().astimezone().isoformat(timespec="seconds"), "statut": "en veille de mesure",
                "periode_s": a.periode, "familles": {n: derniers.get(n) for n in fams}}, ensure_ascii=False))
        except OSError:
            pass
        if a.une_fois:
            break
        for _ in range(max(1, a.periode)):
            if ARRET or (pc / "arret.txt").exists():
                break
            time.sleep(1)
    print("Agent de contrôle arrêté.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
