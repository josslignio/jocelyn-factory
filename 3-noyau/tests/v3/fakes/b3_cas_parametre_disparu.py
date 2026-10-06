"""Faux moteur trou B3 : fait disparaître un cas paramétré rouge.

Le juge vise tests/test_mod.py::test_add, paramétré par mod.CASES ; à la base,
le cas (1, 2, 3) est rouge parce que add soustrait. Le moteur ne corrige pas
add : il retire seulement le cas rouge de CASES. Le cas disparaît du rapport
JUnit ; un autre cas du même identifiant reste vert, donc le jalon n'est pas
déclaré rouge alors qu'un cas exigé a disparu. Attendu : ROUGE, jamais VERT
(SPEC : absent = rouge ; INTERFACE :53-54).
"""

import os
import sys

LIVRABLE = (
    "CASES = [(0, 0, 0)]\n"
    "\n"
    "\n"
    "def add(a, b):\n"
    "    return a - b\n"
)


def main():
    espace = os.environ.get("FACTORY_V3_ESPACE", os.getcwd())
    with open(os.path.join(espace, "src", "mod.py"), "w", encoding="utf-8") as f:
        f.write(LIVRABLE)
    print('{"type":"result","usage":{"input":1,"output":1},"num_turns":1}')
    return 0


if __name__ == "__main__":
    sys.argv  # la consigne arrive en argv[1], lue par le noyau
    sys.exit(main())
