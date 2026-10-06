"""Faux moteur robustesse n°2 : se tait plus longtemps que le budget moteur.

Il corrige d'abord src/mod.py (le travail honnête est fait), puis dort
400 secondes sans rien écrire — ni fichier, ni ligne résultat. Le noyau doit
appliquer le mur de budget (--budget-s) et le tuer : le banc lance ce moteur
avec un budget court et attend un ABORT côté reçu, pas un run qui traîne.
La ligne résultat n'est imprimée qu'après le sommeil, donc jamais atteinte
si le mur fonctionne.
"""

import os
import sys
import time


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    with open(os.path.join(espace, "src", "mod.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    sys.stdout.flush()
    time.sleep(400)
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
