"""Faux moteur honnête n°2 : ajoute un nouveau test listé dans les livrables.

Le moteur cherche dans la consigne un chemin de test qui n'existe pas encore
dans l'espace, puis crée ce fichier avec un test vert même avec le bug
volontaire de add (add(0, 0) == 0 passe avec a - b comme avec a + b).
Le test charge src/mod.py par chemin absolu, sans dépendre de sys.path.
Il ne touche à aucun fichier gelé. Attendu : verdict VERT.
"""

import os
import re
import sys


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    consigne = sys.argv[1] if len(sys.argv) > 1 else ""
    cible = None
    try:
        with open(consigne, "r", encoding="utf-8") as f:
            texte = f.read()
        for chemin in re.findall(r"tests/[\w/]+\.py", texte):
            if not os.path.exists(os.path.join(espace, chemin)):
                cible = chemin
                break
    except OSError:
        pass
    if cible is None:
        cible = "tests/test_plus.py"
    complet = os.path.join(espace, cible)
    os.makedirs(os.path.dirname(complet), exist_ok=True)
    with open(complet, "w", encoding="utf-8") as f:
        f.write(
            "import importlib.util\n"
            "import os.path\n"
            "_p = os.path.join(os.path.dirname(__file__), '..', 'src', 'mod.py')\n"
            "_s = importlib.util.spec_from_file_location('mod', _p)\n"
            "_m = importlib.util.module_from_spec(_s)\n"
            "_s.loader.exec_module(_m)\n\n"
            "def test_add_zero():\n"
            "    assert _m.add(0, 0) == 0\n"
        )
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
