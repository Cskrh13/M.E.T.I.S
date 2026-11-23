# -*- coding: utf-8 -*-
"""agent4_2.py — Grand Planificateur, Agent 5.0 : lanceur de l'agent continu (une ou plusieurs instances).

Lancement (Windows) :
    H:\\GrandPlanificateur\\moteur\\python\\python.exe agent4_2.py

Il suffit de laisser la fenêtre ouverte (ou d'utiliser lancer_agent.bat, qui le relance s'il s'arrête).
Ctrl+C arrête proprement (tout est sauvegardé). Journal lisible : etat_agent4_2\\agent.log
Options utiles : --audit (diagnostic seul, sans LLM), --une-fois (un seul cycle), --sans-llm.
PLUSIEURS INSTANCES : lancez la même commande avec --instance 0, --instance 1, … (même --competences et
--donnees) ; en général c'est le Grand Planificateur (tableau de bord) qui s'en charge.
"""
import argparse
import signal
import sys
import time
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
if str(DOSSIER) not in sys.path:
    sys.path.insert(0, str(DOSSIER))

from ollama_client import OllamaClient
from agent4_2_core import Agent, Referentiel, lire_json, ecrire_json, dossier_prive, VERSION_AGENT

ARRET = False


def rendre_autonome(fichier_log):
    """Pour que l'agent tourne SANS vous : (1) un clic dans la fenêtre ne le fige plus
    (mode « sélection rapide » de Windows désactivé), (2) Windows ne se met plus en veille
    tant qu'il travaille, (3) tout ce qui s'affiche est aussi écrit dans agent.log."""
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)
            m = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(m)):
                k.SetConsoleMode(h, (m.value | 0x80) & ~0x40)   # EXTENDED_FLAGS, sans QUICK_EDIT
            k.SetThreadExecutionState(0x80000001)               # CONTINUOUS | SYSTEM_REQUIRED
        except Exception:
            pass

    class Double:
        def __init__(self, flux, log):
            self.flux, self.log = flux, log

        def write(self, t):
            for f in (self.flux, self.log):
                try:
                    f.write(t)
                    f.flush()
                except Exception:
                    pass

        def flush(self):
            pass

        def isatty(self):
            return False

    try:
        fichier_log.parent.mkdir(parents=True, exist_ok=True)
        log = open(fichier_log, "a", encoding="utf-8", errors="replace", buffering=1)
        for nom in ("stdout", "stderr"):
            flux = getattr(sys, nom)
            try:
                flux.reconfigure(errors="replace")
            except Exception:
                pass
            setattr(sys, nom, Double(flux, log))
    except Exception:
        pass


def demander_arret(*_):
    global ARRET
    ARRET = True
    print("\nArrêt demandé : fin du travail en cours puis sauvegarde…")


def premier_existant(*chemins):
    for c in chemins:
        if c and Path(c).exists():
            return Path(c)
    return Path(chemins[0])


def main():
    ap = argparse.ArgumentParser(description="Grand Planificateur — Agent %s (révision continue)" % VERSION_AGENT)
    ap.add_argument("--documents", type=Path, help="Dossier surveillé (défaut : ..\\documents puis .\\documents)")
    ap.add_argument("--competences", type=Path, help="Fichier competences.json (défaut : .\\competences.json puis ..\\competences.json)")
    ap.add_argument("--donnees", type=Path, default=DOSSIER / "etat_agent4_2", help="Dossier d'état de l'agent")
    ap.add_argument("--instance", type=int, default=0,
                    help="Numéro d'instance (0 = principale, 1, 2… = instances supplémentaires sur la même famille)")
    ap.add_argument("--modele", default="qwen3.5:4b")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--delai-llm", type=int, default=900, help="Délai max d'un appel LLM en secondes")
    ap.add_argument("--attente", type=int, default=30, help="Secondes entre deux cycles quand il n'y a rien à faire")
    ap.add_argument("--duree-cycle", type=int, default=180, help="Durée de travail d'un cycle avant sauvegarde (s)")
    ap.add_argument("--appels-heure", type=int, default=120, help="Budget : appels LLM maximum par heure")
    ap.add_argument("--taille-segment", type=int, default=2000, help="Caractères par segment analysé")
    ap.add_argument("--periode-audit", type=int, default=600, help="Secondes entre deux audits")
    ap.add_argument("--sans-llm", action="store_true", help="Désactive le LLM (audit et validations seulement)")
    ap.add_argument("--sans-demandes", action="store_true", help="L'agent ne demande pas de documents au chercheur")
    ap.add_argument("--une-fois", action="store_true", help="Un seul cycle puis arrêt")
    ap.add_argument("--audit", action="store_true", help="Affiche l'audit de competences.json et s'arrête")
    a = ap.parse_args()

    a.documents = a.documents or premier_existant(DOSSIER.parent / "documents", DOSSIER / "documents")
    a.competences = a.competences or premier_existant(DOSSIER / "competences.json", DOSSIER.parent / "competences.json")
    a.documents.mkdir(parents=True, exist_ok=True)
    if not a.competences.exists():
        print("competences.json introuvable : %s\nCopiez-le dans ce dossier ou indiquez --competences." % a.competences)
        return 1

    if a.audit:
        ref = Referentiel(lire_json(a.competences, {}))
        rapport = ref.audit()
        ecrire_json(a.donnees / "constats.json", rapport)
        print("Compétences :", rapport["nombre_competences"])
        for k, v in rapport["resume"].items():
            print("  %-26s %d" % (k, v))
        print("Détail : %s" % (a.donnees / "constats.json"))
        return 0

    a.taille_segment = max(500, min(a.taille_segment, 2500))
    a.instance = max(0, a.instance)
    prive = dossier_prive(a.donnees, a.instance)
    rendre_autonome(prive / "agent.log")
    client = OllamaClient(a.ollama_url, a.modele, timeout=a.delai_llm)
    agent = Agent(a, client)
    agent.arret = lambda: ARRET or (agent.prive / "arret.txt").exists()   # fichier posé par le process racine
    signal.signal(signal.SIGINT, demander_arret)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, demander_arret)

    print("Agent %s (%s, instance %d) — %d compétences" % (VERSION_AGENT, agent.biome["id"], a.instance, len(agent.ref.liste)))
    print("Documents surveillés : %s" % a.documents)
    print("Validation humaine   : %s" % agent.p_csv)
    print("LLM : %s" % ("désactivé" if a.sans_llm else ("disponible" if client.disponible() else "INDISPONIBLE (travail réduit, nouvel essai à chaque cycle)")))
    try:
        while not agent.arret():
            try:
                reste = agent.cycle()
            except Exception as e:  # l'agent ne doit pas mourir sur une erreur isolée
                agent.journal("erreur_cycle", erreur=repr(e))
                print("[ERREUR CYCLE] %r" % e)
                reste = False
            if a.une_fois:
                break
            if not reste:
                for _ in range(max(1, a.attente)):
                    if agent.arret():
                        break
                    time.sleep(1)
    finally:
        try:
            agent.commit()
        finally:
            print("État sauvegardé. Au revoir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
