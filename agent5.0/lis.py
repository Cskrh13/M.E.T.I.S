# -*- coding: utf-8 -*-
"""lis.py — E2 : conversion de competences.json en LIS v1 (index compact), SANS LLM.

Usage :  python lis.py [--competences competences.json] [--familles familles.json] [--sortie dossier]
Écrit dans le dossier de sortie (par défaut ./lis_sortie) :
  lis.jsonl             une ligne par compétence (reconstructible à volonté)
  rapport_lis.md        rapport de couverture lisible
  echantillon_lis.csv   30 compétences tirées au sort, à vérifier dans Excel (colonne 'verdict')
JAMAIS de modification de competences.json (lecture seule).
Bibliothèque standard uniquement.
"""
import argparse, collections, csv, hashlib, json, random, re, sys, unicodedata
from pathlib import Path

ORDRE_NIVEAUX = ["TPS", "PS", "MS", "GS", "CP", "CE1", "CE2", "CM1", "CM2", "6e"]
RANG = {n: i for i, n in enumerate(ORDRE_NIVEAUX)}
RANG["CM1-CM2"] = 7.5          # valeur du dossier de reprise
# "Cycle 3", "A préciser", "CPS" : pas de rang fiable -> niv = null (compté dans le rapport)

def norm(t):
    t = unicodedata.normalize("NFD", str(t or "").lower().replace("’", "'"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").strip()

# --- Lexique des 7 opérations cognitives (PROPOSITION à valider par Vincent) -------------
LEXIQUE_BRUT = {
 "memoriser": "memoriser retenir connaitre reciter restituer nommer citer enumerer epeler rappeler identifier reconnaitre repérer reperer designer situer localiser retrouver evoquer",
 "comprendre": "approprier explorer constater definir comprendre expliquer interpreter decrire degager percevoir saisir reformuler resumer paraphraser decouvrir observer ecouter distinguer discerner sentir ressentir",
 "appliquer": "reproduire adapter trouver completer ajouter placer appliquer utiliser employer mobiliser realiser effectuer executer calculer resoudre mesurer lire decoder dechiffrer encoder copier tracer dessiner conjuguer orthographier accorder transcrire additionner soustraire multiplier diviser compter denombrer convertir oraliser reinvestir reemployer reutiliser pratiquer respecter adopter automatiser maitriser traduire transformer substituer",
 "analyser": "determiner associer rechercher etablir caracteriser analyser classer trier categoriser organiser structurer deduire inferer relier hierarchiser decomposer justifier evaluer critiquer verifier controler raisonner rapprocher reperer_relations argumenter selectionner choisir",
 "produire": "construire representer donner constituer dire prononcer formuler cooperer produire ecrire rediger composer inventer creer raconter exposer presenter exprimer proposer imaginer concevoir debattre participer reagir repondre interagir dialoguer chanter interpreter_oral rediger",
 "comparer": "comparer ranger ordonner encadrer differencier opposer confronter estimer arrondir",
 "manipuler": "manipuler experimenter assembler decouper plier modeler jouer deplacer courir nager lancer sauter danser grimper franchir tenir pousser lancer attraper equilibrer realiser_geste coller peindre dessiner_geste monter demonter programmer"
}
LEXIQUE = {}
for op, mots in LEXIQUE_BRUT.items():
    for m in mots.split():
        LEXIQUE.setdefault(m, op)        # en cas de doublon, la première opération listée gagne
LEXIQUE.pop("interpreter_oral", None); LEXIQUE.pop("reperer_relations", None); LEXIQUE.pop("realiser_geste", None); LEXIQUE.pop("dessiner_geste", None)
MULTI = [("prendre conscience", "comprendre"), ("mettre en relation", "analyser"), ("mettre en oeuvre", "appliquer"),
         ("mettre en ordre", "comparer"), ("prendre la parole", "produire"), ("s'approprier", "comprendre"),
         ("faire preuve", "appliquer"), ("avoir automatise", "appliquer"), ("avoir memorise", "memoriser")]
AUXILIAIRES = {"developper", "consolider", "enrichir", "approfondir", "poursuivre", "aborder", "ameliorer", "acquerir", "savoir", "pouvoir", "commencer", "parvenir", "etre", "devenir", "continuer", "apprendre", "vouloir"}
MOTS_VIDES = {"a", "de", "d'", "du", "des", "le", "la", "les", "l'", "capable", "en", "dans", "se", "s'", "ne", "pas", "plus", "peu", "toutes", "tous"}

def operation(texte):
    """Renvoie (operation, via, certitude). via = 'premier_verbe' | 'multi' | 'texte' | None."""
    t = norm(texte)
    t = re.sub(r"^(en fin d'annee|en fin de periode \d|des la fin de la \d\w* periode|des le debut de l'annee|des les premieres semaines|a la fin de l'annee|en milieu d'annee|a l'issue de la periode \d|a partir de la periode \d|des la periode \d|au cours des periodes \d a \d|a la fin du cycle|en fin de cycle)\s*[:,]?\s*", "", t)
    for debut, op in MULTI:
        if t.startswith(debut):
            return op, "multi", 0.8
    mots = re.findall(r"[a-z]+'?", t)
    i = 0
    while i < len(mots):
        m = mots[i]
        if m in AUXILIAIRES and i + 1 < len(mots) and (m != "apprendre" or mots[i + 1] in ("a", "de", "d'")):
            i += 1
            continue
        if m in MOTS_VIDES:
            i += 1
            continue
        break
    if i < len(mots) and mots[i] in LEXIQUE:
        return LEXIQUE[mots[i]], "premier_verbe", 0.8
    for m in mots[:25]:                       # repli : un verbe connu ailleurs dans le début de la phrase
        if m in LEXIQUE:
            return LEXIQUE[m], "texte", 0.5
    return None, None, 0.0

def charger_familles(chemin):
    f = json.load(open(chemin, encoding="utf-8"))
    table = {norm(d): fam for fam, ds in f["familles"].items() for d in ds}
    return table, f["famille_par_defaut"]

def convertir(c, table, defaut):
    disc = norm(c.get("discipline"))
    fam = table.get(disc, defaut)
    texte = c.get("intitule") or c.get("nom") or ""
    op, via, cert = operation(texte)
    if op is None and c.get("objectifs"):
        op, via, cert = operation(c["objectifs"][0])
        if op: via, cert = "objectif_" + via, min(cert, 0.6)
    aff = (c.get("affinage") or {}).get("valeurs") or {}
    v = {k: x.get("valeur") for k, x in aff.items() if isinstance(x, dict)}
    rec = {"lis": 1, "id": c["id"], "fam": fam, "dom": c.get("domaine_id"),
           "niv": RANG.get(c.get("niveau")), "op": op, "pre": c.get("prerequis_competences") or [],
           "src": {"f": c.get("source"), "h": hashlib.sha1(texte.encode("utf-8")).hexdigest()[:10]},
           "etat": c.get("statut") or "actif", "cert": cert, "via": via}
    if v: rec["v"] = v
    return rec

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--competences", default="competences.json")
    ap.add_argument("--familles", default=str(Path(__file__).with_name("familles.json")))
    ap.add_argument("--sortie", default="lis_sortie")
    a = ap.parse_args(argv)
    d = json.load(open(a.competences, encoding="utf-8"))
    comps = d["competences"] if isinstance(d, dict) else d
    table, defaut = charger_familles(a.familles)
    sortie = Path(a.sortie); sortie.mkdir(parents=True, exist_ok=True)
    recs = [convertir(c, table, defaut) for c in comps]
    with open(sortie / "lis.jsonl", "w", encoding="utf-8") as fh:
        for r in recs: fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    par_id = {c["id"]: c for c in comps}
    n = len(recs); couv = sum(1 for r in recs if r["op"]); sur = sum(1 for r in recs if r["via"] in ("premier_verbe", "multi"))
    pre_tot = sum(len(r["pre"]) for r in recs)
    fam_par_id = {r["id"]: r["fam"] for r in recs}
    traversent = sum(1 for r in recs for p in r["pre"] if p in fam_par_id and fam_par_id[p] != r["fam"])
    inconnus = sum(1 for r in recs for p in r["pre"] if p not in fam_par_id)
    fam_c = collections.Counter(r["fam"] for r in recs); op_c = collections.Counter(r["op"] or "(non couvert)" for r in recs)
    sans_niv = collections.Counter(c.get("niveau") for c, r in zip(comps, recs) if r["niv"] is None)
    defaut_disc = collections.Counter(c.get("discipline") for c, r in zip(comps, recs) if norm(c.get("discipline")) not in table)
    non = collections.Counter()
    for c, r in zip(comps, recs):
        if not r["op"]:
            m = re.findall(r"[a-zàâçéèêëîïôûùüœ']+", (c.get("intitule") or c.get("nom") or "").lower())
            non[m[0] if m else "(vide)"] += 1
    rnd = random.Random(2026)
    ech = rnd.sample(range(n), min(30, n))
    with open(sortie / "echantillon_lis.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["id", "intitule", "niveau", "famille", "operation_proposee", "comment_trouvee", "verdict (ok / faux / ?)", "operation_correcte"])
        for i in ech:
            c, r = comps[i], recs[i]
            w.writerow([c["id"], c.get("intitule") or c.get("nom"), c.get("niveau"), r["fam"], r["op"] or "", r["via"] or "", "", ""])
    L = ["# Rapport de couverture LIS v1", "",
         "Compétences lues : **%d** (competences.json n'est pas modifié).\n" % n,
         "## Couverture des opérations cognitives par règles (sans LLM)",
         "- Opération trouvée : **%d / %d (%.1f %%)**" % (couv, n, 100.0 * couv / n),
         "- dont par le premier verbe ou une locution (fiable) : %d (%.1f %%)" % (sur, 100.0 * sur / n),
         "- dont par repli ailleurs dans la phrase (moins fiable) : %d" % (couv - sur),
         "- Non couvertes (resteraient au LLM ou à vous) : %d" % (n - couv), "",
         "**Important** : une opération « trouvée » n'est pas forcément « juste ». L'exactitude se mesure avec l'échantillon `echantillon_lis.csv` (30 lignes, à remplir par vous).", "",
         "## Répartition par opération", *["- %s : %d" % kv for kv in op_c.most_common()], "",
         "## Répartition par famille (table familles.json)", *["- %s : %d" % kv for kv in fam_c.most_common()], "",
         "Disciplines envoyées dans la famille par défaut (« %s ») faute d'être dans la table :" % defaut, *["- %s : %d" % kv for kv in defaut_disc.most_common()], "",
         "## Prérequis", "- Liens de prérequis : %d" % pre_tot,
         "- Liens qui traverseraient deux familles : %d (%.1f %%)" % (traversent, 100.0 * traversent / max(1, pre_tot)),
         "- Liens vers un identifiant inconnu : %d" % inconnus, "",
         "## Niveaux sans rang (niv = null)", *["- %s : %d" % (k, v) for k, v in sans_niv.most_common()], "",
         "## Premiers mots des compétences non couvertes (pour enrichir le lexique)",
         *["- %s : %d" % kv for kv in non.most_common(40)]]
    (sortie / "rapport_lis.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:12])); print("Fichiers écrits dans", sortie.resolve())
    return 0

if __name__ == "__main__":
    sys.exit(main())
