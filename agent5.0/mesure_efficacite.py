# -*- coding: utf-8 -*-
"""mesure_efficacite.py — Grand Planificateur 5.0 : mesure de l'efficacité en fonction du nombre d'instances.

Question posée : « ajouter une instance d'agent-compétences fait-il vraiment avancer plus vite, ou en perd-on ? »

Entrées (écrites par les agents, toutes instances confondues) :
  etat\\taches.jsonl               une ligne par tâche : instant de fin, instance, type (segment / affinage), durée,
                                  durée de l'appel LLM, attente du verrou, réussite
  etat\\controle\\echantillons.jsonl  nombre d'instances vivantes / actives, relevé toutes les ~30 s par l'agent-contrôle
                                  (à défaut, il est reconstitué à partir des tâches : voir echantillons_depuis_taches)

Méthode : on découpe le temps en tronçons où le nombre d'instances actives N est constant (on écarte les premières
minutes après chaque changement : le modèle « chauffe »), puis, POUR CHAQUE TYPE DE TÂCHE séparément (un segment et un
affinage ne se comparent pas), on calcule le débit en tâches réussies par heure pour chaque N.
  accélération = débit(N) / débit(N de référence)          efficacité = accélération / (N / N de référence)
  gain = débit(N) / débit(N précédent) - 1                 (100 % d'efficacité = chaque instance apporte autant que la 1re)
Alertes : perte nette (le débit baisse), gain faible (< seuil), saturation du LLM (les appels s'allongent à peu près comme N :
Ollama les traite l'un après l'autre), attente du verrou élevée, erreurs en hausse.

HONNÊTETÉ : c'est une mesure OBSERVÉE en conditions réelles, pas une expérience contrôlée. La difficulté des tâches et la
charge de la machine varient. Un verdict n'est donné que si chaque palier compte assez de tâches et de minutes ; sinon
le rapport dit « insuffisant » plutôt que de conclure. Bibliothèque standard uniquement.

Usage :  python mesure_efficacite.py --donnees agents\\mathematiques\\etat [--fenetre-h 24] [--json]
"""
import argparse
import bisect
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

PARAMS = {
    "chauffe_s": 120,          # secondes ignorées après chaque changement de N
    "trou_max_s": 300,         # au-delà, on ne sait pas ce qui s'est passé (contrôle arrêté) : tronçon ignoré
    "min_taches": 10,          # tâches réussies minimum par (N, type) pour conclure
    "min_minutes": 10,         # minutes minimum passées à ce N
    "seuil_gain": 0.10,        # une instance en plus doit apporter au moins +10 % de débit
    "tol_perte": 0.05,         # en dessous de -5 % : perte nette
    "seuil_verrou": 0.05,      # attente du verrou > 5 % de la durée d'une tâche
    "seuil_saturation": 0.8,   # durée d'appel multipliée par au moins 80 % du rapport des N : appels mis en file
    "hausse_erreurs": 0.10,    # +10 points de taux d'erreur
}


# ======================================================================
# Lecture
# ======================================================================
def lire_jsonl(chemin, depuis_t=0.0, max_octets=20_000_000):
    chemin = Path(chemin)
    if not chemin.exists():
        return []
    try:
        with open(chemin, "rb") as f:
            f.seek(0, 2)
            taille = f.tell()
            f.seek(max(0, taille - max_octets))
            lignes = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    sortie = []
    for l in lignes:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("t", 0) >= depuis_t:
            sortie.append(d)
    return sortie


def _lire_json(chemin):
    try:
        with open(chemin, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def etat_instances(dossier_etat, maintenant=None, seuil_age_s=240):
    """Instances de la famille, d'après leurs signes de vie. vivante : signe récent et pas d'arrêt demandé ;
    active : vivante ET en train de travailler (une instance qui n'a rien à faire ne produit rien)."""
    dossier_etat = Path(dossier_etat)
    maintenant = maintenant or time.time()
    chemins = [(0, dossier_etat)]
    inst = dossier_etat / "instances"
    if inst.exists():
        for d in sorted(inst.iterdir()):
            if d.is_dir() and d.name.isdigit():
                chemins.append((int(d.name), d))
    res = []
    for k, d in chemins:
        v = _lire_json(d / "vivant.json")
        if not v or not v.get("date"):
            continue
        try:
            age = maintenant - datetime.fromisoformat(v["date"]).timestamp()
        except Exception:
            continue
        statut = str(v.get("statut") or "")
        vivante = age <= seuil_age_s and not (d / "arret.txt").exists()
        res.append({"instance": k, "age_s": round(age, 1), "statut": statut, "vivante": vivante,
                    "active": vivante and statut.startswith("travail")})
    return res


def echantillonner(dossier_etat, maintenant=None, seuil_age_s=240):
    maintenant = maintenant or time.time()
    inst = etat_instances(dossier_etat, maintenant, seuil_age_s)
    return {"t": round(maintenant, 1), "vivantes": sum(i["vivante"] for i in inst), "actives": sum(i["active"] for i in inst)}


def echantillons_depuis_taches(taches, pas=30, demi_fenetre=None):
    """Reconstitution (approximative) quand l'agent-contrôle n'a pas tourné : à chaque instant, N = nombre d'instances
    distinctes ayant terminé une tâche dans les environs."""
    if not taches:
        return []
    ts = sorted(taches, key=lambda x: x["t"])
    durees = [x["duree_s"] for x in ts if x.get("duree_s")]
    fen = demi_fenetre or max(90.0, 2.0 * (statistics.median(durees) if durees else 30.0))
    debut, fin = ts[0]["t"], ts[-1]["t"]
    out, t, i0, i1 = [], debut, 0, 0
    while t <= fin:
        while i0 < len(ts) and ts[i0]["t"] < t - fen:
            i0 += 1
        while i1 < len(ts) and ts[i1]["t"] <= t + fen:
            i1 += 1
        n = len({x["instance"] for x in ts[i0:i1]})
        out.append({"t": round(t, 1), "vivantes": n, "actives": n, "reconstitue": True})
        t += pas
    return out


# ======================================================================
# Analyse
# ======================================================================
def _troncons(echantillons, maintenant, p):
    """[(debut, fin, N)] : tronçons à N constant, chauffe retirée, trous ignorés."""
    s = sorted(echantillons, key=lambda e: e["t"])
    if not s:
        return []
    bornes = [(s[i]["t"], s[i + 1]["t"], s[i]["actives"]) for i in range(len(s) - 1)]
    if 0 <= maintenant - s[-1]["t"] <= p["trou_max_s"]:
        bornes.append((s[-1]["t"], maintenant, s[-1]["actives"]))
    out, debut_run, n_run, fin_prec = [], None, None, None
    for a, b, n in bornes:
        if b - a > p["trou_max_s"]:
            debut_run, n_run, fin_prec = None, None, None       # trou : on ne sait pas ce qui s'est passé
            continue
        if debut_run is None or n != n_run or a != fin_prec:
            debut_run, n_run = a, n                             # changement de N : une nouvelle chauffe commence
        fin_prec = b
        eff_debut = max(a, debut_run + p["chauffe_s"])
        if n > 0 and b > eff_debut:
            out.append((eff_debut, b, n))
    return out


def _moy(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def analyser(taches, echantillons, maintenant=None, params=None):
    p = dict(PARAMS, **(params or {}))
    maintenant = maintenant or time.time()
    reconstitue = bool(echantillons) and all(e.get("reconstitue") for e in echantillons)
    troncons = _troncons(echantillons, maintenant, p)
    debuts = [t[0] for t in troncons]
    secondes = {}
    for a, b, n in troncons:
        secondes[n] = secondes.get(n, 0.0) + (b - a)
    # tâches rattachées au tronçon qui les contient
    cumuls = {}
    for t in taches:
        i = bisect.bisect_right(debuts, t["t"]) - 1
        if i < 0 or not (troncons[i][0] <= t["t"] <= troncons[i][1]):
            continue
        n = troncons[i][2]
        c = cumuls.setdefault((n, t.get("type") or "?"), {"ok": 0, "err": 0, "duree": [], "llm": [], "verrou": []})
        if t.get("ok", True):
            c["ok"] += 1
            c["duree"].append(t.get("duree_s"))
            c["llm"].append(t.get("llm_s"))
            c["verrou"].append(t.get("attente_verrou_s"))
        else:
            c["err"] += 1
    paliers = {}
    for n, sec in sorted(secondes.items()):
        h = sec / 3600.0
        types = {}
        for (n2, typ), c in cumuls.items():
            if n2 != n:
                continue
            total = c["ok"] + c["err"]
            types[typ] = {"taches": c["ok"], "erreurs": c["err"], "taux_erreur": round(c["err"] / total, 3) if total else 0.0,
                          "debit_h": round(c["ok"] / h, 1) if h > 0 else None,
                          "debit_par_instance_h": round(c["ok"] / h / n, 1) if h > 0 else None,
                          "duree_moy_s": _moy(c["duree"]), "llm_moy_s": _moy(c["llm"]), "verrou_moy_s": _moy(c["verrou"]),
                          "suffisant": c["ok"] >= p["min_taches"] and sec >= p["min_minutes"] * 60}
        paliers[n] = {"n": n, "heures": round(h, 3), "minutes": round(sec / 60, 1), "types": types}
    par_type = {typ: _verdict_type(typ, paliers, p) for typ in sorted({k[1] for k in cumuls})}
    tries = sorted(echantillons, key=lambda e: e["t"])
    n_actuel = tries[-1]["actives"] if tries else 0
    n_dernier = next((e["actives"] for e in reversed(tries) if e["actives"] > 0), 0)    # dernier N non nul (si plus rien ne tourne)
    res = {"date": datetime.fromtimestamp(maintenant).astimezone().isoformat(timespec="seconds"),
           "n_actuel": n_actuel, "n_dernier_actif": n_dernier, "reconstitue_depuis_taches": reconstitue, "parametres": p,
           "paliers": [paliers[n] for n in sorted(paliers)], "par_type": par_type}
    res.update(_synthese(res, taches))
    return res


def _verdict_type(typ, paliers, p):
    suff = [(n, paliers[n]["types"][typ]) for n in sorted(paliers) if typ in paliers[n]["types"] and paliers[n]["types"][typ]["suffisant"]]
    info = {"valeurs_n_mesurees": [n for n, _ in suff], "alertes": [], "reference_n": None, "n_optimal": None, "statut": None}
    if len(suff) < 2:
        info["statut"] = "insuffisant"
        if not suff:
            info["raison"] = "pas encore assez de mesures (au moins %d tâches et %d min à un même nombre d'instances)" % (p["min_taches"], p["min_minutes"])
        else:
            info["raison"] = "une seule valeur du nombre d'instances observée (N=%d) : rien à comparer tant que N n'a pas varié" % suff[0][0]
        return info
    n0, r0 = suff[0]
    info["reference_n"] = n0
    opt, chaine = n0, True
    for i, (n, st) in enumerate(suff):
        if i == 0:
            st.update({"acceleration": 1.0, "efficacite": 1.0, "gain_vs_precedent": None, "alertes": []})
            continue
        np_, sp = suff[i - 1]
        acc = st["debit_h"] / r0["debit_h"] if r0["debit_h"] else None
        gain = st["debit_h"] / sp["debit_h"] - 1 if sp["debit_h"] else None
        st["acceleration"] = round(acc, 2) if acc is not None else None
        st["efficacite"] = round(acc / (n / n0), 2) if acc is not None else None
        st["gain_vs_precedent"] = round(gain, 3) if gain is not None else None
        al = []
        if gain is not None and gain < -p["tol_perte"]:
            al.append({"code": "perte_nette", "texte": "à N=%d le débit baisse de %.0f %% par rapport à N=%d (%.1f contre %.1f tâches/h)" % (
                n, -gain * 100, np_, st["debit_h"], sp["debit_h"])})
        elif gain is not None and gain < p["seuil_gain"]:
            al.append({"code": "gain_faible", "texte": "passer de N=%d à N=%d n'apporte que %+.0f %% de débit (seuil : +%.0f %%)" % (
                np_, n, gain * 100, p["seuil_gain"] * 100)})
        d_ref, d_n = (r0["llm_moy_s"] or r0["duree_moy_s"]), (st["llm_moy_s"] or st["duree_moy_s"])
        if d_ref and d_n and n > n0 and d_n / d_ref >= p["seuil_saturation"] * (n / n0):
            al.append({"code": "saturation_llm", "texte": "un appel dure %.1f fois plus longtemps qu'à N=%d : les appels sont servis l'un après l'autre "
                       "(OLLAMA_NUM_PARALLEL trop bas ?) ou le processeur est saturé" % (d_n / d_ref, n0)})
        if st["duree_moy_s"] and st["verrou_moy_s"] is not None and st["verrou_moy_s"] / st["duree_moy_s"] > p["seuil_verrou"]:
            al.append({"code": "verrou", "texte": "les instances attendent le verrou %.1f s par tâche (%.0f %% de la durée) : "
                       "contention sur competences.json" % (st["verrou_moy_s"], 100 * st["verrou_moy_s"] / st["duree_moy_s"])})
        if st["taux_erreur"] - r0["taux_erreur"] > p["hausse_erreurs"]:
            al.append({"code": "erreurs_en_hausse", "texte": "taux d'erreur %.0f %% contre %.0f %% à N=%d" % (
                st["taux_erreur"] * 100, r0["taux_erreur"] * 100, n0)})
        st["alertes"] = al
        if chaine and gain is not None and gain >= p["seuil_gain"]:
            opt = n
        else:
            chaine = False
    info["n_optimal"] = opt
    info["statut"] = "mesure"
    return info


def _synthese(res, taches):
    """Verdict global : celui du type de tâche le plus fréquent dans la fenêtre."""
    p = res["parametres"]
    n_act = res["n_actuel"] or res["n_dernier_actif"]
    arret = "" if res["n_actuel"] else " (aucune instance active en ce moment : dernier état observé à N=%d)" % n_act
    if not res["par_type"]:
        return {"verdict": "insuffisant", "message": "Aucune tâche mesurable pour l'instant.",
                "recommandation": "Laissez tourner : il faut au moins %d tâches et %d min à un même nombre d'instances." % (p["min_taches"], p["min_minutes"]),
                "type_dominant": None}
    freq = {}
    for t in taches:
        if t.get("ok", True):
            freq[t.get("type")] = freq.get(t.get("type"), 0) + 1
    dom = max(res["par_type"], key=lambda k: freq.get(k, 0))
    v = res["par_type"][dom]
    out = {"type_dominant": dom}
    if v["statut"] == "insuffisant":
        out.update(verdict="insuffisant", message="Tâches « %s » : %s." % (dom, v["raison"]),
                   recommandation="Pour savoir si le nombre d'instances est bon, faites varier N (par exemple 1 puis 2 instances, "
                                  "15 à 20 minutes chacun).")
        return out
    paliers = {pl["n"]: pl["types"].get(dom) for pl in res["paliers"]}
    st = paliers.get(n_act)
    mesures = v["valeurs_n_mesurees"]
    if not st or not st.get("suffisant"):
        out.update(verdict="en_cours", message="N=%d : mesure en cours (pas encore assez de tâches à ce nombre d'instances). "
                   "Déjà mesuré pour « %s » : N=%s, optimum observé N=%d." % (n_act, dom, ", ".join(map(str, mesures)), v["n_optimal"]),
                   recommandation="Attendre la fin de la mesure à N=%d avant de modifier le nombre d'instances." % n_act)
        return out
    codes = {a["code"]: a["texte"] for a in st.get("alertes", [])}
    causes = " ; ".join(t for c, t in codes.items() if c not in ("perte_nette", "gain_faible"))
    if "perte_nette" in codes:
        out.update(verdict="perte", message="Perte d'efficacité (« %s ») : %s." % (dom, codes["perte_nette"]),
                   recommandation="Repassez à %d instance(s)." % v["n_optimal"] + (" Cause probable : " + causes + "." if causes else ""))
    elif "gain_faible" in codes or n_act > v["n_optimal"]:
        out.update(verdict="a_surveiller", message="Gain faible (« %s ») : %s." % (dom, codes.get("gain_faible", "N=%d dépasse l'optimum observé N=%d" % (n_act, v["n_optimal"]))),
                   recommandation="%d instance(s) suffisent apparemment ; au-delà vous consommez de la machine pour peu de gain." % v["n_optimal"]
                                  + (" Cause probable : " + causes + "." if causes else ""))
    else:
        out.update(verdict="ok", message="Le nombre d'instances est justifié (« %s ») : efficacité %.0f %% par rapport à N=%d, débit %.1f tâches/h." % (
            dom, 100 * (st.get("efficacite") or 1.0), v["reference_n"], st["debit_h"]),
            recommandation="Rien à changer. Ajouter une instance reste à tester (non mesuré)." + (" À noter : " + causes + "." if causes else ""))
    if arret and out.get("message"):
        out["message"] += arret
    return out


# ======================================================================
# Rapport
# ======================================================================
def rapport_markdown(res, famille="famille"):
    L = ["# Contrôle d'efficacité — %s" % famille, "", "Mis à jour : %s — instances actives maintenant : %d%s" % (
        res["date"], res["n_actuel"], " (nombre reconstitué à partir des tâches)" if res["reconstitue_depuis_taches"] else ""), "",
        "**Verdict : %s** — %s" % (res["verdict"], res["message"]), "", "**Recommandation :** %s" % res["recommandation"], ""]
    for typ, v in res["par_type"].items():
        L += ["## Tâches « %s »" % typ, ""]
        if v["statut"] == "insuffisant":
            L += ["Comparaison impossible : %s." % v["raison"], ""]
        L += ["| N | minutes | tâches | débit (tâches/h) | accélération | efficacité | gain vs N précédent | s/tâche | s/appel LLM | attente verrou (s) | erreurs |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for pl in res["paliers"]:
            st = pl["types"].get(typ)
            if not st:
                continue
            fmt = lambda x, f="%s": ("—" if x is None else f % x)
            L.append("| %d%s | %.0f | %d | %s | %s | %s | %s | %s | %s | %s | %d |" % (
                pl["n"], "" if st["suffisant"] else " (données insuffisantes)", pl["minutes"], st["taches"], fmt(st["debit_h"]),
                fmt(st.get("acceleration")), fmt(None if st.get("efficacite") is None else st["efficacite"] * 100, "%.0f %%"),
                fmt(None if st.get("gain_vs_precedent") is None else st["gain_vs_precedent"] * 100, "%+.0f %%"),
                fmt(st["duree_moy_s"]), fmt(st["llm_moy_s"]), fmt(st["verrou_moy_s"]), st["erreurs"]))
        L.append("")
        for pl in res["paliers"]:
            st = pl["types"].get(typ) or {}
            for a in st.get("alertes", []):
                L.append("- ⚠ N=%d : %s" % (pl["n"], a["texte"]))
        L.append("")
    L += ["---", "Mesure observée en conditions réelles (non contrôlée) : la difficulté des tâches et la charge de la machine varient. "
          "Chaque type de tâche est comparé à lui-même ; les %d premières secondes après un changement de N sont ignorées."
          % res["parametres"]["chauffe_s"], ""]
    return "\n".join(L)


def mesurer_famille(dossier_etat, fenetre_h=24, maintenant=None, params=None):
    """Analyse complète d'une famille à partir de son dossier d'état partagé."""
    maintenant = maintenant or time.time()
    dossier_etat = Path(dossier_etat)
    depuis = maintenant - fenetre_h * 3600
    taches = lire_jsonl(dossier_etat / "taches.jsonl", depuis)
    ech = lire_jsonl(dossier_etat / "controle" / "echantillons.jsonl", depuis)
    if not ech:
        ech = echantillons_depuis_taches(taches)
    return analyser(taches, ech, maintenant, params)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Efficacité selon le nombre d'instances (lecture seule)")
    ap.add_argument("--donnees", type=Path, required=True, help="Dossier d'état partagé de la famille (agents/<famille>/etat)")
    ap.add_argument("--fenetre-h", type=float, default=24)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = mesurer_famille(a.donnees, a.fenetre_h)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else rapport_markdown(res, a.donnees.parent.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
