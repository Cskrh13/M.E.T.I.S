# -*- coding: utf-8 -*-
"""grand_planificateur.py — PROCESS RACINE (version 5.0) : lance, surveille et contrôle les agents, avec tableau de bord.

Usage :  python grand_planificateur.py [--ouvrir] [--port 8766] [--racine .]
Tableau de bord : http://127.0.0.1:8766   (accessible uniquement depuis cet ordinateur)

Ce qu'il fait :
 - un processus par agent : le chercheur + au moins un agent-compétences par famille (agents/<famille>/competences.json) ;
 - 5.0 : PLUSIEURS INSTANCES d'un même agent-compétences (boutons « + instance » / « − instance » du tableau de bord).
   Les instances d'une famille se partagent competences.json et les validations ; elles ne traitent jamais la même tâche
   (réservations + verrou, voir partage.py et agent4_2_core.py). Le chercheur reste en un seul exemplaire (il écrit un index unique).
 - 5.0 : un AGENT DE CONTRÔLE (agent_controle.py, sans LLM) mesure en continu si le nombre d'instances fait gagner ou perdre
   en efficacité (logique de mesure_efficacite.py) ; verdict et recommandation s'affichent dans le tableau de bord.
   Il avertit, il ne démarre ni n'arrête rien tout seul.
 - arrêt PROPRE d'un agent par un fichier arret.txt dans son dossier etat (il finit sa tâche, sauvegarde, s'arrête) ;
   si l'agent ne s'arrête pas dans le délai, il est terminé de force (son état est sauvegardé à chaque cycle) ;
 - redémarrage automatique en cas de panne (limité, pour ne pas boucler) ;
 - rien n'est décidé à votre place : les validations restent dans les fichiers CSV de chaque famille.
Configuration : grand_planificateur.json (créé au premier lancement, modifiable).
"""
import argparse, collections, csv, json, os, secrets, shutil, subprocess, sys, threading, time, urllib.request, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_DEFAUT = {
    "actifs": ["chercheur", "comp-mathematiques", "comp-langages"],   # lancés au démarrage ; les autres : bouton « Démarrer »
    "redemarrage_auto": True, "max_redemarrages_par_heure": 5,
    "delai_arret_s": 120,                                             # attente avant arrêt forcé
    "silence_alerte_s": 900,                                          # agent sans signe de vie depuis (en marche) : alerte
    "options_agent": [],                                              # options ajoutées à chaque agent-compétences, ex. ["--appels-heure", "60"] (budget PAR instance)
    "controle": True,                                                 # lance l'agent de contrôle de l'efficacité (désactivable)
    "instances": {},                                                  # nombre d'instances voulu par famille, ex. {"comp-mathematiques": 2} (mis à jour par le tableau de bord)
    "max_instances_par_famille": 4,
    "demarrage_auto": True,                                           # toute famille créée (par l'aiguilleur ou à la main) démarre toute seule
    "aiguilleur": True,                                               # lance l'aiguilleur : crée les agents manquants et affecte les documents
    "port": 8766
}

def lire_json(p, defaut):
    try:
        with open(p, encoding="utf-8") as fh: return json.load(fh)
    except Exception: return defaut

def lire_num_parallel():
    """Valeur de OLLAMA_NUM_PARALLEL : variable d'environnement, sinon réglage utilisateur Windows (celui que lit Ollama au lancement)."""
    v = os.environ.get("OLLAMA_NUM_PARALLEL")
    if not v and sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: v = winreg.QueryValueEx(k, "OLLAMA_NUM_PARALLEL")[0]
        except Exception: v = None
    try: return int(v) if v else None
    except ValueError: return None

def diagnostic_parallele(configure, travailleurs):
    """Texte d'avertissement, ou None si tout va bien (un seul agent travaille, ou le réglage suffit)."""
    if travailleurs <= 1 or (configure is not None and configure >= travailleurs): return None
    etat = ("OLLAMA_NUM_PARALLEL est réglé sur %d" % configure) if configure is not None else "OLLAMA_NUM_PARALLEL n'est pas défini (Ollama choisit lui-même)"
    return ("%d agents travaillent en même temps, mais %s : leurs demandes risquent de se faire attendre l'une l'autre. "
            "Pour qu'Ollama les serve ensemble : quittez Ollama, créez la variable d'environnement utilisateur OLLAMA_NUM_PARALLEL = %d "
            "(Paramètres Windows > variables d'environnement, ou « setx OLLAMA_NUM_PARALLEL %d » dans un terminal), puis relancez Ollama. "
            "Le Grand Planificateur relit ce réglage ; il ne sait pas si Ollama a été relancé depuis. Vérifiez le gain dans « Efficacité » de chaque famille."
            % (travailleurs, etat, travailleurs, travailleurs))

class Processus:
    def __init__(self, nom, genre, cmd, cwd, dossier_etat, journal, base=None, numero=0, partage=None):
        self.nom, self.genre, self.cmd, self.cwd, self.etat_dir, self.journal = nom, genre, cmd, cwd, Path(dossier_etat), Path(journal)
        self.base, self.numero = base or nom, numero           # base : nom de la famille (comp-xxx) ; numero : 0 = principale, 1, 2…
        self.partage = Path(partage) if partage else self.etat_dir   # dossier d'état commun à toutes les instances
        self.retirer = False                                   # instance supprimée depuis le tableau de bord : disparaît une fois arrêtée
        self.proc = None; self.voulu = False; self.arret_demande = None; self.depuis = None
        self.redemarrages = collections.deque(maxlen=50); self.dernier_code = None; self.en_panne = False

    @property
    def vivant(self): return self.proc is not None and self.proc.poll() is None

    def demarrer(self):
        if self.vivant: return
        self.etat_dir.mkdir(parents=True, exist_ok=True)
        (self.etat_dir / "arret.txt").unlink(missing_ok=True)          # sinon l'agent s'arrêterait aussitôt
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        out = open(self.journal, "a", encoding="utf-8", errors="replace")
        out.write("\n===== démarrage %s =====\n" % datetime.now().isoformat(timespec="seconds")); out.flush()
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(self.cmd, cwd=str(self.cwd), stdout=out, stderr=subprocess.STDOUT, creationflags=flags, env=env)
        out.close()
        self.voulu, self.arret_demande, self.depuis, self.en_panne = True, None, time.time(), False

    def demander_arret(self):
        self.voulu = False
        if not self.vivant: return
        self.arret_demande = time.time()
        if self.genre in ("agent", "controle", "aiguilleur"): (self.etat_dir / "arret.txt").write_text("arret demande par le process racine\n", encoding="utf-8")
        else: self.proc.terminate()                                    # chercheur : écritures atomiques, arrêt sans risque

    def forcer(self):
        if self.vivant:
            self.proc.terminate()
            try: self.proc.wait(10)
            except Exception: self.proc.kill()

class Superviseur:
    def __init__(self, racine, cfg, chemin_config=None):
        self.racine, self.cfg = Path(racine), cfg
        self.chemin_config = Path(chemin_config) if chemin_config else self.racine / "grand_planificateur.json"
        self.procs = {}
        self.verrou = threading.RLock()
        self.token = secrets.token_urlsafe(16)
        self._ollama = (0, None)
        self.construire()

    def construire(self):
        r = self.racine
        self.procs["chercheur"] = Processus("chercheur", "chercheur", [sys.executable, "chercheur.py", "--racine", str(r), "--boucle", "--sans-distribution"], r, r / "chercheur", r / "journaux" / "chercheur.log")
        self.procs["controle"] = Processus("controle", "controle", [sys.executable, "agent_controle.py", "--racine", str(r)], r, r / "controle", r / "journaux" / "controle.log")
        (r / "controle").mkdir(parents=True, exist_ok=True)
        self.procs["aiguilleur"] = Processus("aiguilleur", "aiguilleur", [sys.executable, "aiguilleur.py", "--racine", str(r), "--boucle"], r, r / "aiguilleur", r / "journaux" / "aiguilleur.log")
        self.familles = {}
        self.decouvrir_familles()

    def decouvrir_familles(self):
        """Une famille existe dès que agents/<famille>/competences.json existe (créé par migration_familles.py).
        Appelé aussi à chaque tick : après la migration, pas besoin de relancer le Grand Planificateur."""
        fam_dir = self.racine / "agents"
        nouvelles = []
        for d in sorted(fam_dir.iterdir()) if fam_dir.exists() else []:
            n = "comp-" + d.name
            if d.is_dir() and (d / "competences.json").exists() and n not in self.familles:
                self.familles[n] = d
                voulu = max(1, min(int(self.cfg.get("instances", {}).get(n, 1) or 1), self.max_instances()))
                for k in range(voulu):
                    p = self.creer_instance(n, k)
                    if n in self.cfg.get("actifs", []) or self.cfg.get("demarrage_auto", True): p.demarrer()
                nouvelles.append(n)
        return nouvelles

    def diagnostic(self):
        """Messages pour le tableau de bord quand quelque chose empêche de voir un agent-compétences."""
        fam_dir = self.racine / "agents"
        sans = [d.name for d in sorted(fam_dir.iterdir()) if d.is_dir() and not (d / "competences.json").exists()] if fam_dir.exists() else []
        msgs = []
        if not self.familles:
            msgs.append("Aucun agent-compétences pour l'instant : déposez un document avec « Déposer des documents » (ou dans le dossier documents/). "
                        "L'aiguilleur crée l'agent de la bonne famille et le démarre tout seul."
                        + (" Dossiers en attente (sans competences.json) : %s." % ", ".join("agents/" + x for x in sans) if sans else ""))
        elif sans:
            msgs.append("Dossiers ignorés (pas de competences.json) : %s." % ", ".join("agents/" + x for x in sans))
        return msgs

    def max_instances(self):
        return max(1, int(self.cfg.get("max_instances_par_famille", 4) or 1))

    @staticmethod
    def nom_instance(base, k):
        return base if k == 0 else "%s#%d" % (base, k + 1)

    def creer_instance(self, base, k):
        d = self.familles[base]
        nom = self.nom_instance(base, k)
        etat = d / "etat"
        prive = etat if k == 0 else etat / "instances" / str(k)
        cmd = [sys.executable, "agent4_2.py", "--competences", str(d / "competences.json"), "--documents", str(d / "documents"),
               "--donnees", str(etat), "--instance", str(k)] + list(self.cfg.get("options_agent") or [])
        p = Processus(nom, "agent", cmd, self.racine, prive, self.racine / "journaux" / (nom.replace("#", "-") + ".log"),
                      base=base, numero=k, partage=etat)
        self.procs[nom] = p
        return p

    def instances_de(self, base, vivantes_seulement=False):
        return sorted((p for p in self.procs.values() if p.genre == "agent" and p.base == base and not p.retirer),
                      key=lambda p: p.numero)

    def sauver_instances(self):
        """Mémorise le nombre d'instances par famille dans grand_planificateur.json (retrouvé au prochain lancement)."""
        try:
            cfg = lire_json(self.chemin_config, {})
            inst = {b: len(self.instances_de(b)) for b in self.familles if len(self.instances_de(b)) > 1}
            cfg["instances"] = inst
            cfg["actifs"] = list(self.cfg.get("actifs", []))
            self.cfg["instances"] = inst
            tmp = str(self.chemin_config) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.chemin_config)
        except Exception as e:
            print("[ATTENTION] nombre d'instances non mémorisé : %r" % e)

    def ajouter_instance(self, base):
        if base not in self.familles:
            return "famille inconnue (le chercheur ne peut pas être multiplié : il écrit un index unique)"
        actives = self.instances_de(base)
        if len(actives) >= self.max_instances():
            return "maximum atteint (%d instances par famille, réglable : max_instances_par_famille)" % self.max_instances()
        pris = {p.numero for p in actives}
        k = next(i for i in range(self.max_instances()) if i not in pris)
        for p in [q for q in self.procs.values() if q.base == base and q.numero == k]:   # reste d'une instance en cours de retrait
            return "l'instance %d est encore en cours d'arrêt, réessayez dans un instant" % (k + 1)
        p = self.creer_instance(base, k)
        p.demarrer()
        self.sauver_instances()
        return "instance %d ajoutée et démarrée (%s)" % (k + 1, p.nom)

    def retirer_instance(self, base):
        if base not in self.familles:
            return "famille inconnue"
        actives = self.instances_de(base)
        if len(actives) <= 1:
            return "il ne reste que l'instance principale ; utilisez « Arrêter » pour l'arrêter"
        p = actives[-1]
        p.retirer = True
        p.demander_arret()
        if not p.vivant:
            self.procs.pop(p.nom, None)
        self.sauver_instances()
        return "instance %d : arrêt en cours, elle sera retirée dès qu'elle aura terminé sa tâche" % (p.numero + 1)

    def demarrer_actifs(self):
        for p in list(self.procs.values()):
            if p.base in self.cfg["actifs"] or p.nom in self.cfg["actifs"] or (p.genre == "controle" and self.cfg.get("controle", True)) or (p.genre == "aiguilleur" and self.cfg.get("aiguilleur", True)):
                p.demarrer()

    def tick(self):
        """Appelé toutes les 2 s : constate les arrêts, applique le délai d'arrêt, relance les pannes (avec limite)."""
        with self.verrou:
            self.decouvrir_familles()
            for nom in [n for n, q in self.procs.items() if q.retirer and not q.vivant]:
                del self.procs[nom]                                    # instance retirée depuis le tableau de bord, maintenant arrêtée
            for p in list(self.procs.values()):
                if p.proc is None: continue
                if p.vivant:
                    if p.arret_demande and time.time() - p.arret_demande > self.cfg["delai_arret_s"]: p.forcer()
                    continue
                p.dernier_code = p.proc.returncode
                if p.voulu:                                            # arrêt NON demandé = panne
                    recents = [t for t in p.redemarrages if time.time() - t < 3600]
                    if self.cfg["redemarrage_auto"] and len(recents) < self.cfg["max_redemarrages_par_heure"]:
                        p.redemarrages.append(time.time()); p.demarrer()
                    else:
                        p.voulu, p.en_panne = False, True              # on garde le constat « en panne » affiché
                elif not p.en_panne:
                    p.proc = None

    def ajouter_famille(self, nom, disciplines):
        """Ajoute une famille (donc un futur agent) dans familles.json ; l'aiguilleur la crée dès qu'un document la concerne."""
        import chercheur, re
        slug = re.sub(r"[^a-z0-9]+", "_", chercheur.norm(nom)).strip("_")
        discs = [x.strip() for x in re.split(r"[;,\n]", disciplines or "") if x.strip()] or [nom.strip()]
        if not slug: return "nom de famille invalide"
        pf = self.racine / "familles.json"; f = lire_json(pf, None)
        if not f: return "familles.json introuvable"
        if slug in f["familles"]: return "la famille « %s » existe déjà" % slug
        f["familles"][slug] = discs
        tmp = str(pf) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh: json.dump(f, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, pf); (self.racine / "agents" / slug / "documents").mkdir(parents=True, exist_ok=True)
        return "famille « %s » ajoutée (%s). Son agent démarrera dès qu'un document la concernera." % (slug, ", ".join(discs))

    def action(self, nom, action, extra=None):
        if action == "demander_document":
            import courrier
            q = ((extra or {}).get("requete") or "").strip()
            if not q: return "précisez des mots-clés ou l'adresse d'un PDF"
            courrier.deposer(self.racine / "chercheur", courrier.message("utilisateur", "chercheur", "demande_document", {"requete": q}))
            return "demande transmise au chercheur (sites officiels uniquement) ; le document apparaîtra dans documents/ puis sera affecté"
        if action == "ajouter_famille": return self.ajouter_famille(nom, (extra or {}).get("disciplines"))
        with self.verrou:
            if action == "stop_all":
                for p in self.procs.values(): p.demander_arret()
                return "arrêt demandé pour tous"
            if action == "start_all":
                for p in self.procs.values():
                    if not p.retirer: p.demarrer()
                return "démarrage de tous"
            if action == "creer_famille": return self.creer_famille(nom)
            if action in ("creer_toutes", "lancer_tout"):
                rep = [self.creer_famille(d["nom"]) for d in self.disponibles()]
                if action == "lancer_tout":
                    for p in self.procs.values():
                        if not p.retirer: p.demarrer()
                    rep.append("tous les agents démarrés")
                return " ; ".join(rep) or "rien à créer"
            if action == "add_instance": return self.ajouter_instance(nom)
            if action == "remove_instance": return self.retirer_instance(nom)
            if action in ("start_famille", "stop_famille"):
                membres = self.instances_de(nom)
                if not membres: return "famille inconnue"
                for p in membres: (p.demarrer if action == "start_famille" else p.demander_arret)()
                return "%s : %d instance(s) %s" % (nom, len(membres), "démarrée(s)" if action == "start_famille" else "en cours d'arrêt")
            p = self.procs.get(nom)
            if not p: return "agent inconnu"
            if action == "start": p.demarrer(); return "démarré"
            if action == "stop": p.demander_arret(); return "arrêt demandé"
            if action != "restart": return "action inconnue"
            p.demander_arret()
        t0 = time.time()                                               # attente HORS verrou : le tableau de bord reste réactif
        while p.vivant and time.time() - t0 < self.cfg["delai_arret_s"]: time.sleep(0.5)
        with self.verrou:
            p.forcer(); p.proc = None; p.demarrer()
        return "redémarré"

    def arreter_tout(self):
        with self.verrou:
            for p in self.procs.values(): p.demander_arret()
        t0 = time.time()
        while any(p.vivant for p in self.procs.values()) and time.time() - t0 < self.cfg["delai_arret_s"]: time.sleep(0.5)
        for p in self.procs.values(): p.forcer()

    def ollama(self):
        if time.time() - self._ollama[0] > 10:
            try:
                urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2).read(); ok = True
            except Exception: ok = False
            self._ollama = (time.time(), ok)
        return self._ollama[1]

    def table_familles(self):
        f = lire_json(self.racine / "familles.json", {})
        noms = list((f.get("familles") or {}).keys())
        d = f.get("famille_par_defaut")
        return noms + ([d] if d and d not in noms else [])

    def comptes_racine(self):
        """Compétences de competences.json par famille (relu seulement si le fichier a changé)."""
        p = self.racine / "competences.json"
        try: m = p.stat().st_mtime_ns
        except OSError: return {}
        if getattr(self, "_cr", (None, {}))[0] != m:
            n = {}
            try:
                import lis
                table, defaut = lis.charger_familles(self.racine / "familles.json")
                for c in lire_json(p, {}).get("competences", []):
                    fam = table.get(lis.norm(c.get("discipline")), defaut); n[fam] = n.get(fam, 0) + 1
            except Exception: n = {}
            self._cr = (m, n)
        return self._cr[1]

    def disponibles(self):
        """Familles définies dans familles.json qui ont des compétences à la racine mais pas encore d'agent."""
        cr = self.comptes_racine()
        return [{"nom": "comp-" + f, "nb_competences": cr[f]} for f in self.table_familles()
                if cr.get(f) and "comp-" + f not in self.familles and not (self.racine / "agents" / f / "competences.json").exists()]

    def creer_famille(self, nom):
        """Crée agents/<famille>/competences.json depuis competences.json (sauvegarde d'abord) et lance l'agent."""
        import lis, migration_familles as mig
        fam = nom[5:] if nom.startswith("comp-") else nom
        if fam not in self.table_familles(): return "famille inconnue dans familles.json"
        cible = self.racine / "agents" / fam / "competences.json"
        if cible.exists(): return "cette famille existe déjà"
        src = self.racine / "competences.json"
        donnees = lire_json(src, None)
        if not donnees: return "competences.json introuvable ou illisible à la racine"
        table, defaut = lis.charger_familles(self.racine / "familles.json")
        sortie = mig.decouper(donnees, table, defaut).get(fam)
        if not sortie: return "aucune compétence de la famille « %s » dans competences.json" % fam
        sauv = self.racine / "sauvegardes" / ("competences_avant_creation_%s_%s.json" % (fam, datetime.now().strftime("%Y%m%d_%H%M%S")))
        sauv.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, sauv)       # l'original n'est jamais modifié
        mig.ecrire_atomique(cible, sortie)
        for sous in ("documents", "etat"): (cible.parent / sous).mkdir(exist_ok=True)
        ip = self.racine / "consolide" / "index_ids.json"
        idx = lire_json(ip, {}); idx.update({c["id"]: fam for c in sortie["competences"]}); mig.ecrire_atomique(ip, idx)
        n = "comp-" + fam
        if n not in self.cfg.setdefault("actifs", []): self.cfg["actifs"].append(n)
        self.decouvrir_familles()                                      # crée et démarre l'instance 1
        self.sauver_instances()
        return "agent « %s » créé (%d compétences) et lancé" % (fam, len(sortie["competences"]))

    def parallele(self, res):
        repos = ("rien à faire", "Ollama indisponible", "budget", "attente", "LLM désactivé")
        n = sum(1 for d in res if d["genre"] == "agent" and d["etat"] == "en marche" and not any(x in (d.get("statut_agent") or "") for x in repos))
        c = lire_num_parallel()
        return {"configure": c, "agents_actifs": n, "message": diagnostic_parallele(c, n)}

    def efficacite(self, base):
        """Verdict de l'agent de contrôle pour une famille, sous forme compacte pour le tableau de bord."""
        d = self.familles.get(base)
        e = lire_json(d / "etat" / "controle" / "efficacite.json", None) if d else None
        if not e: return None
        dom = e.get("type_dominant")
        lignes = []
        for pl in e.get("paliers") or []:
            st = (pl.get("types") or {}).get(dom)
            if st: lignes.append({"n": pl["n"], "taches": st["taches"], "debit_h": st["debit_h"], "efficacite": st.get("efficacite"),
                                  "gain": st.get("gain_vs_precedent"), "s_appel": st.get("llm_moy_s") or st.get("duree_moy_s"),
                                  "verrou_s": st.get("verrou_moy_s"), "suffisant": st["suffisant"]})
        return {"verdict": e.get("verdict"), "message": e.get("message"), "recommandation": e.get("recommandation"),
                "type": dom, "n_actuel": e.get("n_actuel"), "date": e.get("date"), "lignes": lignes}

    def statut(self):
        res = []
        with self.verrou:
            for n, p in self.procs.items():
                if p.vivant: etat = "arrêt en cours" if p.arret_demande else "en marche"
                else: etat = "en panne" if p.en_panne else "arrêté"
                d = {"nom": n, "genre": p.genre, "base": p.base, "numero": p.numero + 1, "retire": p.retirer, "etat": etat, "pid": p.proc.pid if p.vivant else None,
                     "depuis_s": int(time.time() - p.depuis) if p.vivant and p.depuis else None,
                     "redemarrages_1h": len([t for t in p.redemarrages if time.time() - t < 3600]), "code_sortie": p.dernier_code, "alertes": []}
                if p.genre == "agent":
                    v = lire_json(p.etat_dir / "vivant.json", {})
                    res_ = v.get("resume") or {}
                    d.update({"statut_agent": v.get("statut"), "erreur": v.get("erreur"), "dernier_travail": v.get("dernier_travail"),
                              "restant": res_.get("restant"), "jour": res_.get("jour"), "stats_llm": res_.get("stats_llm")})
                    try: age = (datetime.now().astimezone() - datetime.fromisoformat(v["date"])).total_seconds()
                    except Exception: age = None
                    d["signe_de_vie_s"] = int(age) if age is not None else None
                    if etat == "en marche" and age is not None and age > self.cfg["silence_alerte_s"]: d["alertes"].append("aucun signe de vie depuis %d min" % (age // 60))
                    if v.get("erreur"): d["alertes"].append("dernière erreur : " + str(v["erreur"])[:160])
                    d["validations_en_attente"] = self.validations(p.partage / "validation" / "a_valider.csv")
                    d["journal"] = self.queue(p.etat_dir / "journal_lisible.log", 6)
                elif p.genre == "controle":
                    v = lire_json(self.racine / "controle" / "etat.json", {})
                    d["description"] = ("Ne fait aucun appel au LLM et ne modifie rien. Mesure si le nombre d'instances d'agent-compétences "
                                        "fait gagner ou perdre en efficacité ; le verdict apparaît en tête de chaque famille.")
                    d["statut_agent"] = v.get("statut")
                    d["verdicts"] = v.get("familles") or {}
                    for fam, verdict in d["verdicts"].items():
                        if verdict == "perte": d["alertes"].append("%s : perte d'efficacité, voir la recommandation de la famille" % fam)
                    d["journal"] = self.queue(p.journal, 4)
                elif p.genre == "aiguilleur":
                    v = lire_json(self.racine / "aiguilleur" / "etat.json", {})
                    d["description"] = ("Sans LLM. Crée les agents-compétences qui manquent, affecte chaque document déposé dans documents/ à la ou aux familles concernées, "
                                        "puis l'archive dans documents/_traites/ quand tous les agents l'ont traité.")
                    d["statut_agent"] = v.get("statut"); d["journal"] = self.queue(self.racine / "aiguilleur" / "journal.log", 5)
                else:
                    idx = lire_json(self.racine / "chercheur" / "index" / "index.json", {})
                    d["description"] = ("Indexe les documents du dossier documents/ (recherche par passages), répond aux requêtes des agents-compétences et télécharge les PDF demandés depuis les sites officiels autorisés. "
                                        "Il n'affecte plus rien : c'est le travail de l'aiguilleur.")
                    d["documents_indexes"] = len(idx.get("documents") or {}); d["passages"] = len(idx.get("passages") or [])
                    d["journal"] = self.queue(p.journal, 4)
                if etat == "en panne": d["alertes"].append("panne répétée : redémarrage automatique abandonné (voir journaux/%s.log)" % n)
                res.append(d)
        familles = {}
        for d in res:
            if d["genre"] != "agent" or d["retire"]: continue
            f = familles.setdefault(d["base"], {"nom": d["base"], "instances": 0, "en_marche": 0, "restant": None,
                                                "validations_en_attente": d.get("validations_en_attente", 0),
                                                "max_instances": self.max_instances(), "_age": None})
            f["instances"] += 1
            if d["etat"] == "en marche": f["en_marche"] += 1
            age = d.get("signe_de_vie_s")
            if d.get("restant") and (f["_age"] is None or (age is not None and age < f["_age"])):
                f["restant"], f["_age"] = d["restant"], age                 # l'état est partagé : on prend l'estimation la plus fraîche
        for f in familles.values():
            f.pop("_age", None)
            f["efficacite"] = self.efficacite(f["nom"])
        return {"date": datetime.now().isoformat(timespec="seconds"), "ollama": self.ollama(), "version": "5.0",
                "agents_en_marche": sum(1 for d in res if d["genre"] == "agent" and d["etat"] == "en marche"), "diagnostic": self.diagnostic(),
                "ollama_parallele": self.parallele(res), "familles": list(familles.values()), "disponibles": self.disponibles(), "agents": res}

    @staticmethod
    def validations(p):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                return sum(1 for row in csv.DictReader(fh, delimiter=";") if not (row.get("decision") or "").strip())
        except Exception: return 0

    @staticmethod
    def queue(p, n):
        try: return Path(p).read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except Exception: return []

PAGE = r"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grand Planificateur 5.0 — pilotage</title><style>
:root{--bg:#EEF1F4;--p:#fff;--ink:#1B2430;--soft:#5A6675;--line:#D5DBE2;--ok:#2E7D4F;--warn:#B26A00;--bad:#B3322B;--acc:#0E6B8C}
@media(prefers-color-scheme:dark){:root{--bg:#10161D;--p:#19222C;--ink:#E7ECF1;--soft:#9AA7B6;--line:#2B3744;--ok:#6CCB92;--warn:#E8A94A;--bad:#F08A83;--acc:#4FB3D4}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
header{background:var(--p);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:5}
main{max-width:1200px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0}h2{font-size:18px;margin:0}h3{font-size:15px;margin:18px 0 8px}
button{font:inherit;color:inherit;background:var(--p);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer}button:hover:not(:disabled){border-color:var(--acc)}
button:disabled{opacity:.45;cursor:not-allowed}button.main{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button.big{padding:10px 18px;font-size:17px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between}.soft{color:var(--soft);font-size:14px}
.kpis{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:8px}.kpi{background:var(--p);border:1px solid var(--line);border-radius:10px;padding:10px 14px}.kv{font-size:26px;font-weight:700}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}
.fc{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:16px;border-top:6px solid var(--line);display:flex;flex-direction:column;gap:8px}.fc.on{border-top-color:var(--ok)}.fc.new{border-style:dashed;border-top-color:var(--acc)}
.pill{border-radius:99px;padding:1px 10px;font-size:13px;font-weight:700;border:1px solid var(--line)}.p-on{color:var(--ok);border-color:var(--ok)}.p-off{color:var(--soft)}
.dots{display:flex;gap:6px}.dot{width:14px;height:14px;border-radius:50%;border:2px solid var(--line)}.dot.on{background:var(--ok);border-color:var(--ok)}.dot.run{animation:p 1.4s infinite}@keyframes p{50%{opacity:.35}}
@media(prefers-reduced-motion:reduce){.dot.run{animation:none}}
.drop{border:2px dashed var(--acc);border-radius:12px;padding:14px;margin:10px 0;display:flex;gap:14px;flex-wrap:wrap;align-items:center;background:var(--p)}.drop.over{background:var(--bg)}.drop input[type=text]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
.step{display:flex;gap:8px;align-items:center}.step b{min-width:3.5em;text-align:center}
.card{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;border-left:6px solid var(--soft);margin-top:8px}.card.ok{border-left-color:var(--ok)}.card.warn{border-left-color:var(--warn)}.card.bad{border-left-color:var(--bad)}
.alert{color:var(--bad);font-size:14px}pre{background:var(--p);padding:8px;border-radius:6px;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:140px;overflow:auto;margin:8px 0 0}
.badge{font-weight:700}.ok .badge{color:var(--ok)}.warn .badge{color:var(--warn)}.bad .badge{color:var(--bad)}
.verdict{border:1px solid var(--line);border-left:6px solid var(--soft);border-radius:8px;padding:8px 12px;font-size:14px}.v-ok{border-left-color:var(--ok)}.v-a_surveiller{border-left-color:var(--warn)}.v-perte{border-left-color:var(--bad)}
table{border-collapse:collapse;margin-top:6px;font-size:13px}th,td{border:1px solid var(--line);padding:2px 8px;text-align:right}tr.peu{opacity:.5}
.note{background:var(--p);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin:0 0 10px;font-size:14px}summary{cursor:pointer;font-weight:600}
:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
</style></head><body><header><h1>Grand Planificateur 5.0 — pilotage</h1><div class="row"><span id="oll" class="soft"></span><span id="maj" class="soft"></span>
<button id="all1" class="main">Tout lancer</button><button id="all0">Tout arrêter</button></div></header>
<main><div id="note"></div><div class="kpis" id="kpis"></div>
<div class="drop" id="drop"><div><b>Déposer des documents</b><div class="soft">Glissez-les ici : l’aiguilleur les affecte aux agents concernés et crée les agents manquants.</div></div>
<input type="file" id="fic" multiple accept=".pdf,.txt,.md,.docx,.odt"><span class="soft" id="depmsg"></span></div>
<div class="drop"><div><b>Demander un document officiel</b><div class="soft">Le chercheur le télécharge depuis les sites officiels autorisés (eduscol, education.gouv).</div></div><input type="text" id="dq" size="42" placeholder="ex. programme mathématiques cycle 3, ou adresse d’un PDF" aria-label="Document à rechercher"><button id="dqb" class="main">Demander</button></div>
<details><summary>Ajouter un agent (nouvelle famille)</summary><div class="drop"><input type="text" id="nf" placeholder="Nom, ex. Sciences" aria-label="Nom de la famille"><input type="text" id="nd" size="40" placeholder="Disciplines séparées par des virgules" aria-label="Disciplines"><button id="nfb" class="main">Ajouter</button></div></details><div id="g"></div><p class="soft" id="msg" role="status" aria-live="polite"></p></main>
<script>
const TOKEN="__TOKEN__";
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
async function act(nom,action,extra){const m=document.getElementById("msg");m.textContent="…";try{const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.assign({token:TOKEN,nom,action},extra||{}))});
 const j=await r.json();m.textContent=(nom||"tous")+" : "+(j.message||j.erreur)}catch(e){m.textContent="Process racine injoignable"}load()}
function duree(s){if(s==null)return"—";if(s<90)return s+" s";if(s<5400)return Math.round(s/60)+" min";return(s/3600).toFixed(1)+" h"}
function bouton(txt,fn,cls,off){const x=el("button",cls||"",txt);x.disabled=!!off;x.onclick=fn;return x}
function carte(a){
 const cls=a.etat==="en marche"?(a.alertes.length?"warn":"ok"):a.etat==="en panne"?"bad":a.etat==="arrêt en cours"?"warn":"";
 const c=el("div","card "+cls),h=el("div","row");
 const titre=a.genre==="controle"?"Agent de contrôle":a.genre==="aiguilleur"?"Aiguilleur":a.genre==="agent"?"Instance "+a.numero+(a.numero===1?" (principale)":"")+(a.retire?" — retrait en cours":""):"Chercheur";
 h.append(el("b","",titre),el("span","badge",a.etat));c.append(h);
 const l=[];if(a.pid)l.push("pid "+a.pid+", depuis "+duree(a.depuis_s));if(a.redemarrages_1h)l.push(a.redemarrages_1h+" redémarrage(s) en 1 h");
 if(a.genre==="agent"){if(a.statut_agent)l.push("État : "+a.statut_agent);l.push("Signe de vie : il y a "+duree(a.signe_de_vie_s));if(a.jour)l.push("Aujourd’hui : "+a.jour.actions+" action(s), "+a.jour.erreurs+" erreur(s)")}
 else if(a.genre==="aiguilleur"){if(a.statut_agent)l.push("État : "+a.statut_agent)}
 else if(a.genre==="controle"){if(a.statut_agent)l.push("État : "+a.statut_agent);const fs=Object.entries(a.verdicts||{});
  if(!fs.length)l.push("Aucune famille à surveiller pour l’instant.");else fs.forEach(([k,v])=>l.push(k+" : "+(v||"pas encore de mesure")))}
 else l.push(a.documents_indexes+" document(s) indexé(s), "+a.passages+" passages");
 if(a.description)c.append(el("div","soft",a.description));
 l.forEach(t=>c.append(el("div","soft",t)));a.alertes.forEach(t=>c.append(el("div","alert","⚠ "+t)));
 const b=el("div","row");b.style.cssText="margin-top:6px;justify-content:flex-start";
 [["start","Démarrer"],["stop","Arrêter"],["restart","Redémarrer"]].forEach(([k,t])=>b.append(bouton(t,()=>act(a.nom,k))));c.append(b);
 if(a.journal&&a.journal.length)c.append(el("pre","",a.journal.join("\n")));return c}
function verdict(e){const v=el("div","verdict v-"+e.verdict);
 const noms={ok:"Efficacité : OK",a_surveiller:"Efficacité : à surveiller",perte:"PERTE d’efficacité",insuffisant:"Efficacité : mesure insuffisante",en_cours:"Efficacité : mesure en cours"};
 v.append(el("b","",noms[e.verdict]||e.verdict),el("div","",e.message||""),el("div","",e.recommandation||""));
 if(e.lignes&&e.lignes.length){const t=el("table"),th=el("tr");["N","tâches","tâches/h","efficacité","gain","s/appel","verrou (s)"].forEach(x=>th.append(el("th","",x)));t.append(th);
  e.lignes.forEach(l=>{const tr=el("tr",l.suffisant?"":"peu");[l.n,l.taches,l.debit_h,l.efficacite==null?"—":Math.round(l.efficacite*100)+" %",l.gain==null?"—":(l.gain>=0?"+":"")+Math.round(l.gain*100)+" %",l.s_appel==null?"—":l.s_appel,l.verrou_s==null?"—":l.verrou_s].forEach(x=>tr.append(el("td","",String(x))));t.append(tr)});
  v.append(t)}return v}
function kpi(t,v){const d=el("div","kpi");d.append(el("div","kv",String(v)),el("div","soft",t));return d}
function carteFam(f,agents,ouvert){
 const on=f.en_marche>0,c=el("section","fc"+(on?" on":"")),h=el("div","row");
 h.append(el("h2","",f.nom.replace("comp-","")),el("span","pill "+(on?"p-on":"p-off"),on?f.en_marche+" en marche":"à l’arrêt"));c.append(h);
 const r=f.restant;c.append(el("div","soft",r&&r.total!=null?"Reste "+r.total+" tâche(s)"+(r.heures_estimees!=null?" · ≈ "+r.heures_estimees+" h pour une instance":""):"Pas encore d’estimation du travail restant"));
 const d=el("div","dots");agents.forEach(a=>d.append(el("span","dot"+(a.etat==="en marche"?" on run":""))));c.append(d);
 const s=el("div","step");s.append(bouton("−",()=>act(f.nom,"remove_instance"),"",f.instances<=1),el("b","",f.instances+" / "+f.max_instances),bouton("+",()=>act(f.nom,"add_instance"),"",f.instances>=f.max_instances),el("span","soft","instances"));c.append(s);
 const b=el("div","row");b.style.justifyContent="flex-start";
 b.append(on?bouton("Arrêter la famille",()=>act(f.nom,"stop_famille"),"big"):bouton("Lancer la famille",()=>act(f.nom,"start_famille"),"main big"));c.append(b);
 if(f.validations_en_attente)c.append(el("div","soft","✎ "+f.validations_en_attente+" validation(s) en attente (CSV de la famille)"));
 if(f.efficacite)c.append(verdict(f.efficacite));
 const dt=el("details");if(ouvert)dt.open=true;dt.append(el("summary","","Instances et journal"));agents.forEach(a=>dt.append(carte(a)));c.append(dt);return c}
function carteNouvelle(d){const c=el("section","fc new"),h=el("div","row");
 h.append(el("h2","",d.nom.replace("comp-","")),el("span","pill","pas encore créé"));c.append(h);
 c.append(el("div","soft",d.nb_competences+" compétence(s) de competences.json relèvent de cette famille. Créer l’agent copie ces compétences dans agents/"+d.nom.replace("comp-","")+"/ (l’original n’est jamais modifié ; une sauvegarde est faite) puis lance l’agent."));
 c.append(bouton("Créer et lancer",()=>act(d.nom,"creer_famille"),"main big"));return c}
async function load(){try{const s=await(await fetch("/api/status")).json();const g=document.getElementById("g");
 const ag=s.agents.filter(a=>a.genre==="agent"&&!a.retire),dispo=s.disponibles||[];
 const rest=s.familles.reduce((t,f)=>t+((f.restant&&f.restant.total)||0),0),val=s.familles.reduce((t,f)=>t+(f.validations_en_attente||0),0);
 document.getElementById("kpis").replaceChildren(kpi("agents en marche",s.agents_en_marche),kpi("familles actives",s.familles.filter(f=>f.en_marche>0).length+" / "+(s.familles.length+dispo.length)),kpi("tâches restantes",rest),kpi("validations en attente",val),kpi("Ollama",s.ollama?"prêt":"injoignable"));
 const out=[],t1=el("h3","","Agents-compétences — lancer, multiplier, arrêter"),gr=el("div","grid");
 s.familles.forEach(f=>gr.append(carteFam(f,ag.filter(a=>a.base===f.nom).sort((x,y)=>x.numero-y.numero),s.familles.length===1)));
 dispo.forEach(d=>gr.append(carteNouvelle(d)));
 if(!s.familles.length&&!dispo.length)out.push(el("div","note","Aucun agent-compétences pour l’instant : déposez un document ci-dessus, l’aiguilleur crée l’agent et le démarre."));
 out.push(t1,gr);
 const ch=s.agents.filter(a=>a.genre!=="agent");
 if(ch.length){const gr2=el("div","grid");ch.forEach(a=>gr2.append(carte(a)));out.push(el("h3","","Chercheur et contrôle (un seul exemplaire de chacun)"),gr2)}
 const op=document.querySelectorAll("details[open]").length;if(!op||!g.children.length)g.replaceChildren(...out);else{const ouverts=[...g.querySelectorAll("section.fc")].map(x=>x.querySelector("details")&&x.querySelector("details").open);g.replaceChildren(...out);[...g.querySelectorAll("section.fc")].forEach((x,i)=>{const d=x.querySelector("details");if(d&&ouverts[i])d.open=true})}
 const n=document.getElementById("note");n.replaceChildren();
 (s.diagnostic||[]).forEach(t=>{if(!dispo.length)n.append(el("div","note",t))});
 if(s.ollama_parallele&&s.ollama_parallele.message)n.append(el("div","note",s.ollama_parallele.message));
 document.getElementById("oll").textContent="Ollama : "+(s.ollama?"disponible":"INJOIGNABLE");document.getElementById("maj").textContent="Mis à jour à "+s.date.slice(11)}
 catch(e){document.getElementById("maj").textContent="Process racine injoignable"}}
async function deposer(fs){const m=document.getElementById("depmsg");for(const f of fs){m.textContent="Envoi de "+f.name+"…";
 try{const r=await fetch("/api/depot?nom="+encodeURIComponent(f.name),{method:"POST",headers:{"X-Token":TOKEN},body:f});const j=await r.json();m.textContent=j.message||j.erreur}catch(e){m.textContent="Envoi impossible"}}load()}
const dz=document.getElementById("drop");document.getElementById("fic").onchange=e=>deposer([...e.target.files]);
dz.ondragover=e=>{e.preventDefault();dz.classList.add("over")};dz.ondragleave=()=>dz.classList.remove("over");dz.ondrop=e=>{e.preventDefault();dz.classList.remove("over");deposer([...e.dataTransfer.files])};
document.getElementById("dqb").onclick=()=>{const q=document.getElementById("dq").value.trim();if(q)act("chercheur","demander_document",{requete:q})};
document.getElementById("nfb").onclick=()=>{const n=document.getElementById("nf").value.trim();if(n)act(n,"ajouter_famille",{disciplines:document.getElementById("nd").value})};
document.getElementById("all1").onclick=()=>act("","lancer_tout");document.getElementById("all0").onclick=()=>{if(confirm("Arrêter tous les agents ?"))act("","stop_all")};
load();setInterval(load,5000);
</script></body>
</html>"""

def fabriquer_handler(sup):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _ok_hote(self):
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")      # contre le « DNS rebinding »
        def _envoyer(self, code, corps, type_="application/json; charset=utf-8"):
            b = corps if isinstance(corps, bytes) else corps.encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", type_); self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if not self._ok_hote(): return self._envoyer(403, "{}")
            if self.path == "/": return self._envoyer(200, PAGE.replace("__TOKEN__", sup.token), "text/html; charset=utf-8")
            if self.path == "/api/status": return self._envoyer(200, json.dumps(sup.statut(), ensure_ascii=False))
            self._envoyer(404, "{}")
        def do_POST(self):
            chemin = self.path.split("?")[0]
            if not self._ok_hote() or chemin not in ("/api/action", "/api/depot"): return self._envoyer(403, "{}")
            if chemin == "/api/depot":                                  # dépôt d'un document : documents/<nom> (l'aiguilleur l'affecte)
                from urllib.parse import parse_qs, urlparse
                if not secrets.compare_digest(self.headers.get("X-Token", ""), sup.token): return self._envoyer(403, json.dumps({"erreur": "jeton invalide"}))
                nom = Path(parse_qs(urlparse(self.path).query).get("nom", [""])[0]).name
                n = int(self.headers.get("Content-Length", "0"))
                if Path(nom).suffix.lower() not in (".pdf", ".txt", ".md", ".docx", ".odt") or not 0 < n <= 80 << 20:
                    return self._envoyer(400, json.dumps({"erreur": "format non pris en charge (pdf, txt, md, docx, odt) ou fichier trop gros (80 Mo)"}))
                dossier = sup.racine / "documents"; dossier.mkdir(exist_ok=True)
                tmp = dossier / (nom + ".part"); tmp.write_bytes(self.rfile.read(n)); os.replace(tmp, dossier / nom)
                return self._envoyer(200, json.dumps({"message": "« %s » déposé : l'aiguilleur l'affecte dans quelques secondes" % nom}, ensure_ascii=False))
            try:
                n = int(self.headers.get("Content-Length", "0")); d = json.loads(self.rfile.read(min(n, 10000)))
            except Exception: return self._envoyer(400, json.dumps({"erreur": "requête illisible"}))
            if not secrets.compare_digest(str(d.get("token", "")), sup.token): return self._envoyer(403, json.dumps({"erreur": "jeton invalide"}))
            self._envoyer(200, json.dumps({"message": sup.action(d.get("nom", ""), d.get("action", ""), d)}, ensure_ascii=False))
    return H

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--racine", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--port", type=int, default=None); ap.add_argument("--ouvrir", action="store_true")
    a = ap.parse_args(argv)
    pc = a.racine / "grand_planificateur.json"
    if not pc.exists(): pc.write_text(json.dumps(CONFIG_DEFAUT, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = dict(CONFIG_DEFAUT); cfg.update(lire_json(pc, {}))
    if a.port: cfg["port"] = a.port
    sup = Superviseur(a.racine, cfg, pc)
    sup.demarrer_actifs()
    srv = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), fabriquer_handler(sup))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % cfg["port"]
    print("Process racine en marche. Tableau de bord : %s\nCtrl+C : arrêt propre de tous les agents." % url)
    if a.ouvrir: webbrowser.open(url)
    try:
        while True:
            sup.tick(); time.sleep(2)
    except KeyboardInterrupt:
        print("\nArrêt demandé : les agents terminent leur tâche en cours puis sauvegardent…")
    finally:
        sup.arreter_tout(); srv.shutdown(); print("Tout est arrêté. Au revoir.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
