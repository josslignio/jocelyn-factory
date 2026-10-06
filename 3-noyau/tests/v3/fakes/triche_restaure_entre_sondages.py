"""Faux moteur requalifié (décision relecteur adverse, 24/09) : le descendant finit après le CLI.

L'attaque d'origine — faire alterner corrigé/bugué dans src/mod.py pour désynchroniser
des échantillonnages du noyau — est MORTE par construction : un seul gel (copier_livrables,
au contrat), un espace de jugement neuf, zéro ré-échantillonnage ; l'alternance ne servait
plus qu'à rendre le verdict non déterministe, elle est retirée. Le scénario mesure ce qui
reste : le moteur n'écrit RIEN lui-même, il lance un enfant détaché (setsid, cwd=/, env
nue : invisible au balayage des survivants) qui attend la mort du CLI (fin du tube) puis
termine le travail — écrit la correction dans le livrable et la maintient pendant 10 s,
donc APRÈS le retour du CLI, autour de la capture du gel. Ces octets sont gelés, jugés,
publiés : VERT. Conforme au contrat requalifié (INTERFACE §11) ; l'obligation non
satisfaite — refuser un VERT forgé après la mort du CLI — est mesurée ROUGE par
tests/v3/test_v3_trous.py::test_r3_residuel_ecriture_avant_capture.
"""

import os
import subprocess
import sys


ENFANT = (
    "import os, sys, time\n"
    "chemin = os.path.join(sys.argv[1], 'src', 'mod.py')\n"
    "os.read(3, 1)\n"                   # fin du tube = retour du CLI
    "fin = time.time() + 10\n"
    "while time.time() < fin:\n"
    "    try:\n"
    "        with open(chemin, 'w', encoding='utf-8') as f:\n"
    "            f.write('def add(a, b):\\n    return a + b\\n')\n"
    "    except OSError:\n"
    "        pass\n"
    "    time.sleep(0.05)\n"
)


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    r, w = os.pipe()
    try:
        subprocess.Popen(
            [sys.executable, "-c", ENFANT, espace], cwd="/",
            env={"PATH": "/usr/bin:/bin"}, pass_fds=(r,),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    finally:
        os.close(r)
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    os.close(w)                         # sa mort ferme le tube : l'enfant écrit
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
