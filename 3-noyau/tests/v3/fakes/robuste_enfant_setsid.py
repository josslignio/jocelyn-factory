"""Faux moteur robustesse n°1 : laisse un enfant détaché derrière lui.

Il corrige honnêtement src/mod.py (livrable du jalon), puis lance
« setsid sleep 600 » détaché (nouvelle session, stdout/stderr fermés) avant
de sortir. Le noyau doit tuer tout l'arbre du moteur, y compris ce petit-
fils : le banc vérifie qu'aucun « sleep 600 » ne survit au run. Attendu :
VERT côté contrat, aucun orphelin côté processus.
"""

import os
import subprocess
import sys


def lancer_enfant():
    """Lance sleep 600 dans sa propre session, en survivant au père."""
    try:
        enfant = subprocess.Popen(
            ["setsid", "sleep", "600"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        with open(os.path.join(os.environ["FACTORY_V3_RUN_DIR"], "engine", "enfant_setsid.pid"), "w") as f:
            f.write(str(enfant.pid))
        return
    except (OSError, FileNotFoundError):
        pass
    try:
        enfant = subprocess.Popen(
            ["sleep", "600"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        with open(os.path.join(os.environ["FACTORY_V3_RUN_DIR"], "engine", "enfant_setsid.pid"), "w") as f:
            f.write(str(enfant.pid))
    except OSError:
        pass


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    with open(os.path.join(espace, "src", "mod.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    lancer_enfant()
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
