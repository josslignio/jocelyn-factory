"""Faux moteur du DIAGNOSTIC D1 (scénario H2) : dépasse son budget, sans rien détacher.

Modèle : cand_v6/tests/v3/fakes/honnete_livrable.py (catalogue exigé par run.py:327-329 —
Claude copie ce fichier dans cand_v6/tests/v3/fakes/, jamais exécuté depuis le lot G).
N'ouvre AUCUN sous-processus : dort EXACTEMENT SOMMEIL_S = 7 secondes — CONSTANTE CODÉE,
aucune variable d'environnement ne la change (correction CTO passe 5 : un H2 borné) —
puis écrit le même correctif que honnete_livrable (src/mod.py : add retourne la somme)
et sort 0.
Lancé par lanceur_diag AVEC --budget-s 5 exactement (refus rc 3 sinon) : budget mur du
noyau à 5 s ; arreter (run.py:462-475) émet SIGTERM — journalisé, NON émis, signaux
neutralisés par le lanceur — puis attend sa grâce de 5 s. 7 < 5+5 : ce moteur sort SEUL
pendant la grâce ; SIGKILL jamais atteint, aucun survivant ne reste sur le Mac.
"""

import os
import sys
import time

SOMMEIL_S = 7.0  # constante codée : 7 < grâce 5+5 du profil faux moteur (run.py:333)


def espace():
    """Retourne le chemin de l'espace de travail du run."""
    return os.environ.get("FACTORY_V3_ESPACE", os.getcwd())


def main():
    time.sleep(SOMMEIL_S)
    chemin = os.path.join(espace(), "src", "mod.py")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
