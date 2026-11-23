# -*- coding: utf-8 -*-
"""courrier.py — E3 : boîtes aux lettres entre agents (fichiers JSON, bibliothèque standard).

Règles (dossier de reprise, D3 et §5.3) :
 - un message = un fichier JSON, écrit sous un nom temporaire puis renommé : jamais lu à moitié ;
 - n'importe quel agent DÉPOSE dans la boîte d'un autre ; seul le destinataire traite, déplace, supprime ;
 - traitement idempotent par id de message ; le message traité va dans courrier/traite avec son résultat.
"""
import datetime, hashlib, json, os, uuid
from pathlib import Path

def maintenant():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def nouvel_id():
    return "msg-%s-%s" % (datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])

def boites(dossier):
    d = Path(dossier) / "courrier"
    (d / "entrant").mkdir(parents=True, exist_ok=True)
    (d / "traite").mkdir(parents=True, exist_ok=True)
    return d / "entrant", d / "traite"

def message(de, a, type_, contenu, re=None, priorite=2):
    return {"id": nouvel_id(), "de": de, "a": a, "type": type_, "date": maintenant(),
            "re": re, "priorite": priorite, "contenu": contenu}

def deposer(dossier_destinataire, msg):
    """Dépôt atomique : fichier temporaire (.tmp, ignoré par le lecteur) puis renommage."""
    entrant, _ = boites(dossier_destinataire)
    nom = "%s_%s_%s.json" % (msg["date"].replace(":", "").replace("+", "p").replace("-", ""),
                             msg["de"], hashlib.sha1(msg["id"].encode()).hexdigest()[:6])
    tmp = entrant / (nom + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(msg, fh, ensure_ascii=False, indent=2)
        fh.flush(); os.fsync(fh.fileno())
    final = entrant / nom
    os.replace(tmp, final)
    return final

def lire_entrants(dossier):
    """Messages en attente, du plus prioritaire (1) au moins prioritaire, puis par date. Fichiers illisibles : listés à part."""
    entrant, traite = boites(dossier)
    deja = {p.stem.split("__")[0] for p in traite.glob("*.json")}
    ok, abimes = [], []
    for p in sorted(entrant.glob("*.json")):          # .tmp jamais lu
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(m, dict) or "id" not in m:
                raise ValueError("pas un message")
        except Exception as e:
            abimes.append((p, str(e))); continue
        if m["id"] in deja:                           # déjà traité (reprise après arrêt) : on nettoie seulement l'entrant
            p.unlink(); continue
        ok.append((p, m))
    ok.sort(key=lambda pm: (pm[1].get("priorite", 2), pm[1].get("date", "")))
    return ok, abimes

def marquer_traite(dossier, chemin, msg, resultat):
    """Ajoute le résultat au message puis le déplace vers courrier/traite (seul le destinataire appelle ceci)."""
    _, traite = boites(dossier)
    msg = dict(msg); msg["traite"] = {"date": maintenant(), "resultat": resultat}
    cible = traite / (msg["id"] + "__" + Path(chemin).name)
    with open(str(cible) + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(msg, fh, ensure_ascii=False, indent=2)
    os.replace(str(cible) + ".tmp", cible)
    Path(chemin).unlink(missing_ok=True)
