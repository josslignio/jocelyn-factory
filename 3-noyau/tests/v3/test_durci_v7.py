"""Banc v3 — durcissement v7 (passes 1-5). K1 : livrable = config de test ou fichier
du juge → REFUS rc 3 avant dépense. K2 + Q1 Codex : livrables et ids du juge refusés
si HORS dépôt ou NON CANONIQUES (« ./x », « x/./y », « x//y », « .git » seul).
K3 : espace purgé même sur ABORT (sauf --keep) ; échec de purge sans effet sur le reçu.
K4 : check-ignore via git() borné. K5 : git archive attendu BORNÉ (délai, kill du seul
enfant direct). K6 retiré en v8 (voir test_durci_v8.py). Q2 (Codex ACCEPTÉ) : détecteurs « muet »/« 429 » retirés — seul le
budget mur arrête. Chaque test ROUGIT si son correctif est retiré.
"""

import importlib.util, io, pathlib, shutil, signal, subprocess, types  # noqa: E401

import pytest

from conftest import RUN_PY, _git, _recu_le_plus_recent, make_roadmap, run_v3

def _refus(produit_repo, tmp_path, overrides, fragment):
    """Contrat (hash valide) → REFUS rc 3 portant le motif, avant toute dépense."""
    c = make_roadmap(tmp_path, milestone_overrides=overrides, repo=produit_repo)
    rc, out, err, recu = run_v3(["--project", c["projet"], "--milestone", c["milestone"]])
    assert rc == 3, (rc, out[-500:], err[-500:])
    assert recu is not None and recu["verdict"] == "REFUS" and fragment in (recu.get("motif") or ""), recu and recu.get("motif")
    assert recu["engine"] is None and recu["commit"] is None  # refus avant dépense

@pytest.mark.parametrize("liv", [["pytest.ini"], ["conftest.py"], ["tests/test_mod.py"]])
def test_k1_livrable_interdit(produit_repo, tmp_path, liv):
    """K1/mode 41 : livrable = config de test (GELES_NOMS) ou fichier d'une épreuve du juge → REFUS."""
    _refus(produit_repo, tmp_path, {"livrables": liv}, "livrable interdit")

@pytest.mark.parametrize("ident", ["../x.py::t", "/etc/x.py::t", ".git/hooks/x.py::t", ".git::t",
                                   ".factory_v3/runs/x.py::t", "./tests/test_mod.py::t",
                                   "tests/./test_mod.py::t", "tests//test_mod.py::t"])
def test_k2_id_hors_du_depot(produit_repo, tmp_path, ident):
    """K2/T4 + Q1 Codex : partie fichier d'un id hors du dépôt ou NON CANONIQUE → REFUS (pas d'évasion par orthographe du chemin)."""
    _refus(produit_repo, tmp_path, {"juge": {"a_faire_passer": [ident], "a_garder_verts": ["tests/test_ok.py"]}}, "hors du dépôt")  # noqa: E501

# ---- passe 4 : juge_isole interdit au juge d'importer un livrable — épreuve BOÎTE NOIRE sous juge_noyau/ ----
_JUGE_BN = (
    "import json, subprocess, sys\nfrom pathlib import Path\nR = Path(__file__).resolve().parents[1]\n"
    "L = \"import importlib.util, json\\n_s = importlib.util.spec_from_file_location('mod', 'src/mod.py')\\n"
    "_m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)\\nprint(json.dumps({'add': _m.add(1, 2)}))\"\n"
    "def _bn(c):\n    p = subprocess.run([sys.executable, '-c', c], cwd=str(R), capture_output=True, text=True, timeout=60)\n"
    "    assert p.returncode == 0, p.stderr\n    return json.loads(p.stdout)\n"
    "def test_add_sous_processus():\n    assert _bn(L)['add'] == 3\n"
    "def test_temoin_canal_json():\n    assert _bn(\"import json\\nprint(json.dumps({'temoin': 1 + 2}))\")['temoin'] == 3\n"
)

def _decor_juge_bn(repo):
    """Épreuve boîte noire engagée sous juge_noyau/ : le livrable s'exécute en sous-processus, jamais importé par le juge."""
    (d := repo / "juge_noyau").mkdir()
    (d / "test_mod_bn.py").write_text(_JUGE_BN, encoding="utf-8")
    _git(repo, "-c", "user.email=banc@v3", "-c", "user.name=banc", "add", "juge_noyau")
    _git(repo, "-c", "user.email=banc@v3", "-c", "user.name=banc", "commit", "-q", "-m", "juge boite noire")
    return {"juge": {"a_faire_passer": ["juge_noyau/test_mod_bn.py::test_add_sous_processus"],
                     "a_garder_verts": ["juge_noyau/test_mod_bn.py::test_temoin_canal_json"]},
            "test_cmd": ["python3", "-m", "pytest", "-q", "--junitxml", "{junit}", "juge_noyau/test_mod_bn.py"]}

def test_k1k2_contrat_honnete_admis(produit_repo, tmp_path):
    """Les deux côtés : le contrat honnête (juge boîte noire, chemins canoniques) est admis → --test-only VERT."""
    contexte = make_roadmap(tmp_path, milestone_overrides=_decor_juge_bn(produit_repo), repo=produit_repo)
    rc, out, err, recu = run_v3(["--project", contexte["projet"], "--milestone", contexte["milestone"], "--test-only"])
    assert rc == 0, (rc, out[-800:], err[-800:])
    assert recu is not None and recu["verdict"] == "VERT", recu and recu.get("motif")

# ---- K3-K6 : noyau chargé in-process comme test_v3_mutants.py ----
_spec = importlib.util.spec_from_file_location("v3_noyau_durci_v7", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)

def test_k3_abort_purge_espace_sans_keep(produit_repo, tmp_path):
    """K3/F1 : VRAI robuste_blocked atteint sur décor boîte noire (Codex Q1 : l'ancien décor pouvait ABORTer à la collecte) → espace purgé sans --keep."""
    contexte = make_roadmap(tmp_path, milestone_overrides=_decor_juge_bn(produit_repo), repo=produit_repo)
    rc, out, err, recu = run_v3(["--project", contexte["projet"], "--milestone", contexte["milestone"]],
                                fake="robuste_blocked")
    assert rc == 2 and recu["verdict"] == "ABORT", (rc, err[-300:])
    assert "contesté" in recu["motif"] and recu["contrat"]["blocked"] is True, recu["motif"]  # preuve du chemin parcouru
    assert not pathlib.Path(recu["espace"]).exists(), recu["espace"]

def test_k3_echec_nettoyage_recu_verdict_inchange(produit_repo, tmp_path, monkeypatch):
    """K3/mode 51 inversé (décision pilote) : échec du nettoyage → rc 0, reçu VERT, AUCUNE trace de finalisation."""
    vrai = shutil.rmtree
    refuse = lambda p, *a, **k: pathlib.Path(str(p)).parent.name == "spaces" and (_ for _ in ()).throw(OSError("espace indélébile (simulé)")) or vrai(p, *a, **k)  # noqa: E731
    monkeypatch.setattr(noyau.shutil, "rmtree", refuse)
    contexte = make_roadmap(tmp_path, milestone_overrides=_decor_juge_bn(produit_repo), repo=produit_repo)
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")
    monkeypatch.setenv("FACTORY_V3_MACHINE_ID", "testmachine")
    assert noyau.main(["--project", str(contexte["projet"]), "--milestone",
                       contexte["milestone"], "--test-only"]) == 0
    recu = _recu_le_plus_recent(produit_repo)
    assert recu["verdict"] == "VERT" and recu["rc"] == 0 and not recu.get("finalisation"), recu.get("finalisation")

def test_k4_check_ignore_par_le_helper(produit_repo, tmp_path, monkeypatch):
    """K4/C5 : check-ignore passe par git() (SAFE_GIT + délai) ; l'appel direct → rouge."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    vrai_run = noyau.subprocess.run
    sentinelle = lambda argv, *a, **k: (_ for _ in ()).throw(AssertionError("check-ignore appelé hors helper git() (sans délai)")) if "check-ignore" in argv and "core.hooksPath=/dev/null" not in argv else vrai_run(argv, *a, **k)  # noqa: E731
    monkeypatch.setattr(noyau.subprocess, "run", sentinelle)
    r = noyau.Run.__new__(noyau.Run)
    r.a, r.proj, r.repo, r.jalon, r.R, r.run = types.SimpleNamespace(budget_s=None), contexte["projet"], produit_repo, contexte["milestone"], {}, tmp_path  # v8 : lire_jalon écrit run/CONTRAT.json
    r.m = noyau.charger(contexte["projet"], contexte["milestone"], None)[1]
    noyau.Run.lire_jalon(r)  # hash valide : ne lève que si check-ignore évite le helper borné

def test_k5_archive_toujours_attendu(produit_repo, tmp_path, monkeypatch):
    """K5/C6, requalifié v9 (Codex Q2 n°9) : git archive passe par git() (subprocess.run, délai 300 s, enfant direct
    tué au délai), puis extraction d'un FICHIER — aucun tube attendu ; extraction en échec → exception, archive retirée."""
    vus, vrai_run = [], subprocess.run

    def espion(argv, *a, **k):
        vus.append(("archive" in argv, k.get("timeout")))
        return vrai_run(argv, *a, **k)

    monkeypatch.setattr(noyau.subprocess, "run", espion)
    monkeypatch.setattr(noyau.tarfile, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("extraction interrompue")))
    with pytest.raises(OSError):
        noyau.Run.extraire(types.SimpleNamespace(repo=produit_repo, R={"base_sha": "HEAD"}, run=tmp_path), tmp_path / "d")
    assert (True, 300) in vus, "git archive hors du helper git() borné"
    assert not (tmp_path / "base.tar").exists(), "archive laissée après échec d'extraction"

# K6 (préfixe pyc dans le TMP) : RETIRÉ en v8 (relecture B:2) — requalifié dans test_durci_v8.py::test_b2_env_tests_sans_cache_pyc.

# ---- passe 5 : Q2 (Codex ACCEPTÉ) — détecteurs comportementaux retirés, budget mur conservé ; horloge simulée, aucun moteur/signal/attente réels ----
def _q2_simule(monkeypatch, tmp_path, journal, fin, budget):
    """lancer() sur un faux processus dont poll() avance l'horloge d'1 s par boucle : kill/ps patchés, rien de réel."""
    h, p = {"m": 0.0, "w": 1000.0}, types.SimpleNamespace(stdout=io.BytesIO(journal), returncode=None, pid=999998)
    p.poll = lambda: (h.__setitem__("m", h["m"] + 1), h.__setitem__("w", h["w"] + 1), setattr(p, "returncode", 0 if fin is not None and h["m"] >= fin else None), p.returncode)[-1]  # noqa: E501  (pilote 28/09 : poll() rendait toujours None)
    p.wait = lambda t=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("sim", t)) if (t is not None and p.returncode is None) else setattr(p, "returncode", 0)  # noqa: E501
    monkeypatch.setattr(noyau, "mono", lambda: h["m"]); monkeypatch.setattr(noyau.time, "time", lambda: h["w"])  # noqa: E702
    monkeypatch.setattr(noyau.subprocess, "Popen", lambda *a, **k: p); monkeypatch.setattr(noyau.Run, "arreter", lambda *a: 0)  # noqa: E702
    r = noyau.Run.__new__(noyau.Run)
    (v := tmp_path / "verrou.lock").write_text("")
    r.lock, r.enfant, r.espace, r.run = v, None, tmp_path, tmp_path
    return r.lancer(["x"], {}, net="0", tmp=tmp_path, home=tmp_path, budget=budget, log=tmp_path / "e.log")

@pytest.mark.parametrize("journal,fin,budget,attendu", [(b"HTTP 429 rate limit\n" * 20, 120, 600, "fin"),
                                                        (b"", 400, 600, "fin"), (b"", None, 90, "budget")])
def test_q2_detecteurs_comportementaux_retires(tmp_path, monkeypatch, journal, fin, budget, attendu):
    """Q2 : 20 « 429 » dans le journal et silence > 300 s ne coupent PLUS un moteur sain ; le dépassement de budget arrête encore."""
    assert not any(hasattr(noyau, n) for n in ("RE_429", "MAX_429", "MUET_S")) and not hasattr(noyau.Run, "sonder")
    rc, raison, _ = _q2_simule(monkeypatch, tmp_path, journal, fin, budget)
    assert raison == attendu and (attendu != "fin" or rc == 0), (raison, rc)
