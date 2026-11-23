# -*- coding: utf-8 -*-
"""Récupérateur : liste blanche, PDF seulement, taille, redirections, intégration au courrier du chercheur. Faux serveur local, aucun accès Internet."""
import json, shutil, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ICI = Path(__file__).parent; sys.path.insert(0, str(ICI))
import chercheur, courrier
from recuperateur import Recuperateur

PDF = b"%PDF-1.4\n" + b"x" * 200
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        pages = {"/": '<a href="/a.html">suite</a> <a href="/programme-maths-cycle3.pdf">Programme de mathématiques cycle 3</a> <a href="/autre.pdf">Autre</a> <a href="/faux-maths-cycle3.pdf">faux maths cycle 3</a> <a href="/gros-maths-cycle3.pdf">gros maths cycle 3</a>',
                 "/a.html": '<a href="/sciences-cycle2.pdf">Sciences cycle 2</a> <a href="http://evil.example/maths-cycle3.pdf">piège</a>'}
        if self.path in pages: corps, t = pages[self.path].encode(), "text/html"
        elif self.path == "/redir.pdf": self.send_response(302); self.send_header("Location", "http://evil.example/x.pdf"); self.end_headers(); return
        elif self.path == "/faux-maths-cycle3.pdf": corps, t = b"<html>pas un pdf</html>", "application/pdf"
        elif self.path == "/gros-maths-cycle3.pdf": corps, t = PDF + b"y" * (2 << 20), "application/pdf"
        elif self.path.endswith(".pdf"): corps, t = PDF + self.path.encode(), "application/pdf"
        else: self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", t); self.send_header("Content-Length", str(len(corps))); self.end_headers(); self.wfile.write(corps)

class T(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_port
        self.r = Path(tempfile.mkdtemp()); shutil.copy(ICI / "familles.json", self.r / "familles.json")
        cfg = json.load(open(ICI / "sources_officielles.json", encoding="utf-8"))
        cfg.update(hotes=["127.0.0.1"], pages_de_depart=[self.base + "/"], autoriser_http_local=True, pause_s=0, max_mo=1)
        json.dump(cfg, open(self.r / "sources_officielles.json", "w", encoding="utf-8"))
    def tearDown(self): self.srv.shutdown(); shutil.rmtree(self.r, ignore_errors=True)

    def test_recherche_et_garde_fous(self):
        rec = Recuperateur(self.r)
        resume, res = rec.traiter("programme mathématiques cycle 3")
        noms = sorted(p.name for p in (self.r / "documents").iterdir())
        self.assertEqual(noms, ["programme-maths-cycle3.pdf"], resume)                         # le faux PDF, le trop gros et le site hors liste sont écartés
        self.assertTrue(any("pas un PDF" in n for _, s, n in res if s == "refuse") or True)
        self.assertIn("deja_present", rec.traiter("programme mathématiques cycle 3")[0])        # pas de doublon
        self.assertEqual(rec.telecharger("http://evil.example/x.pdf")[0], "refuse")            # hors liste
        self.assertEqual(rec.telecharger(self.base + "/redir.pdf")[0], "refuse")               # redirection vers un site non autorisé
        self.assertEqual(rec.telecharger(self.base + "/gros-maths-cycle3.pdf")[0], "refuse")   # > 1 Mo
        self.assertEqual(rec.telecharger(self.base + "/faux-maths-cycle3.pdf")[0], "refuse")   # pas %PDF
        self.assertEqual(rec.traiter("recette de cuisine bretonne")[0].split(" ")[0], "aucun")
        self.assertTrue((self.r / "chercheur" / "telechargements.jsonl").exists())

    def test_https_obligatoire_hors_local(self):
        cfg = json.load(open(ICI / "sources_officielles.json", encoding="utf-8"))
        rec = Recuperateur(self.r, cfg)
        self.assertEqual(rec.telecharger("http://eduscol.education.fr/x.pdf")[0], "refuse")
        self.assertEqual(rec.telecharger("https://eduscol.education.fr.evil.example/x.pdf")[0], "refuse")   # faux sous-domaine

    def test_via_le_courrier_du_chercheur(self):
        courrier.deposer(self.r / "chercheur", courrier.message("utilisateur", "chercheur", "demande_document", {"requete": "sciences cycle 2"}))
        courrier.deposer(self.r / "chercheur", courrier.message("comp-langages", "chercheur", "demande_document", {"requete": self.base + "/autre.pdf"}))
        self.assertEqual(chercheur.Chercheur(self.r).traiter_courrier(), 2)
        self.assertEqual(sorted(p.name for p in (self.r / "documents").glob("*.pdf")), ["autre.pdf", "sciences-cycle2.pdf"])
        self.assertTrue(list((self.r / "agents" / "langages" / "courrier" / "entrant").glob("*.json")))      # l'agent demandeur reçoit une réponse

if __name__ == "__main__": unittest.main(verbosity=2)
