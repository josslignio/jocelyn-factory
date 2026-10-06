"""L2 (02/10) — publication idempotente. Un noyau tué autour de la création de branche, puis rejoué, laisse UNE branche et
UN commit publiés ; une branche existante n'est jamais forcée ni doublée ; altérée (base, contrat, octets, hors livrables) → refus.
Hermétique : faux moteur honnête du catalogue, panne injectée par SIGKILL du noyau lui-même juste avant / après `git branch`."""
import os, signal, subprocess, sys  # noqa: E401
import pytest
import yaml
from conftest import RUN_PY, RACINE, make_roadmap, run_v3, _git

HARNAIS = (  # vrai run.py, seul `git branch` encadré d'un SIGKILL (fenêtre exacte de la panne)
    "import os, signal, sys, importlib.util\n"
    "s = importlib.util.spec_from_file_location('run_l2', sys.argv[1]); k = importlib.util.module_from_spec(s); s.loader.exec_module(k)\n"
    "vrai, mode = k.git, os.environ['L2_TUER']\n"
    "def git(repo, *a, **kw):\n"
    "    if a[:1] == ('branch',) and mode == 'avant': os.kill(os.getpid(), signal.SIGKILL)\n"
    "    r = vrai(repo, *a, **kw)\n"
    "    if a[:1] == ('branch',) and mode == 'apres': os.kill(os.getpid(), signal.SIGKILL)\n"
    "    return r\n"
    "k.git = git\nsys.exit(k.main(sys.argv[2:]))\n")


def _env(**kw):
    return {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "FACTORY_V3_MACHINE_ID": "testmachine",
            "FACTORY_V3_FAKE_ENGINE": "honnete_livrable", **kw}


def _publies(repo, base):
    """Faits : branches v3/* et commits atteignables depuis une branche, hors ancêtres de la base."""
    br = {b: _git(repo, "rev-parse", b).strip() for b in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/v3/").split()}
    return br, _git(repo, "rev-list", "--branches", "^" + base).split()


def _decor(tmp_path, produit_repo):
    d = make_roadmap(tmp_path, repo=produit_repo)
    base = _git(produit_repo, "rev-parse", "HEAD").strip()
    hc = yaml.safe_load(d["roadmap"].read_text())["milestones"][0]["valide"]["hash_contrat"]
    return d, base, hc, f"v3/M1-{base[:8]}-{hc[:16]}"


@pytest.mark.parametrize("moment", ["avant", "apres"])
def test_noyau_tue_autour_de_la_branche_rejeu_un_seul_commit(tmp_path, produit_repo, moment):
    """SIGKILL du noyau juste avant / juste après `git branch` (reçu resté RUNNING), puis rejeu : 1 branche, 1 commit, VERT,
    et le reçu du rejeu nomme le commit publié (après : CELUI du run tué, jamais un second)."""
    d, base, hc, nom = _decor(tmp_path, produit_repo)
    p = subprocess.run([sys.executable, "-c", HARNAIS, str(RUN_PY), "--project", str(d["projet"]), "--milestone", "M1"],
                       cwd=str(RACINE), env=_env(L2_TUER=moment), capture_output=True, timeout=180)
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stderr[-400:])
    avant, _ = _publies(produit_repo, base)
    assert len(avant) == (moment == "apres"), avant
    rc, out, err, R = run_v3(["--project", d["projet"], "--milestone", "M1"], fake="honnete_livrable", timeout=180)
    br, commits = _publies(produit_repo, base)
    assert (rc, R["verdict"]) == (0, "VERT"), (rc, out[-300:], err[-300:])
    assert len(br) == 1 and len(commits) == 1, (br, commits)
    assert R["commit"]["sha"] == commits[0] and R["commit"]["branch"] == nom and br == {nom: commits[0]}, (R["commit"], br)
    if moment == "apres":
        assert br == avant, (avant, br)                                  # la branche du run tué est retrouvée, jamais déplacée


def _commit(repo, base, parent, msg, fichiers):
    """Commit fabriqué hors noyau (plomberie git) : arbre de la base + fichiers donnés, parent donné."""
    idx = repo.parent / "l2.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(idx), "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
           "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x"}
    g = lambda *a, i=None: subprocess.run(["git", "-C", str(repo), *a], env=env, input=i, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    g("read-tree", base)
    for f, txt in fichiers.items():
        g("update-index", "--add", "--cacheinfo", f"100644,{g('hash-object', '-w', '--stdin', i=txt)},{f}")
    sha = g("commit-tree", g("write-tree"), "-p", parent, "-m", msg)
    idx.unlink()
    return sha


HONNETE = "def add(a, b):\n    return a + b\n"                          # octets exacts du faux moteur honnete_livrable


@pytest.mark.parametrize("cas", ["identique", "octets", "contrat", "parent", "hors_livrables"])
def test_branche_existante_retrouvee_ou_refusee(tmp_path, produit_repo, cas):
    """Deux côtés : branche déterministe déjà présente — identique (base, contrat, octets jugés) → VERT sans nouveau commit ;
    altérée → ABORT « publication altérée », branche intacte (jamais forcée), aucune autre branche créée."""
    d, base, hc, nom = _decor(tmp_path, produit_repo)
    msg = f"v3: M1 essai 1\n\nrun_uuid: autre\nhash_contrat: {'f' * 64 if cas == 'contrat' else hc}\nbase: {base}\n"
    parent = _commit(produit_repo, base, base, "intermédiaire", {}) if cas == "parent" else base
    fichiers = {"src/mod.py": "def add(a, b):\n    return b + a\n" if cas == "octets" else HONNETE}
    if cas == "hors_livrables":
        fichiers["src/autre.py"] = "x = 1\n"
    sha = _commit(produit_repo, base, parent, msg, fichiers)
    _git(produit_repo, "branch", nom, sha)
    rc, out, err, R = run_v3(["--project", d["projet"], "--milestone", "M1"], fake="honnete_livrable", timeout=180)
    br, _ = _publies(produit_repo, base)
    assert br == {nom: sha}, (cas, br)                                   # jamais forcée, jamais doublée
    if cas == "identique":
        assert (rc, R["verdict"], R["commit"]["sha"]) == (0, "VERT", sha), (cas, rc, R.get("motif"))
    else:
        assert (rc, R["verdict"], R["motif_echec"], R["commit"]) == (2, "ABORT", "publication altérée", None), (cas, rc, R.get("motif"))


def test_bout_en_bout_mf_noyau_tue_apres_branche_puis_vert(tmp_path, produit_repo, monkeypatch):
    """Chaîne réelle mf → noyau (faux moteur) : noyau tué juste après `git branch`, mf vivant → panne technique, pret ;
    `suivant` suivant → VERT accepté, preuve = LE commit déjà publié ; 1 branche, 1 commit."""
    import importlib.util, shlex, time  # noqa: E401
    d, base, hc, nom = _decor(tmp_path, produit_repo)
    for k, v in _env(MF_ETAT=str(tmp_path / "etat"), MF_MOTEURS_RELIES="glm", L2_TUER="apres",
                     MF_NOYAU=f"{sys.executable} -c {shlex.quote(HARNAIS)} {RUN_PY}").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MF_MOTEUR_FORCE", raising=False)
    s = importlib.util.spec_from_file_location(f"mf_l2b_{time.time_ns()}", RUN_PY.parents[2] / "mini_factory" / "mf.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    assert m.main(["ajouter", "T", "--projet", str(d["projet"]), "--jalon", "M1"]) == 0
    m.main(["suivant"]); t = m.lire()["T"]
    assert (t["etat"], t["demarrages"][-1].get("technique")) == ("pret", True), t
    avant, _ = _publies(produit_repo, base)
    monkeypatch.setenv("L2_TUER", "non")
    assert m.main(["suivant"]) == 0
    t, (br, commits) = m.lire()["T"], _publies(produit_repo, base)
    assert (t["etat"], t["preuve"]["commit"], br, len(commits)) == ("accepte", avant[nom], avant, 1), (t, br, commits)
