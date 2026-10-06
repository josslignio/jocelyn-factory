"""Faux moteur robustesse n°3 : déclare le contrat contradictoire.

Il ne touche à rien — ni livrable, ni gelé, ni .git — et écrit uniquement
FACTORY_V3_BLOCKED.md à la racine de l'espace pour signaler que le test et
le but se contredisent : test_add exige add(1, 2) == 3 alors que le but lu
dans la consigne demande la soustraction. Le noyau doit traduire ce signal
en rc 2 « contrat contesté » (verdict ABORT), pas en ROUGE silencieux ni en
VERT. Aucune autre écriture, aucune correction.
"""

import os
import sys

BLOCAGE = """# Blocage signalé par le moteur

Le test et le but se contredisent, je refuse de trancher.

- But lu dans la consigne : faire que add(a, b) retourne la somme.
- Test à faire passer : tests/test_mod.py::test_add exige add(1, 2) == 3.
- Code de base : add(a, b) retourne a - b.

Corriger le livrable rendrait le test vert, mais je ne peux pas vérifier
que le but exprimé est bien celui-là : les deux énoncés divergent sur
l'opération attendue. Décision humaine requise.
"""


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    chemin = os.path.join(espace, "FACTORY_V3_BLOCKED.md")
    try:
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(BLOCAGE)
    except OSError:
        pass
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
