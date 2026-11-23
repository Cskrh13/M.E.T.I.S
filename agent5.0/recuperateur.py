# -*- coding: utf-8 -*-
"""recuperateur.py — téléchargement de documents depuis des SOURCES OFFICIELLES uniquement (sans LLM).

Garde-fous : liste blanche de domaines (sources_officielles.json), https obligatoire, redirections revérifiées, PDF seulement
(contenu contrôlé : il doit commencer par %PDF), taille limitée, pause entre deux pages, budget de temps par demande,
aucun écrasement d'un document existant. Chaque téléchargement est tracé dans chercheur/telechargements.jsonl.
Un document téléchargé est déposé dans documents/ : le chercheur l'indexe, l'aiguilleur l'affecte aux agents concernés.
Demande acceptée : mots-clés (« programme mathématiques cycle 3 ») ou adresse(s) directe(s) d'un PDF.
"""
import hashlib, json, math, os, re, time, unicodedata, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

def norm(t):
    t = unicodedata.normalize("NFD", str(t or "").lower().replace("’", "'"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn")

class HorsListe(Exception): pass

class Recuperateur:
    def __init__(self, racine, cfg=None):
        self.r = Path(racine)
        p = self.r / "sources_officielles.json"
        if not p.exists(): p = Path(__file__).with_name("sources_officielles.json")
        self.cfg = cfg or json.load(open(p, encoding="utf-8"))
        self.docs = self.r / "documents"; self.docs.mkdir(parents=True, exist_ok=True)
        self.trace = self.r / "chercheur" / "telechargements.jsonl"; self.trace.parent.mkdir(parents=True, exist_ok=True)
        rec = self
        class Redir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                rec.verifier(newurl); return super().redirect_request(req, fp, code, msg, headers, newurl)
        self.opener = urllib.request.build_opener(Redir)

    # ---------- liste blanche ----------
    def verifier(self, url):
        u = urlparse(url); h = (u.hostname or "").lower()
        local = self.cfg.get("autoriser_http_local") and h in ("127.0.0.1", "localhost")
        if u.scheme != "https" and not local: raise HorsListe("%s : https obligatoire" % url)
        if not any(h == d or h.endswith("." + d) for d in self.cfg["hotes"]): raise HorsListe("%s : site hors liste officielle" % h)

    def _get(self, url, max_octets):
        self.verifier(url)
        req = urllib.request.Request(url, headers={"User-Agent": "GrandPlanificateur-chercheur/5.2 (usage pédagogique local)"})
        with self.opener.open(req, timeout=20) as rep:
            donnees = rep.read(max_octets + 1)
        if len(donnees) > max_octets: raise ValueError("fichier trop gros (limite %d Mo)" % (max_octets >> 20))
        return donnees

    # ---------- exploration ----------
    @staticmethod
    def liens(html, base):
        for href, texte in re.findall(r'<a\s[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
            yield urljoin(base, href.split("#")[0]), " ".join(re.sub(r"<[^>]+>", " ", texte).split())

    def chercher(self, mots_cles, departs=None):
        """Explore les pages de départ (largeur d'abord, limites de pages/temps) ; renvoie [(score, url_pdf, texte)] triés."""
        cles = [m for m in re.findall(r"[a-z0-9]{3,}", norm(" ".join(mots_cles)))]
        if not cles: return []
        fin = time.time() + self.cfg.get("budget_s", 90); vus, pdfs = set(), {}
        file = [(u, 0) for u in (departs or self.cfg["pages_de_depart"])]
        while file and len(vus) < self.cfg.get("max_pages_explorees", 25) and time.time() < fin:
            url, prof = file.pop(0)
            if url in vus: continue
            vus.add(url)
            try: html = self._get(url, 3 << 20).decode("utf-8", errors="replace")
            except Exception: continue
            for lien, texte in self.liens(html, url):
                try: self.verifier(lien)
                except HorsListe: continue
                if urlparse(lien).path.lower().endswith(".pdf"):
                    sc = sum(1 for c in set(cles) if c in norm(unquote(lien) + " " + texte))
                    if sc >= max(1, math.ceil(2 * len(set(cles)) / 3)): pdfs[lien] = (sc, texte)
                elif prof < self.cfg.get("profondeur", 2): file.append((lien, prof + 1))
            time.sleep(self.cfg.get("pause_s", 1.0))
        return sorted(((s, u, t) for u, (s, t) in pdfs.items()), reverse=True)

    # ---------- téléchargement ----------
    def telecharger(self, url, demandeur="utilisateur"):
        """Renvoie (statut, nom_ou_raison). Statuts : telecharge, deja_present, refuse."""
        try:
            donnees = self._get(url, int(self.cfg.get("max_mo", 80)) << 20)
        except HorsListe as e: return "refuse", str(e)
        except Exception as e: return "refuse", "%s : %s" % (url, e)
        if not donnees.startswith(b"%PDF"): return "refuse", "%s : ce n'est pas un PDF" % url
        sha = hashlib.sha256(donnees).hexdigest()
        for p in self.docs.glob("*.pdf"):
            if hashlib.sha256(p.read_bytes()).hexdigest() == sha: return "deja_present", p.name
        nom = re.sub(r"[^\w .()-]", "_", unquote(Path(urlparse(url).path).name)) or "document.pdf"
        if (self.docs / nom).exists(): nom = "%s_%s.pdf" % (Path(nom).stem, sha[:6])
        tmp = self.docs / (nom + ".part"); tmp.write_bytes(donnees); os.replace(tmp, self.docs / nom)
        with open(self.trace, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "url": url, "fichier": nom, "sha256": sha, "demandeur": demandeur}, ensure_ascii=False) + "\n")
        return "telecharge", nom

    def traiter(self, demande, demandeur="utilisateur", maxi=3):
        """demande : texte (mots-clés ou adresses) ou dict {mots_cles, urls}. Renvoie un résumé lisible + la liste des résultats."""
        if isinstance(demande, str):
            urls = re.findall(r"https?://\S+", demande); mots = [] if urls else [demande]
        else: urls, mots = list(demande.get("urls") or []), list(demande.get("mots_cles") or [])
        res = []
        if not urls and mots:
            trouves = self.chercher(mots)
            if not trouves: return "aucun PDF correspondant trouvé sur les sites officiels explorés", res
            urls = [u for _, u, _ in trouves[:maxi]]
        for u in urls[:maxi]: res.append((u,) + self.telecharger(u, demandeur))
        return "; ".join("%s : %s" % (s, n) for _, s, n in res) or "rien à télécharger", res
