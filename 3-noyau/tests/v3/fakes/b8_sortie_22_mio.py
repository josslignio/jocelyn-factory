"""Faux moteur trou B8 : 22 Mio sur la sortie → engine.log sous son plafond.

INTERFACE §3 annonce engine.log « plafond 20 Mo, tronqué avec marqueur ».
Ce moteur rend un travail honnête puis écrit 22 Mio sur sa sortie standard.
Le plafond annoncé doit tenir : le journal final doit rester ≤ 20 Mo.
(Faux moteur ajouté par le banc lot 2 : aucun moteur du catalogue n'émet de
sortie volumineuse, le test B8 en a besoin.)
"""

import os
import sys

TOTAL = 22 * 1024 * 1024
BLOC = b"x" * 65536


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    with open(os.path.join(espace, "src", "mod.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    sys.stdout.write("B8 debut du flux volumineux\n")
    sys.stdout.flush()
    reste = TOTAL
    while reste > 0:
        n = min(len(BLOC), reste)
        sys.stdout.buffer.write(BLOC[:n])
        sys.stdout.buffer.flush()
        reste -= n
    sys.stdout.write("\nB8 fin du flux volumineux\n")
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
