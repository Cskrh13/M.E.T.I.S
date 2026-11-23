# -*- coding: utf-8 -*-
"""chercheur.py — E3 : agent-chercheur SANS LLM, documents locaux uniquement.

 - indexe page par page (chaque passage garde fichier + page), pondération de type BM25, Python pur ;
 - répond aux requêtes déposées dans son courrier (jamais de passage inventé : aucun résultat = liste vide + note) ;
 - distribue chaque document à la famille la plus probable (COPIE, jamais déplacement ni modification de la source) ;
   si le score est ambigu : dossier a_router + a_router.csv pour décision humaine.
Arborescence sous RACINE : documents/  chercheur/{index,a_router,courrier}  agents/<famille>/{documents,courrier}
Usage : python chercheur.py --racine .   (un passage ; --boucle pour tourner en continu)
"""
import argparse, collections, csv, hashlib, json, math, os, re, shutil, sys, time, unicodedata
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import courrier

EXT = {".pdf", ".txt", ".md", ".docx", ".odt"}
SEUIL_SCORE = 5          # nombre minimal de mots-clés de famille trouvés sur les premières pages  (À CALIBRER)
SEUIL_RATIO = 1.5        # le meilleur score doit valoir au moins 1,5 fois le second                 (À CALIBRER)
PAGES_ROUTAGE = 5
MAX_PASSAGE = 600

def norm(t):
    t = unicodedata.normalize("NFD", str(t or "").lower().replace("’", "'"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn")

def mots(t):
    out = []
    for m in re.findall(r"[a-z0-9]{3,}", norm(t)):
        if len(m) > 4 and m[-1] in "sx": m = m[:-1]     # pluriel simple
        out.append(m)
    return out

def empreinte(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

def lire_pages(p):
    from agent4_2_core import lire_document      # réutilise la lecture du noyau (pdf, txt, docx, odt)
    return lire_document(p)

def passages_de(fichier, pages):
    res = []
    for page, texte in pages:
        courant = ""
        for para in re.split(r"\n\s*\n|(?<=[.!?])\s+", texte or ""):
            para = " ".join(para.split())
            if not para: continue
            if len(courant) + len(para) + 1 > MAX_PASSAGE and courant:
                res.append({"fichier": fichier, "page": page, "texte": courant}); courant = ""
            while len(para) > MAX_PASSAGE:
                res.append({"fichier": fichier, "page": page, "texte": para[:MAX_PASSAGE]}); para = para[MAX_PASSAGE:]
            courant = (courant + " " + para).strip()
        if len(courant) >= 20: res.append({"fichier": fichier, "page": page, "texte": courant})
    return res

class Chercheur:
    def __init__(self, racine, distribuer_actif=True):
        self.distribuer_actif = distribuer_actif       # False : l'aiguilleur s'occupe de l'affectation
        self.r = Path(racine)
        self.docs_dir = self.r / "documents"
        self.me = self.r / "chercheur"
        self.p_index = self.me / "index" / "index.json"
        for d in (self.docs_dir, self.me / "index", self.me / "a_router"):
            d.mkdir(parents=True, exist_ok=True)
        self.familles = self._lire_familles()
        self.index = json.load(open(self.p_index, encoding="utf-8")) if self.p_index.exists() else {"documents": {}, "passages": []}
        self._bm25 = None

    def _lire_familles(self):
        p = self.r / "familles.json"
        if not p.exists(): p = Path(__file__).with_name("familles.json")
        return json.load(open(p, encoding="utf-8"))["familles"]

    # ---------- indexation ----------
    def mettre_a_jour(self):
        """Indexe les documents nouveaux ou modifiés, oublie ceux qui ont disparu. Renvoie un résumé."""
        self.familles = self._lire_familles()                   # relu : familles ajoutées en cours de route
        present = {p.name: p for p in self.docs_dir.iterdir() if p.is_file() and p.suffix.lower() in EXT}
        resume = {"nouveaux": [], "modifies": [], "retires": [], "erreurs": []}
        for nom in list(self.index["documents"]):
            if nom not in present:
                self.index["passages"] = [x for x in self.index["passages"] if x["fichier"] != nom]
                del self.index["documents"][nom]; resume["retires"].append(nom)
        for nom, p in present.items():
            h = empreinte(p); ancien = self.index["documents"].get(nom)
            if ancien and ancien["empreinte"] == h: continue
            try:
                pages = lire_pages(p)
            except Exception as e:
                resume["erreurs"].append("%s : %s" % (nom, e)); continue
            self.index["passages"] = [x for x in self.index["passages"] if x["fichier"] != nom]
            ps = passages_de(nom, pages); self.index["passages"].extend(ps)
            fam, scores = self.router(pages)
            self.index["documents"][nom] = {"empreinte": h, "pages": len(pages), "passages": len(ps),
                                            "famille": fam, "scores": scores}
            (resume["modifies"] if ancien else resume["nouveaux"]).append(nom)
            if self.distribuer_actif: self.distribuer(nom, p, fam, scores)
        self._bm25 = None
        tmp = str(self.p_index) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.index, fh, ensure_ascii=False)
        os.replace(tmp, self.p_index)
        return resume

    # ---------- routage ----------
    def router(self, pages):
        texte = norm(" ".join(t for _, t in pages[:PAGES_ROUTAGE]))
        sc = {}
        for fam, discs in self.familles.items():
            n = 0
            for d in discs:
                n += texte.count(norm(d))
                for m in set(mots(d)):
                    if len(m) >= 5: n += texte.count(m) // 4          # mots isolés pèsent moins que le nom entier
            sc[fam] = n
        tri = sorted(sc.items(), key=lambda kv: -kv[1])
        (f1, s1), (f2, s2) = tri[0], tri[1]
        if s1 >= SEUIL_SCORE and s1 >= SEUIL_RATIO * max(s2, 1):
            return f1, sc
        return None, sc

    def distribuer(self, nom, src, fam, scores):
        if fam:
            cible = self.r / "agents" / fam / "documents"; cible.mkdir(parents=True, exist_ok=True)
            if not (cible / nom).exists(): shutil.copy2(src, cible / nom)           # COPIE
        else:
            a = self.me / "a_router"
            csvp = a / "a_router.csv"; neuf = not csvp.exists()
            with open(csvp, "a", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh, delimiter=";")
                if neuf: w.writerow(["fichier", "scores_par_famille", "famille_choisie_par_vous"])
                w.writerow([nom, json.dumps(scores, ensure_ascii=False), ""])

    # ---------- recherche BM25 ----------
    def _preparer(self):
        ps = self.index["passages"]
        toks = [collections.Counter(mots(p["texte"])) for p in ps]
        df = collections.Counter(m for t in toks for m in t)
        moy = (sum(sum(t.values()) for t in toks) / len(toks)) if toks else 1
        self._bm25 = (toks, df, moy)

    def chercher(self, requete_mots, familles=None, maxi=3, k1=1.5, b=0.75):
        if self._bm25 is None: self._preparer()
        toks, df, moy = self._bm25; N = len(toks)
        q = [m for mm in requete_mots for m in mots(mm)]
        res = []
        for i, p in enumerate(self.index["passages"]):
            if familles:
                f = self.index["documents"].get(p["fichier"], {}).get("famille")
                if f and f not in familles: continue
            tf, L, s = toks[i], sum(toks[i].values()) or 1, 0.0
            for m in set(q):
                if m in tf:
                    idf = math.log(1 + (N - df[m] + 0.5) / (df[m] + 0.5))
                    s += idf * tf[m] * (k1 + 1) / (tf[m] + k1 * (1 - b + b * L / moy))
            if s > 0: res.append((s, p))
        res.sort(key=lambda x: -x[0])
        return [{"fichier": p["fichier"], "page": p["page"], "texte": p["texte"][:MAX_PASSAGE], "score": round(s, 3)}
                for s, p in res[:maxi]]

    # ---------- courrier ----------
    def traiter_courrier(self):
        ok, abimes = courrier.lire_entrants(self.me)
        n = 0
        for chemin, m in ok:
            if m.get("type") == "demande_document":                  # téléchargement depuis les sources officielles
                from recuperateur import Recuperateur
                c = m.get("contenu") or {}
                try: resume, _ = Recuperateur(self.r).traiter(c.get("requete") or c, demandeur=m.get("de", "?"))
                except Exception as e: resume = "échec : %r" % e
                print("[RÉCUPÉRATION] %s -> %s" % (m.get("de"), resume), flush=True)
                if m.get("de", "").startswith("comp-"):
                    courrier.deposer(self.r / "agents" / m["de"].replace("comp-", ""),
                                     courrier.message("chercheur", m["de"], "reponse", {"note": resume}, re=m["id"]))
                courrier.marquer_traite(self.me, chemin, m, resume); n += 1; continue
            if m.get("type") != "requete":
                courrier.marquer_traite(self.me, chemin, m, "ignore : type non pris en charge"); continue
            c = m.get("contenu") or {}
            maxi = max(1, min(int(c.get("max_passages", 3)), 10))
            fams = (c.get("portee") or {}).get("familles")
            mots_cles = list(c.get("mots_cles") or [])
            passages = self.chercher(mots_cles, fams, maxi) if mots_cles else []
            note = "" if passages else ("requête sans mots-clés" if not mots_cles else "aucun passage trouvé")
            rep = courrier.message("chercheur", m["de"], "reponse",
                                   {"passages": passages, "complet": bool(passages), "note": note}, re=m["id"], priorite=m.get("priorite", 2))
            courrier.deposer(self.r / "agents" / m["de"].replace("comp-", ""), rep)     # boîte de l'agent demandeur
            courrier.marquer_traite(self.me, chemin, m, "réponse envoyée (%d passage(s))" % len(passages)); n += 1
        for p, e in abimes: print("[COURRIER] fichier illisible laissé en place :", p.name, e)
        return n

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--racine", default="."); ap.add_argument("--boucle", action="store_true"); ap.add_argument("--sans-distribution", action="store_true")
    a = ap.parse_args(argv); c = Chercheur(a.racine, distribuer_actif=not a.sans_distribution)
    while True:
        r = c.mettre_a_jour()
        if any(r.values()): print("[INDEX]", {k: v for k, v in r.items() if v})
        n = c.traiter_courrier()
        if n: print("[COURRIER] %d requête(s) traitée(s)" % n)
        if not a.boucle: break
        time.sleep(10)
    return 0

if __name__ == "__main__":
    sys.exit(main())
