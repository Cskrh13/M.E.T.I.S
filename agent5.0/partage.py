# -*- coding: utf-8 -*-
"""partage.py — Grand Planificateur 5.0 : verrou inter-processus pour les instances d'un même agent.

Plusieurs instances d'agent-compétences (processus distincts) travaillent sur le MÊME competences.json et le
même dossier d'état. Ce module fournit le seul mécanisme de coordination : un verrou-fichier créé de façon
atomique (O_CREAT | O_EXCL, valable sous Windows comme ailleurs). Bibliothèque standard uniquement.

Règle d'usage (voir agent4_2_core.Agent.transaction) : on ne garde le verrou que le temps de
« relire l'état partagé -> appliquer un résultat -> réécrire ». Jamais pendant un appel au LLM.
"""
import os
import threading
import time


class VerrouFichier:
    """Verrou réentrant, exclusif entre processus ET entre fils d'un même processus."""

    def __init__(self, chemin, obsolete_s=120, attente_max_s=600):
        self.chemin = str(chemin)
        self.obsolete_s = obsolete_s          # un verrou plus vieux que cela est celui d'un processus mort
        self.attente_max_s = attente_max_s
        self._fil = threading.RLock()
        self._profondeur = 0
        self.attente_s = 0.0                  # temps cumulé passé à attendre le verrou (mesuré par l'agent-contrôle)

    def _prendre_fichier(self):
        debut = time.time()
        pause = 0.02
        while True:
            try:
                fd = os.open(self.chemin, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, ("%d %f\n" % (os.getpid(), time.time())).encode("ascii"))
                finally:
                    os.close(fd)
                return
            except FileExistsError:
                pass
            except PermissionError:           # Windows : fichier en cours de suppression par l'autre processus
                pass
            try:
                if time.time() - os.path.getmtime(self.chemin) > self.obsolete_s:
                    # verrou orphelin : on le déplace (renommage atomique, un seul gagnant) puis on le supprime
                    morceau = "%s.orphelin.%d.%d" % (self.chemin, os.getpid(), threading.get_ident())
                    os.replace(self.chemin, morceau)
                    os.unlink(morceau)
                    continue
            except OSError:
                pass
            if time.time() - debut > self.attente_max_s:
                raise TimeoutError("verrou %s non obtenu après %d s" % (self.chemin, self.attente_max_s))
            time.sleep(pause)
            pause = min(0.25, pause * 1.5)

    def __enter__(self):
        self._fil.acquire()
        try:
            if self._profondeur == 0:
                t0 = time.time()
                try:
                    self._prendre_fichier()
                finally:
                    self.attente_s += time.time() - t0
            self._profondeur += 1
        except BaseException:
            self._fil.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._profondeur -= 1
            if self._profondeur == 0:
                try:
                    os.unlink(self.chemin)
                except OSError:
                    pass
        finally:
            self._fil.release()
        return False
