"""Faux moteur R2 (lacune) : enfant détaché sans aucune marque repérable.

L'enfant échappe aux trois filets du noyau : pas de marque d'environnement
(env minimal, sans FACTORY_V3_RUN_UUID), cwd=/ (aucun chemin vers l'espace
ou le run), entrées-sorties sur /dev/null. Le moteur rend sinon un travail
honnête. Si le noyau ne peut pas tuer cet enfant, il survit au run : c'est
le vecteur des corrections posthumes. Attendu : aucun survivant après la
fin du run (INTERFACE :91-92, robustesse).
"""

import os
import subprocess
import sys

CODE_ENFANT = "import time\ntime.sleep(120)\n# cible_r2_survivante"


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    with open(os.path.join(espace, "src", "mod.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    try:
        subprocess.Popen(
            [sys.executable, "-c", CODE_ENFANT], cwd="/",
            env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        pass
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
