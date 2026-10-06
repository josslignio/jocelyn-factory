"""Banc v3 — durcissement v8 (relectures A frontière, B verdict, C robustesse, 28/09).
Tests PURS : noyau et mini-factory chargés in-process, sous-processus du noyau remplacés par monkeypatch ;
seuls git (dépôt jetable) et sandbox-exec + /bin/cat (profil seul, jamais le noyau) tournent réellement.
Chaque test ROUGIT si son correctif est retiré (nom du test = item du lot v8)."""

import contextlib, importlib.util, json, os, pathlib, signal, stat, subprocess, threading, time, types  # noqa: E401

import pytest

from conftest import RUN_PY

_spec = importlib.util.spec_from_file_location("v3_noyau_durci_v8", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)
V3, SRC = RUN_PY.parent, RUN_PY.read_text(encoding="utf-8")
MF_PY = RUN_PY.parents[2] / "mini_factory" / "mf.py"
VRAI_RUN = subprocess.run  # capturé avant tout monkeypatch

def _charger(nom, chemin):
    s = importlib.util.spec_from_file_location(nom, chemin)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m

def _run(tmp_path, **kw):
    """Run nu (sans __init__ : ni git, ni verrou) ; etape/ecrire/ev écrivent sous tmp_path/run."""
    r = noyau.Run.__new__(noyau.Run)
    (run := tmp_path / "run").mkdir(exist_ok=True)
    r.run, r.seq, r.m0, r.w0, r.uuid, r.jalon, r.enfant, r.lock, r.lockfd = run, 0, noyau.mono(), time.time(), "u", "M1", None, None, None
    r.espace = r.espace_moteur = None
    r.R = {"phases": dict.fromkeys(noyau.PHASES), "etape": None, "temoin": None, "quota": None, "base_sha": "b" * 40, "hash_contrat": "h"}
    r.a, r.index = types.SimpleNamespace(keep=False, engine="glm", budget_s=None), tmp_path / "index.jsonl"
    for k, v in kw.items():
        setattr(r, k, v)
    return r

# ---- 1. A:F1/F3 — le contrôle n'est lu qu'au jugement ; dépôt produit sous le contrôle refusé ----
def _cat(ctrl, cible, net, tmp, run=None):
    p = {"ESPACE": tmp, "RUN": run or tmp, "TMP": tmp, "HOME": tmp, "NET": net, "CONTROL": ctrl,
         "USERHOME": os.path.realpath(pathlib.Path.home())}
    return subprocess.run(["/usr/bin/sandbox-exec", "-f", str(V3 / "bornage.sb"), *[f"-D{k}={v}" for k, v in p.items()],
                           "/bin/cat", cible], capture_output=True, timeout=30).returncode

def test_a_f1_bornage_controle_lu_seulement_au_jugement(tmp_path):
    ctrl = pathlib.Path(os.path.realpath(tmp_path)) / "ctrl"
    for f in ("factory_v3/run.py", ".context/secret", "tests/v3/fakes/f.py"):
        (ctrl / f).parent.mkdir(parents=True, exist_ok=True); (ctrl / f).write_text("x")  # noqa: E702
    (vide := tmp_path / "vide").mkdir()
    v = os.path.realpath(vide)
    assert _cat(ctrl, ctrl / "factory_v3/run.py", "1", v) != 0, "phase moteur : le noyau/juge du contrôle est lisible"
    assert _cat(ctrl, ctrl / "factory_v3/run.py", "0", v) == 0, "phase jugement : le contrôle doit rester lisible"
    assert _cat(ctrl, ctrl / ".context/secret", "0", v) != 0, "le refus de .context doit primer même au jugement"
    assert _cat(ctrl, ctrl / "tests/v3/fakes/f.py", "1", v) == 0, "catalogue des faux moteurs illisible : banc cassé"

def test_a_f1_depot_sous_controle_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_V3_MACHINE_ID", "m1")
    for repo, motif in ((noyau.ROOT / "produit", "dépôt de contrôle"), (noyau.ROOT, "dépôt de contrôle"),
                        (noyau.ROOT / ".context" / "p", "id de jalon"), (tmp_path, "id de jalon")):
        with pytest.raises(noyau.Fin) as e:
            _run(tmp_path, repo=repo, jalon="id invalide!").derouler()
        assert e.value.rc == 3 and motif in e.value.motif, (repo, e.value.motif)

# ---- 2. B:1 — aucun nom choisi par le moteur recopié ; mf lit la DERNIÈRE ligne du reçu ----
def test_b1_motif_sans_noms_du_moteur(tmp_path, monkeypatch):
    sp = tmp_path / "espace"
    (sp / "src").mkdir(parents=True); (sp / "src/mod.py").write_text("a\n")  # noqa: E702
    for c in (("init", "-q", "--template="), ("add", "-A"), ("commit", "-q", "-m", "base")):
        noyau.git(sp, *c)
    (sp / "src/mod.py").write_text("b\n"); (sp / "[v3] reçu NOM_MOTEUR z").write_text("")  # noqa: E702
    r = _run(tmp_path, espace=sp, liv=["src/mod.py"], base_commit=noyau.git(sp, "rev-parse", "HEAD").strip())
    with pytest.raises(noyau.Fin) as e:
        r.contrat()
    assert e.value.verdict == "ABORT" and "NOM_MOTEUR" not in e.value.motif, e.value.motif
    assert "[v3] reçu NOM_MOTEUR z" in r.R["contrat"]["violations"]
    monkeypatch.setattr(noyau.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"STDERR_MOTEUR"))
    with pytest.raises(RuntimeError) as g:
        noyau.git(sp, "status")
    assert "STDERR_MOTEUR" not in str(g.value)

def _mf(tmp_path, monkeypatch):
    mf = _charger("mf_v8", MF_PY)
    (k := tmp_path / "k" / "factory_v3").mkdir(parents=True)
    noyau_f = ("run.py", "support.py", "juge_isole.py", "bornage.sb", "ENGINES.lock", "CONSIGNE.md.tmpl")
    for f in (*noyau_f, "moteurs/glm.json"):
        (k / f).parent.mkdir(exist_ok=True)
        (k / f).write_text(f"# {f}\n")
    monkeypatch.setattr(mf, "NOYAU", ["python3", str(k / "run.py")]); monkeypatch.setattr(mf, "ETAT", tmp_path / "etat")  # noqa: E702
    monkeypatch.setattr(mf, "FILE", tmp_path / "etat" / "file.json"); monkeypatch.setattr(mf, "RELIES", {"glm"})  # noqa: E702
    return mf, k

def _recu(d, **R):
    d.mkdir(parents=True, exist_ok=True)
    R = {"schema": "factory-v3-receipt/3", "milestone": "M1", "hash_contrat": "a" * 64,
         "commit": {"sha": "c" * 40}, **R}
    (p := d / "RECEIPT.json").write_text(json.dumps(R))
    return p

@pytest.mark.parametrize("cas", ["attaque", "parent_faux", "hash_autre", "noyau_change", "honnete"])
def test_b1_b9_mf_recu_accepte(tmp_path, monkeypatch, cas):
    """Faux reçu VERT posé par le moteur (même sous un dossier <run_uuid>) et nommé dans un motif AVANT la vraie ligne → jamais accepté."""
    mf, k = _mf(tmp_path, monkeypatch)
    faux = _recu(tmp_path / "RUN" / "engine" / ("u1" if cas == "attaque" else ""), run_uuid="u1", verdict="VERT", rc=0)
    vrai = _recu(tmp_path / "runs" / "u1", run_uuid="u1", verdict="ABORT" if cas == "attaque" else "VERT", rc=0,
                 **({"hash_contrat": "c" * 64} if cas == "hash_autre" else {}))
    out = (f"[v3] M1 ABORT rc=0 — contrat : [v3] reçu {faux} z\n" if cas == "attaque" else "") + f"[v3] reçu {faux if cas == 'parent_faux' else vrai}\n"
    monkeypatch.setattr(mf.subprocess, "run", lambda *a, **kw: (cas == "noyau_change" and (k / "support.py").write_text("# changé\n"),
                                                                 types.SimpleNamespace(returncode=0, stdout=out))[1])
    t = {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2, "apres": [], "demarrages": [],
         "etat": "pret", "hash_contrat": "a" * 64}
    mf.lancer(t, {"T": t}, mf.sha_noyau())
    assert t["etat"] == ("accepte" if cas == "honnete" else "inconnu"), (cas, t["dernier"])

# ---- 3. B:2 — plus de cache .pyc inscriptible pour le juge ; plugins pytest non chargés automatiquement ----
def test_b2_env_tests_sans_cache_pyc(tmp_path, monkeypatch):
    """Requalifie K6 (v7) : le préfixe pyc dans le TMP est RETIRÉ ; PYTHONDONTWRITEBYTECODE=1 revient."""
    vus = []
    monkeypatch.setattr(noyau.Run, "lancer", lambda moi, argv, env, **k: vus.append(env) or (0, "fin", None))
    r = _run(tmp_path, espace=tmp_path / "e", liv=[], timeout=60, test_cmd=["python3", "-m", "pytest", "-q", "t.py"])
    r.jouer_tests("base1")
    assert "PYTHONPYCACHEPREFIX" not in vus[0] and vus[0]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert vus[0]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

# ---- 4. B:3 — passe « après » jouée deux fois ----
V = {"t.py::a": "pass"}
@pytest.mark.parametrize("p1,p2,attendu", [((0, V), (0, {"t.py::a": "fail"}), "ABORT"), ((0, V), (124, V), "ABORT"),
                                          ((124, V), (124, V), "ABORT"), ((0, V), (0, V), None)])
def test_b3_r2_juge_apres_deux_passes_rc_et_cas(tmp_path, monkeypatch, p1, p2, attendu):
    """R2 : rc ET cas des deux passes comparés ; une passe hors délai (124) n'est jamais un verdict."""
    passes = iter([p1, p2])
    monkeypatch.setattr(noyau.Run, "jouer_tests", lambda moi, ph: next(passes))
    monkeypatch.setattr(noyau.Run, "copier_livrables", lambda *a: None)
    r = _run(tmp_path, afp=["t.py::a"], agv=[], base_cases={"t.py::a": "fail"})
    if attendu:
        with pytest.raises(noyau.Fin) as e:
            r.tests_apres()
        assert e.value.verdict == attendu and "non déterministe ou interrompu" in e.value.motif
    else:
        r.tests_apres()
        assert r.R["tests_apres"]["verts"] == ["t.py::a"]
    assert next(passes, "épuisé") == "épuisé", "passe « après » jouée une seule fois"

# ---- 5/6/9/16/17. retraits (machinerie, déclarations, réglages) ----
def test_b4_machinerie_ecart_gele_retiree():
    """Requalifie S16/S17 : ecart_gele doublait contrat() et lisait read_bytes() sur une FIFO (blocage)."""
    assert not any(hasattr(noyau.Run, n) for n in ("ecart_geles", "geles_presents", "fichiers")) and "ecart_gele" not in SRC

def test_b5_panne_moteur_declaree_retiree(tmp_path, monkeypatch):
    """Un événement « error » écrit par le moteur ne choisit plus la classe du verdict (ABORT au lieu de ROUGE)."""
    def faux(moi, argv, env, **k):
        k["log"].write_text('{"type":"error","message":"panne"}\n')
        return 1, "fin", 0
    monkeypatch.setattr(noyau.Run, "lancer", faux)
    E = {"id": "fake:x", "argv": ["/bin/true"], "arret": {}, "format_sortie": "aucun", "defauts": {}, "cli_version": "fake",
         "config_sha256": "c", "plafonds": {"mecanisme": "aucun", "drapeaux": []}}
    r = _run(tmp_path, moteur=E, espace=tmp_path, budget=60)
    r.lancer_moteur("consigne")
    assert r.R["engine"]["rc"] == 1 and "bareme" not in r.R["engine"]

def test_b8_essai_retire_et_c_b1_horloge_sans_decalage(monkeypatch):
    with pytest.raises(SystemExit):
        noyau.main(["--project", "p", "--milestone", "M1", "--essai", "7"])
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1"); monkeypatch.setenv("FACTORY_V3_NOW_MONOTONIC_OFFSET_S", "90000")  # noqa: E702
    assert abs(noyau.mono() - time.monotonic()) < 60

def test_c_a6_b9_b13_b14_declarations_retirees():
    assert not any(m in SRC for m in ("MINUTES_SANS_RUN", "ECHECS_REPETES", '"fs":', '"reseau":', '"network":', '"pousse"', '"bareme"'))
    assert not hasattr(noyau.support, "utc_epoch") and not hasattr(noyau.support, "min_free_gb")
    for p in (V3 / "moteurs").glob("*.json"):
        d = json.loads(p.read_text())
        assert not {"cli_version_min", "usage_source", "sandbox_une_couche", "canari"} & set(d), p.name
        assert "mode" not in d["auth"] and "note" not in d["plafonds"] and d["arret"]["signal_2"] == "SIGKILL", p.name

# ---- 7. B:6 — nodeid brut, fusion du pire au meilleur, correspondance sans perte ----
def test_b6_juge_isole_nodeid_brut_pire_gagne():
    ji, rep = _charger("juge_isole_v8", V3 / "juge_isole.py"), lambda n, w, o, x=False: types.SimpleNamespace(nodeid=n, when=w, outcome=o, wasxfail=x)
    p = ji.Plugin()
    for r in (rep("t/a.py::t[x::y]", "call", "passed"), rep("t/a.py::t[x::y]", "teardown", "failed"),
              rep("t/a.py::C::s", "call", "skipped"), rep("t/a.py::C::s", "call", "passed")):
        p.pytest_runtest_logreport(r)
    assert p.cas == {"t/a.py::t[x::y]": "fail", "t/a.py::C::s": "skip"}, p.cas
    c = noyau.correspond
    assert c("t/a.py::t", "t/a.py::t[x::y]") and c("t/a.py", "t/a.py::C::s") and c("t", "t/a.py::u") and c("t/a.py::t", "t/a.py::t")
    assert not c("t/a.py::t", "t/a.py::t2") and not c("t/a", "t/ab.py::u")

# ---- 8. B:7 — verdict lu gardé, contrat haché écrit ; 15b. C:A8 — timeout plafonné ----
def test_b7_verdict_lu_garde(tmp_path, monkeypatch):
    octets = b'{"rc": 0, "cas": {"t.py::a": "pass"}}'
    monkeypatch.setattr(noyau.Run, "lancer", lambda moi, argv, env, **k: os.write(k["pass_fds"][0], octets) and (0, "fin", None))
    r = _run(tmp_path, espace=tmp_path / "e", liv=[], timeout=60, test_cmd=["python3", "-m", "pytest", "t.py"])
    assert r.jouer_tests("apres") == (0, {"t.py::a": "pass"})
    assert (r.run / "verdict_apres.json").read_bytes() == octets  # R2 : sous RUN, hors TMP/HOME du bac

def test_b7_contrat_ecrit_et_c_a8_timeout_plafonne(tmp_path, monkeypatch):
    (tache := tmp_path / "T.md").write_text("faire\n")
    m = {"id": "M1", "produit": "p", "spec_ref": "s", "but": "b", "task_file": str(tache), "livrables": ["src/mod.py"],
         "juge": {"a_faire_passer": ["tests/t.py::a"]}, "test_cmd": ["python3", "-m", "pytest", "--junitxml", "{junit}", "tests"],
         "timeout_s": 99999, "juge_isole": True}
    m["valide"] = {"hash_contrat": noyau.sha256(noyau.contrat_octets(m, b"faire\n"))}
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    r = _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path)
    r.lire_jalon()
    assert noyau.sha256((r.run / "CONTRAT.json").read_bytes()) == r.R["hash_contrat"] == m["valide"]["hash_contrat"]
    assert r.timeout == noyau.BUDGET_MAX

# ---- 10. B:9 + C:A13 — mini-factory : empreinte de tout factory_v3, MF_NOYAU obligatoire, contrat épinglé, id inconnu ----
def test_b9_mf_empreinte_noyau_complet(tmp_path, monkeypatch):
    mf, k = _mf(tmp_path, monkeypatch)
    h = mf.sha_noyau()
    (k / "support.py").write_text("# modifié\n")
    assert mf.sha_noyau() != h, "un changement hors run.py doit changer l'empreinte du noyau"

def test_b9_r2_mf_noyau_seulement_pour_ajouter_suivant_et_id_inconnu(tmp_path, monkeypatch, capsys):
    """R2 (inversé) : statut/regler marchent SANS MF_NOYAU ; ajouter/suivant le refusent (rc 3), sans défaut."""
    monkeypatch.delenv("MF_NOYAU", raising=False)
    monkeypatch.setenv("MF_ETAT", str(tmp_path / "e0"))
    nu = _charger("mf_v8_nu", MF_PY)
    assert nu.main(["statut"]) == 0 and nu.main(["regler", "X", "pret", "--raison", "x"]) == 3
    assert "travail inconnu" in capsys.readouterr().out
    for cmd in (["suivant"], ["ajouter", "A", "--projet", "p", "--jalon", "M1"]):
        assert nu.main(cmd) == 3 and "MF_NOYAU obligatoire" in capsys.readouterr().out, cmd
    mf, _ = _mf(tmp_path, monkeypatch)
    assert mf.main(["regler", "INCONNU", "pret", "--raison", "x"]) == 3

def test_b9_mf_contrat_epingle(tmp_path, monkeypatch):
    (mf, _), lances = _mf(tmp_path, monkeypatch), []
    monkeypatch.delenv("MF_MOTEUR_FORCE", raising=False)
    monkeypatch.setattr(mf, "hash_contrat", lambda t: "b" * 64)
    monkeypatch.setattr(mf, "lancer", lambda t, F, h: lances.append(t) or 0)
    t = {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2, "apres": [], "demarrages": [],
         "etat": "pret", "hash_contrat": "a" * 64}
    assert mf.suivant({"T": t}) == 2 and not lances, "contrat changé depuis l'ajout : aucun démarrage"
    t["hash_contrat"] = "b" * 64
    assert mf.suivant({"T": t}) == 0 and lances

# ---- 11. C:A1 — lecteur du tube démon, bout d'écriture fermé même si lancer lève ----
def test_c_a1_lecteur_demon_et_tube_ferme(tmp_path, monkeypatch):
    fils, tubes, vrai_pipe, VraiThread = [], [], os.pipe, threading.Thread
    monkeypatch.setattr(noyau.threading, "Thread", lambda *a, **k: fils.append(VraiThread(*a, **k)) or fils[-1])
    monkeypatch.setattr(noyau.os, "pipe", lambda: tubes.append(vrai_pipe()) or tubes[-1])
    monkeypatch.setattr(noyau.Run, "lancer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Popen impossible (simulé)")))
    r = _run(tmp_path, espace=tmp_path / "e", liv=[], timeout=60, test_cmd=["python3", "-m", "pytest", "t.py"])
    try:
        with pytest.raises(RuntimeError):
            r.jouer_tests("apres")
        assert fils and fils[0].daemon, "fil lecteur non démon : sys.exit l'attendrait sans fin"
        with pytest.raises(OSError):
            os.fstat(tubes[0][1])
    finally:                                   # correctif retiré : fermer ici pour que le banc rougisse sans se figer
        with contextlib.suppress(OSError):
            os.close(tubes[0][1])

# ---- 12/13. C:A3/A4/A5 — terminer : signaux ignorés, verrou libéré au reçu, purge des états ----
def _terminer(tmp_path, monkeypatch, keep=False, reconcilier=lambda *a: None):
    vus = []
    monkeypatch.setattr(noyau.signal, "signal", lambda s, h: vus.append((s, h)))
    monkeypatch.setattr(noyau, "reconcilier", reconcilier)
    (lock := tmp_path / "M1.lock").write_text("{}")
    fd = os.open(lock, os.O_RDONLY)  # v13 : flock réellement détenu, pour éprouver son rendu dans terminer()
    r = _run(tmp_path, lock=lock, lockfd=fd)
    r.a.keep = keep
    for f in ("engine/home/x", "engine/tmp/y", "t_base1/sortie.log", "t_apres/home/junit.xml", "verdict_apres.json"):
        (r.run / f).parent.mkdir(parents=True, exist_ok=True); (r.run / f).write_text("z")  # noqa: E702
    return r, lock, vus

def test_c_a3_a4_signaux_ignores_verrou_libere_au_recu(tmp_path, monkeypatch):
    r, lock, vus = _terminer(tmp_path, monkeypatch, reconcilier=lambda *a: (_ for _ in ()).throw(RuntimeError("index mort")))
    with pytest.raises(RuntimeError):
        r.terminer(noyau.Fin("ROUGE", 1, "m"))
    assert (signal.SIGTERM, signal.SIG_IGN) in vus and (signal.SIGHUP, signal.SIG_IGN) in vus
    with pytest.raises(OSError):
        os.fstat(r.lockfd)  # v13 : flock rendu avec le reçu durable ; le fichier reste, il ne décide plus rien

@pytest.mark.parametrize("keep,echec", [(False, None), (True, None), (False, "magasin remplacé")])
def test_c_a5_r2_purge_etats_sauf_verdicts_et_home_du_magasin(tmp_path, monkeypatch, keep, echec):
    """Purge des états moteur/tests ; verdicts (sous RUN) gardés ; après « magasin remplacé », engine/home gardé."""
    r, _, _ = _terminer(tmp_path, monkeypatch, keep=keep)
    assert r.terminer(noyau.Fin("ABORT" if echec else "ROUGE", 2 if echec else 1, "m", echec)) == (2 if echec else 1)
    restes = {p.relative_to(r.run).as_posix() for p in r.run.rglob("*") if p.is_file()} - {"RECEIPT.json", "EVENTS.jsonl"}
    tout = {"engine/home/x", "engine/tmp/y", "t_base1/sortie.log", "t_apres/home/junit.xml", "verdict_apres.json"}
    attendu = tout if keep else {"verdict_apres.json"} | ({"engine/home/x"} if echec else set())
    assert restes == attendu, restes

# ---- 15. C:A7 + C:B12 — admission ----
@pytest.mark.parametrize("moteur", [None, {"id": "glm"}])
def test_c_a7_test_only_sans_quota_et_b12_seuil_disque_constant(tmp_path, monkeypatch, moteur):
    monkeypatch.setattr(noyau, "reconcilier", lambda *a: None)
    monkeypatch.setattr(noyau, "fichiers_base", lambda *a: [])
    monkeypatch.setattr(noyau, "lire_quota", lambda pool: (_ for _ in ()).throw(AssertionError("quota lu")))
    for k, v in (("FACTORY_NO_REAL_ENGINE", "1"), ("FACTORY_V3_FAKE_DISK_FREE_GB", "5"), ("FACTORY_MIN_FREE_GB", "0")):
        monkeypatch.setenv(k, v)
    r = _run(tmp_path, moteur=moteur, repo=tmp_path)
    with pytest.raises((noyau.Fin, AssertionError)) as e:
        r.admission()
    if moteur:
        assert e.type is AssertionError, "un vrai run doit lire le quota"
    else:
        assert e.type is noyau.Fin and "disque" in e.value.motif, "test-only : aucun quota lu, seuil disque 10 Gio non réglable"

# ==== ROUND 2 (revue Codex n°2, 28/09) ====
def test_r2_verdict_sous_run_fifo_et_liens_sans_effet(tmp_path, monkeypatch):
    """FIFO et liens posés par le code jugé à l'ancien emplacement (TMP du bac) : sans effet ; lien au nouveau : remplacé."""
    octets = b'{"rc": 0, "cas": {"t.py::a": "pass"}}'
    ecrit = lambda moi, argv, env, **k: os.write(k["pass_fds"][0], octets) and (0, "fin", None)  # noqa: E731
    monkeypatch.setattr(noyau.Run, "lancer", ecrit)
    r = _run(tmp_path, espace=tmp_path / "e", liv=[], timeout=60, test_cmd=["python3", "-m", "pytest", "t.py"])
    (cible := tmp_path / "cible").write_text("intact")
    for ph in ("t_apres", "t_base1"):
        (r.run / ph).mkdir()
    os.mkfifo(fifo := r.run / "t_apres" / "verdict.json")
    os.symlink(cible, lien := r.run / "t_base1" / "verdict.json")
    os.symlink(cible, r.run / "verdict_apres.json")
    fin = []
    th = threading.Thread(target=lambda: fin.append([r.jouer_tests(ph) for ph in ("base1", "apres")]), daemon=True)
    th.start()
    th.join(10)
    if not fin:                                # correctif retiré : débloquer l'écrivain pour rougir sans se figer
        os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
    assert fin, "le noyau bloque sur une FIFO posée par le code jugé"
    assert cible.read_text() == "intact", "écriture du noyau détournée par un lien"
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode) and os.readlink(lien) == str(cible), "ancien emplacement touché"
    v = r.run / "verdict_apres.json"
    assert not v.is_symlink() and v.read_bytes() == octets and (r.run / "verdict_base1.json").read_bytes() == octets

def test_r2_bornage_run_engine_illisible_au_jugement(tmp_path):
    ctrl = pathlib.Path(os.path.realpath(tmp_path)) / "ctrl"
    (run := ctrl.parent / "run" / "engine").mkdir(parents=True)
    (run / "jeton_copie").write_text("secret")
    (run.parent / "CONSIGNE.md").write_text("c")
    (vide := tmp_path / "vide").mkdir()
    v, rr = os.path.realpath(vide), str(run.parent)
    assert _cat(ctrl, run / "jeton_copie", "0", v, rr) != 0, "jugement : RUN/engine lisible (copie du jeton exposée)"
    assert _cat(ctrl, run.parent / "CONSIGNE.md", "0", v, rr) != 0, "jugement : la consigne n'a pas à être lue"
    assert _cat(ctrl, run / "jeton_copie", "1", v, rr) == 0, "phase moteur : RUN/engine doit rester lisible"
    assert _cat(ctrl, run.parent / "CONSIGNE.md", "1", v, rr) == 0, "phase moteur : consigne illisible"

# ---- v9 : sonde vidéo n°9 — exec limité aux emplacements nommés ; rougit si l'exec sans filtre revient ----
def test_v9_bornage_exec_hors_emplacements_refuse(tmp_path):
    racine = pathlib.Path(os.path.realpath(tmp_path))
    bac, hors = racine / "bac", racine / "hors"
    for d in (bac, hors):
        d.mkdir()
        (d / "vrai").write_bytes(pathlib.Path("/usr/bin/true").read_bytes())
        (d / "vrai").chmod(0o755)
    for net in ("0", "1"):
        p = {"ESPACE": bac, "RUN": bac, "TMP": bac, "HOME": bac, "NET": net, "CONTROL": racine / "ctrl",
             "USERHOME": os.path.realpath(pathlib.Path.home())}
        sb = ["/usr/bin/sandbox-exec", "-f", str(V3 / "bornage.sb"), *[f"-D{k}={v}" for k, v in p.items()]]
        ex = lambda b: subprocess.run([*sb, str(b)], capture_output=True, timeout=30).returncode  # noqa: E731
        assert ex(hors / "vrai") != 0, f"NET={net} : un binaire copié hors des emplacements permis s'exécute"
        assert ex("/usr/bin/true") == 0, f"NET={net} : témoin — exec d'un binaire système refusé (profil cassé)"
        assert ex(bac / "vrai") == 0, f"NET={net} : témoin — exec dans le bac refusé"

def _mf_recu(tmp_path, monkeypatch, **R):
    mf, _ = _mf(tmp_path, monkeypatch)
    rc = R.pop("rc_noyau", 0)
    vrai = _recu(tmp_path / "runs" / "u1", run_uuid="u1", verdict="VERT", rc=rc, **R)
    sortie = types.SimpleNamespace(returncode=rc, stdout=f"[v3] reçu {vrai}\n")
    monkeypatch.setattr(mf.subprocess, "run", lambda *a, **kw: sortie)
    t = {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2, "apres": [], "demarrages": [],
         "etat": "pret", "hash_contrat": "a" * 64}
    return mf, t, vrai

@pytest.mark.parametrize("cas", ["test_only", "vert_rc1", "sans_commit", "json_casse"])
def test_r2_mf_recu_refuse(tmp_path, monkeypatch, cas):
    R = {"test_only": {"noyau": {"flags": ["test_only"]}}, "vert_rc1": {"rc_noyau": 1},
         "sans_commit": {"commit": None}, "json_casse": {}}[cas]
    mf, t, vrai = _mf_recu(tmp_path, monkeypatch, **R)
    if cas == "json_casse":
        vrai.write_text("{pas du json")
    assert mf.lancer(t, {"T": t}, mf.sha_noyau()) == 2 and t["etat"] == "inconnu", (cas, t["etat"])

def test_r2_mf_hash_contrat_delai_depasse_proprement(tmp_path, monkeypatch):
    mf, _ = _mf(tmp_path, monkeypatch)
    delai = subprocess.TimeoutExpired("n", 120)
    monkeypatch.setattr(mf.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(delai))
    assert mf.hash_contrat({"projet": "p", "jalon": "M1"}) is None

def test_r2_mf_empreinte_fichiers_executes_seulement(tmp_path, monkeypatch):
    mf, k = _mf(tmp_path, monkeypatch)
    h = mf.sha_noyau()
    for f in ("README.md", "INTERFACE.md", "moteurs/glm.json.avant_x", "receipts/r.json"):
        (k / f).parent.mkdir(exist_ok=True)
        (k / f).write_text("doc")
    assert mf.sha_noyau() == h, "une documentation, une sauvegarde ou un reçu change l'empreinte du noyau"
    for f in ("bornage.sb", "moteurs/glm.json", "CONSIGNE.md.tmpl", "ENGINES.lock"):
        (k / f).write_text((k / f).read_text() + "modifié")
        assert mf.sha_noyau() != h, f
        h = mf.sha_noyau()

def test_r2_mf_empreinte_calculee_une_fois_par_appel(tmp_path, monkeypatch):
    mf, _ = _mf(tmp_path, monkeypatch)
    monkeypatch.delenv("MF_MOTEUR_FORCE", raising=False)
    appels = []
    monkeypatch.setattr(mf, "sha_noyau", lambda: appels.append(1) or "h")
    monkeypatch.setattr(mf, "hash_contrat", lambda t: "a" * 64)
    monkeypatch.setattr(mf, "lancer", lambda t, F, h: 0)
    t = {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2, "demarrages": [],
         "etat": "pret", "hash_contrat": "a" * 64}
    assert mf.suivant({"T": t}) == 0 and len(appels) == 1, appels

# ==== ROUND 3 (revue Codex n°3, 28/09) ====
@pytest.mark.parametrize("cas", ["noyau_liste", "commit_chaine", "engine_liste", "verdict_liste", "cles_absentes",
                                 "flags_entier"])
def test_r3_mf_recu_mal_type_inconnu_immediat(tmp_path, monkeypatch, cas):
    """JSON valide mais mal typé : « inconnu » tout de suite, jamais une AttributeError qui laisse « en_cours »."""
    R = {"noyau_liste": {"noyau": ["x"]}, "commit_chaine": {"commit": "abc"}, "engine_liste": {"engine": [1]},
         "verdict_liste": {}, "cles_absentes": {}, "flags_entier": {"noyau": {"flags": 5}}}[cas]
    mf, t, vrai = _mf_recu(tmp_path, monkeypatch, **R)
    if cas == "cles_absentes":
        vrai.write_text(json.dumps({"schema": "factory-v3-receipt/3"}))
    if cas == "verdict_liste":
        vrai.write_text(json.dumps(json.loads(vrai.read_text()) | {"verdict": ["VERT"]}))
    assert mf.lancer(t, {"T": t}, mf.sha_noyau()) == 2 and t["etat"] == "inconnu", (cas, t["etat"])

def test_r3_mf_appel_noyau_borne(tmp_path, monkeypatch, capsys):
    mf, t, _ = _mf_recu(tmp_path, monkeypatch)
    vus = []
    def lent(argv, **k):
        vus.append(k.get("timeout"))
        raise subprocess.TimeoutExpired(argv, k.get("timeout"))
    monkeypatch.setattr(mf.subprocess, "run", lent)
    assert mf.lancer(t, {"T": t}, mf.sha_noyau()) == 2 and t["etat"] == "inconnu"
    assert vus == [mf.DELAI_S] and "hors délai" in capsys.readouterr().out
    assert mf.DELAI_S == 6 * noyau.BUDGET_MAX == 32400, "v9 : borne décidée = moteur + 4 passes + 1 marge"

def test_r3_mf_preuve_porte_et_exige_l_empreinte_de_mf(tmp_path, monkeypatch):
    mf, t, _ = _mf_recu(tmp_path, monkeypatch)
    assert mf.lancer(t, {"T": t}, mf.sha_noyau()) == 0 and t["preuve"]["mf"] == mf.MF_SHA
    assert t["demarrages"][-1]["mf"] == mf.MF_SHA   # v9 : --apres retiré, la preuve reste un relevé

# ==== v9 lot B (élagage) : refus qui remplace le manifeste retiré ; index reconstruit depuis les reçus ====
def test_v9_juge_donnees_refuse_jamais_ignore(tmp_path, monkeypatch):
    """Manifeste de données retiré : un contrat qui le déclare est REFUSÉ, jamais jugé sans ses données."""
    (tache := tmp_path / "T.md").write_text("faire\n")
    m = {"id": "M1", "produit": "p", "spec_ref": "s", "but": "b", "task_file": str(tache), "livrables": ["src/mod.py"],
         "juge": {"a_faire_passer": ["tests/t.py::a"], "donnees": {"manifeste": "m.json", "sha256": "0" * 64}},
         "test_cmd": ["python3", "-m", "pytest", "--junitxml", "{junit}", "tests"], "juge_isole": True}
    m["valide"] = {"hash_contrat": noyau.sha256(noyau.contrat_octets(m, b"faire\n"))}
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
    assert e.value.rc == 3 and "juge.donnees" in e.value.motif, e.value.motif
    del m["juge"]["donnees"]
    m["valide"] = {"hash_contrat": noyau.sha256(noyau.contrat_octets(m, b"faire\n"))}
    _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()  # témoin : le même contrat sans donnees est admis

def test_v9_index_reconstruit_depuis_les_recus(tmp_path):
    """Le reçu fait foi : reçu final absent de l'index → ajouté ; reçu changé → sa ligne remplacée ;
    RUNNING ou autre schéma → jamais ingéré ; ligne orpheline (reçu disparu) → conservée."""
    runs, index = tmp_path / "runs", tmp_path / "index.jsonl"

    def recu(u, **kw):
        (runs / u).mkdir(parents=True, exist_ok=True)
        R = {"schema": noyau.SCHEMA, "run_uuid": u, "verdict": "ROUGE", "milestone": "M1"} | kw
        (runs / u / "RECEIPT.json").write_text(json.dumps(R))
    recu("a")
    recu("b", verdict="RUNNING")
    recu("c", schema="autre/1")
    index.write_text(json.dumps({"run_uuid": "orphelin", "verdict": "VERT"}) + "\n")
    noyau.reconcilier(index, [runs])
    lu = {x["run_uuid"]: x for x in noyau.lire_jsonl(index)}
    assert set(lu) == {"orphelin", "a"} and (lu["a"]["jalon"], lu["a"]["verdict"]) == ("M1", "ROUGE"), lu
    recu("a", verdict="VERT")
    noyau.reconcilier(index, [runs])
    lu = [x for x in noyau.lire_jsonl(index) if x["run_uuid"] == "a"]
    assert [x["verdict"] for x in lu] == ["VERT"], lu

# ==== relecture Codex n°7 (Q1, 28-29/09) : 3 corrections ====
def test_v9_mf_apres_non_vide_refuse_jamais_ignore(tmp_path, monkeypatch):
    """Q1(1) : --apres retiré du CLI, mais une file EXISTANTE avec 'apres' non vide doit être REFUSÉE,
    jamais lancée en silence comme si la dépendance n'avait jamais existé."""
    mf, _ = _mf(tmp_path, monkeypatch)

    def jamais(*a, **kw):
        raise AssertionError("le noyau n'aurait jamais dû être lancé : dépendance 'apres' ignorée")
    monkeypatch.setattr(mf.subprocess, "run", jamais)
    t = {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2, "apres": ["A"],
         "demarrages": [], "etat": "pret", "hash_contrat": "a" * 64}
    assert mf.suivant({"T": t}) == 2 and t["etat"] == "pret", t

def test_v9_conftest_mod_rouge_exige_rc0(produit_repo):
    """Q1(2) : la fixture adaptée (juge isolé) exigeait seulement stdout == '3' ; un livrable qui imprime
    3 PUIS échoue (rc≠0) doit faire rougir test_add, pas passer en vert gratuit."""
    (produit_repo / "src" / "mod.py").write_text("import sys\n\ndef add(a, b):\n    print(a + b)\n    sys.exit(7)\n")
    p = VRAI_RUN([noyau.PY, "-m", "pytest", "-q", "tests/test_mod.py"],
                 cwd=produit_repo, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "1 failed" in p.stdout, (p.returncode, p.stdout)

@pytest.mark.parametrize("valeur", [{}, None, []], ids=["dict_vide", "null", "liste_vide"])
def test_v9_juge_donnees_toute_presence_refusee(tmp_path, monkeypatch, valeur):
    """Q1(3) : le refus doit mordre sur la PRÉSENCE de la clé 'donnees', pas seulement une valeur non vide —
    {}, null et [] passaient l'ancien `not j.get(...)`."""
    (tache := tmp_path / "T.md").write_text("faire\n")
    m = {"id": "M1", "produit": "p", "spec_ref": "s", "but": "b", "task_file": str(tache), "livrables": ["src/mod.py"],
         "juge": {"a_faire_passer": ["tests/t.py::a"], "donnees": valeur},
         "test_cmd": ["python3", "-m", "pytest", "--junitxml", "{junit}", "tests"], "juge_isole": True}
    m["valide"] = {"hash_contrat": noyau.sha256(noyau.contrat_octets(m, b"faire\n"))}
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
    assert e.value.rc == 3 and "juge.donnees" in e.value.motif, (valeur, e.value.motif)

def test_v9_juge_isole_refuse_le_livrable_dans_son_processus(tmp_path):
    """F1 (survivant de l'élagage v9 lot B) : le crochet d'audit refuse d'exécuter un livrable DANS le juge, et le code
    retour suit pytest ; témoin : le même test, livrable hors liste, passe (rc 0)."""
    (tmp_path / "liv.py").write_text("X = 1\n")
    (tmp_path / "test_t.py").write_text("import os, runpy\n\n\ndef test_a():\n"
                                        "    assert runpy.run_path(os.path.join(os.path.dirname(__file__), 'liv.py'))['X'] == 1\n")

    def juger(livs):
        r, w = os.pipe()
        argv = [noyau.PY, str(V3 / "juge_isole.py"), str(w), livs, "--", "-q", "-p", "no:cacheprovider", str(tmp_path)]
        p = VRAI_RUN(argv, pass_fds=(w,), capture_output=True, cwd=tmp_path, timeout=120)
        os.close(w)
        v = json.loads(os.read(r, 1 << 16) or b"{}")
        os.close(r)
        return p.returncode, list((v.get("cas") or {}).values())
    assert juger(str(tmp_path / "liv.py")) == (1, ["fail"])
    assert juger(str(tmp_path / "autre.py")) == (0, ["pass"])

# ==== v10 (besoin propriétaire 29/09, mesuré : OUT_1/OUT_1B édités à la main) : sortir un "pret" proprement ====
def test_v10_regler_pret_vers_bloque(tmp_path, monkeypatch):
    """A : voie la plus courte pour retirer un jalon prêt (ex. juge refusé) sans éditer file.json à la main ;
    raison obligatoire et conservée ; les démarrages déjà comptés ne sont jamais remis à zéro."""
    mf, _ = _mf(tmp_path, monkeypatch)
    F = {"T": {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2,
                "etat": "pret", "demarrages": [{"t": "x"}], "reglages": [], "hash_contrat": "a" * 64}}
    mf.ecrire(F)
    assert mf.main(["regler", "T", "bloque", "--raison", "juge refusé"]) == 0
    t = mf.lire()["T"]
    assert t["etat"] == "bloque" and len(t["demarrages"]) == 1
    assert (t["reglages"][-1]["de"], t["reglages"][-1]["vers"], t["reglages"][-1]["raison"]) == ("pret", "bloque", "juge refusé")

def test_v10_regler_en_cours_toujours_refuse(tmp_path, monkeypatch):
    """en_cours reste hors de portée de regler : un démarrage tenu par un appel en cours ne se contourne jamais."""
    mf, _ = _mf(tmp_path, monkeypatch)
    F = {"T": {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2,
                "etat": "en_cours", "demarrages": [{"t": "x"}], "reglages": [], "hash_contrat": "a" * 64}}
    mf.ecrire(F)
    assert mf.main(["regler", "T", "bloque", "--raison", "x"]) == 3
    assert mf.lire()["T"]["etat"] == "en_cours"

def test_v10_regler_pret_vers_pret_refuse(tmp_path, monkeypatch):
    """pret→pret n'a aucun sens (aucune transition prévue dans TRANS) : refusé comme toute transition absente."""
    mf, _ = _mf(tmp_path, monkeypatch)
    F = {"T": {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2,
                "etat": "pret", "demarrages": [], "reglages": [], "hash_contrat": "a" * 64}}
    mf.ecrire(F)
    assert mf.main(["regler", "T", "pret", "--raison", "x"]) == 3
