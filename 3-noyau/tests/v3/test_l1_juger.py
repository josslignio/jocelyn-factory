"""L1 (02/10) : run.py --juger — UN SEUL chemin de jugement pour l'acceptation et l'intégration : le même jouer_tests que le
noyau (bac NET=0, HOME jetable, verdict par tube), verdict sur la sortie standard du noyau. Chaque refus est éprouvé des DEUX
côtés : le même test passe hors bac (pytest nu) et échoue sous --juger — sinon l'épreuve ne prouverait rien."""
import json
import os
import pathlib
import subprocess
import sys

RUN_PY = pathlib.Path(__file__).resolve().parents[2] / "factory_v3" / "run.py"
ENV = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1"}


def arbre(tmp_path, fichiers):
    a = tmp_path / "arbre"
    for nom, txt in fichiers.items():
        (a / nom).parent.mkdir(parents=True, exist_ok=True)
        (a / nom).write_text(txt, encoding="utf-8")
    return a


def juger(a, *args, livrables="", budget="120"):
    p = subprocess.run([sys.executable, str(RUN_PY), "--juger", str(a), "--livrables", livrables, "--budget-s", budget, "--",
                        "-q", "-p", "no:cacheprovider", *args], capture_output=True, text=True, timeout=300, env=ENV)
    lignes = p.stdout.strip().splitlines()
    return p.returncode, (json.loads(lignes[-1]) if lignes else None), p


def nu(a, *args):
    """Même test, hors bac (pytest nu, environnement hérité) : le côté « honnête » de chaque épreuve."""
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=a, capture_output=True,
                          text=True, timeout=300).returncode


def test_etats_et_verdict_sur_stdout(tmp_path):
    a = arbre(tmp_path, {"t/test_e.py": "import pytest\ndef test_v():\n    pass\ndef test_r():\n    assert False\n"
                         "@pytest.mark.skip\ndef test_s():\n    pass\n"})
    rc, v, _ = juger(a, "t/test_e.py")
    assert rc == 0 and v["rc"] == 1
    assert v["cas"] == {"t/test_e.py::test_v": "pass", "t/test_e.py::test_r": "fail", "t/test_e.py::test_s": "skip"}
    assert not [x for x in tmp_path.iterdir() if x.name.startswith(".bac_")], "dossier de run du bac non effacé"


def test_home_jetable(tmp_path):
    a = arbre(tmp_path, {"test_h.py": "from pathlib import Path\ndef test_h():\n    assert (Path.home() / 'Library').is_dir()\n"})
    assert nu(a, "test_h.py") == 0                      # hors bac : le HOME réel est lu → vert
    assert juger(a, "test_h.py")[1]["cas"] == {"test_h.py::test_h": "fail"}


def test_reseau_coupe(tmp_path):
    a = arbre(tmp_path, {"test_n.py": "import socket\ndef test_n():\n    s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
                                      "    socket.create_connection(s.getsockname(), timeout=2).close()\n"})
    assert nu(a, "test_n.py") == 0
    assert juger(a, "test_n.py")[1]["cas"] == {"test_n.py::test_n": "fail"}


def test_ni_lecture_ni_ecriture_hors_du_bac(tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    a = arbre(tmp_path, {"test_f.py": f"import pathlib\nP = pathlib.Path({str(tmp_path)!r})\n"
                                      "def test_lit():\n    assert (P / 'secret.txt').read_text() == 's'\n"
                                      "def test_ecrit_dehors():\n    (P / 'marque').write_text('x')\n"
                                      "def test_ecrit_dans_l_arbre():\n    (P / 'arbre' / 'marque').write_text('x')\n"})
    assert nu(a, "test_f.py") == 0
    (tmp_path / "marque").unlink(); (a / "marque").unlink()
    assert set(juger(a, "test_f.py")[1]["cas"].values()) == {"fail"}
    assert not (tmp_path / "marque").exists() and not (a / "marque").exists()


def test_faux_verdict_imprime_par_le_code_teste_sans_effet(tmp_path):
    faux = json.dumps({"rc": 0, "cas": {"test_x.py::test_x": "pass"}})
    a = arbre(tmp_path, {"test_x.py": f"import sys\ndef test_x():\n    print({faux!r}); sys.stdout.flush(); assert False\n"})
    rc, v, p = juger(a, "-s", "test_x.py")
    assert v == {"rc": 1, "cas": {"test_x.py::test_x": "fail"}} and p.stdout.count("\n") == 1


def test_livrable_garde_hors_du_processus_du_juge(tmp_path):
    a = arbre(tmp_path, {"src/mod.py": "X = 1\n", "test_l.py": "import runpy, pathlib\ndef test_l():\n"
                         "    assert runpy.run_path(str(pathlib.Path(__file__).parent / 'src' / 'mod.py'))['X'] == 1\n"})
    assert juger(a, "test_l.py")[1]["cas"] == {"test_l.py::test_l": "pass"}
    assert juger(a, "test_l.py", livrables="src/mod.py")[1]["cas"] == {"test_l.py::test_l": "fail"}


def test_sans_verdict_aucun_cas_et_rc_2(tmp_path):
    a = arbre(tmp_path, {"test_lent.py": "import time\ndef test_lent():\n    time.sleep(60)\n"})
    rc, v, _ = juger(a, "test_lent.py", budget="3")
    assert rc == 2 and v == {"rc": 124, "cas": None}
