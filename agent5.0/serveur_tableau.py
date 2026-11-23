# -*- coding: utf-8 -*-
"""serveur_tableau.py — Tableau de bord Synapses en temps réel (lecture seule).

Lancement :  python serveur_tableau.py      (puis la page s'ouvre dans le navigateur)
Ce serveur ne MODIFIE RIEN : il lit competences.json et le dossier etat_agent4_2 de l'agent,
et n'écoute que sur cet ordinateur (127.0.0.1). Ctrl+C pour l'arrêter.
"""
import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent


def lire_json(chemin, defaut):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8-sig"))
    except Exception:
        return defaut


def fin_jsonl(chemin, n):
    try:
        with open(chemin, "rb") as f:
            f.seek(0, 2)
            taille = f.tell()
            f.seek(max(0, taille - 200000))
            lignes = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    sortie = []
    for l in lignes[-n:]:
        try:
            sortie.append(json.loads(l))
        except Exception:
            pass
    return sortie


def toutes_propositions(chemin):
    try:
        return [json.loads(l) for l in Path(chemin).read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--competences", type=Path, default=None)
    ap.add_argument("--donnees", type=Path, default=DOSSIER / "etat_agent4_2")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sans-navigateur", action="store_true")
    a = ap.parse_args()
    comp = a.competences or next((p for p in (DOSSIER / "competences.json", DOSSIER.parent / "competences.json") if p.exists()),
                                 DOSSIER / "competences.json")
    page = DOSSIER / "tableau_de_bord.html"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def envoyer(self, octets, type_="application/json; charset=utf-8", code=200):
            self.send_response(code)
            self.send_header("Content-Type", type_)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(octets)))
            self.end_headers()
            self.wfile.write(octets)

        def do_GET(self):
            chemin = self.path.split("?")[0]
            try:
                if chemin in ("/", "/index.html"):
                    self.envoyer(page.read_bytes(), "text/html; charset=utf-8")
                elif chemin == "/api/competences":
                    self.envoyer(comp.read_bytes())
                elif chemin == "/api/agent":
                    d = a.donnees
                    etat = lire_json(d / "etat.json", {})
                    fichiers = []
                    for nom, f in (etat.get("fichiers") or {}).items():
                        fichiers.append({"nom": nom, "statut": f.get("statut"), "suivant": f.get("suivant"),
                                         "total": len(f.get("segments") or []) or f.get("suivant"),
                                         "ajouts": f.get("ajouts"), "deja_presents": f.get("deja_presents")})
                    props = [p for p in toutes_propositions(d / "propositions.jsonl") if p.get("statut") == "en_attente"]
                    rep = {"mtime_competences": str(comp.stat().st_mtime_ns) if comp.exists() else "0",
                           "biome": lire_json(d / "biome" / "agent.json", None),
                           "constats": lire_json(d / "constats.json", None),
                           "variables": lire_json(d / "variables.json", None),
                           "vivant": lire_json(d / "vivant.json", None), "fichiers": fichiers, "journal": fin_jsonl(d / "journal.jsonl", 80),
                           "a_valider": props[-300:], "nb_a_valider": len(props)}
                    self.envoyer(json.dumps(rep, ensure_ascii=False).encode("utf-8"))
                else:
                    self.envoyer(b"{}", code=404)
            except Exception as e:
                self.envoyer(json.dumps({"erreur": str(e)}).encode("utf-8"), code=500)

    serveur = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    url = "http://127.0.0.1:%d/" % a.port
    print("Tableau de bord : %s\nCompétences : %s\nÉtat agent  : %s\nCtrl+C pour arrêter." % (url, comp, a.donnees))
    if not a.sans_navigateur:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêté.")


if __name__ == "__main__":
    main()
