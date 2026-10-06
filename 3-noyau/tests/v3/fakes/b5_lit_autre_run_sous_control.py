"""Faux moteur trou B5 : lit le run d'un autre jalon sous le dépôt de contrôle.

CONTROL (le dépôt de contrôle) est lisible en entier par le profil sandbox,
sans exclusion de ses .factory_v3/runs. Ce moteur ouvre la CONSIGNE.md d'un
run ancien placé sous le dépôt de contrôle et rapporte le résultat dans sa
sortie (engine.log) : B5_LECTURE_OK s'il lit, B5_REFUS sinon. INTERFACE §9 :
lecture refusée de « .factory_v3/runs des autres runs ». Attendu : accès
refusé, jamais une lecture franche.
"""

import os
import sys


def chemin_autre_run():
    p = os.path.realpath(__file__)
    for _ in range(4):                  # fakes → v3 → tests → racine de contrôle
        p = os.path.dirname(p)
    return os.path.join(p, ".factory_v3", "runs", "ancien_b5", "CONSIGNE.md")


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    try:
        with open(chemin_autre_run(), "r", encoding="utf-8") as f:
            extrait = f.read(48)
        print("B5_LECTURE_OK extrait=%r" % extrait[:32])
    except OSError as exc:
        print("B5_REFUS errno=%s %s" % (exc.errno, exc.strerror))
    with open(os.path.join(espace, "src", "mod.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
