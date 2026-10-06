"""Faux moteur honnête n°1 : corrige le livrable src/mod.py.

Scénario du banc : le dépôt contient src/mod.py avec add(a, b) qui retourne
a - b (bug volontaire). Ce moteur ne touche qu'au livrable déclaré du jalon
(src/mod.py) et remet l'addition correcte. Attendu : verdict VERT.
"""

import os
import sys


def espace():
    """Retourne le chemin de l'espace de travail du run."""
    return os.environ.get("FACTORY_V3_ESPACE", os.getcwd())


def main():
    chemin = os.path.join(espace(), "src", "mod.py")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
