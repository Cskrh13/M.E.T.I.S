# -*- coding: utf-8 -*-
"""ollama_client.py — Agent 4.2
Communication avec Ollama (local). Aucune dépendance externe (bibliothèque standard).
Le LLM est facultatif : si Ollama est absent, l'appelant reçoit une LLMError
et l'agent continue à travailler sans lui (audit, validations, etc.).
"""
import json
import re
import urllib.request
import urllib.error


class LLMError(Exception):
    """Erreur de communication ou de format avec le modèle."""


class OllamaClient:
    def __init__(self, url="http://localhost:11434", modele="qwen3.5:4b",
                 timeout=900, num_ctx=4096, num_predict=1400):
        self.url = url.rstrip("/")
        self.modele = modele
        self.timeout = timeout
        self.num_ctx = num_ctx          # fixé explicitement : évite le dépassement de contexte
        self.num_predict = num_predict
        self.derniere_stats = {}        # statistiques de la dernière réponse (jetons, durées en secondes)

    # ------------------------------------------------------------------
    def _post(self, chemin, charge, timeout):
        donnees = json.dumps(charge).encode("utf-8")
        req = urllib.request.Request(self.url + chemin, data=donnees,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise LLMError("Ollama injoignable : %s" % e)
        except Exception as e:  # délai dépassé, JSON illisible...
            raise LLMError("Erreur Ollama : %s" % e)

    def disponible(self):
        """True si le serveur répond et si le modèle est installé."""
        try:
            req = urllib.request.Request(self.url + "/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                tags = json.loads(r.read().decode("utf-8"))
            noms = [m.get("name", "") for m in tags.get("models", [])]
            return any(n == self.modele or n.startswith(self.modele + ":") for n in noms)
        except Exception:
            return False

    # ------------------------------------------------------------------
    def demander_json(self, systeme, prompt):
        """Envoie une demande et renvoie un dict Python (JSON validé syntaxiquement)."""
        rep = self._post("/api/chat", {
            "model": self.modele,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict},
            "messages": [{"role": "system", "content": systeme},
                         {"role": "user", "content": prompt}],
        }, self.timeout)
        texte = (rep.get("message") or {}).get("content", "")
        ns = 1e9
        self.derniere_stats = {
            "jetons_prompt": rep.get("prompt_eval_count"),
            "jetons_reponse": rep.get("eval_count"),
            "duree_totale_s": round((rep.get("total_duration") or 0) / ns, 1),
            "duree_chargement_s": round((rep.get("load_duration") or 0) / ns, 1),
            "duree_prompt_s": round((rep.get("prompt_eval_duration") or 0) / ns, 1),
            "duree_generation_s": round((rep.get("eval_duration") or 0) / ns, 1),
        }
        return self.extraire_json(texte)

    @staticmethod
    def extraire_json(texte):
        texte = (texte or "").strip()
        texte = re.sub(r"<think>.*?</think>", "", texte, flags=re.S).strip()
        texte = re.sub(r"^```(?:json)?|```$", "", texte, flags=re.M).strip()
        try:
            return json.loads(texte)
        except Exception:
            pass
        debut, fin = texte.find("{"), texte.rfind("}")
        if debut != -1 and fin > debut:
            try:
                return json.loads(texte[debut:fin + 1])
            except Exception:
                pass
        raise LLMError("Réponse non JSON : %r" % texte[:200])
