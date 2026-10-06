"""Banc v3 — Codex par le magasin du CLI (~/.codex/auth.json, OAuth ChatGPT, jeton qui tourne), 28/09.
Tests PURS : aucun CLI réel (which/--version remplacés), aucun noyau lancé, sous-processus moteur remplacé par monkeypatch.
Seuls sandbox-exec + /bin/cat|/usr/bin/touch tournent, sur un FAUX home utilisateur (tmp_path), jamais sur le vrai magasin.
Chaque test ROUGIT si sa règle est retirée (profil : literal/NET=1/fin de profil ; noyau : lien, refus, env, MAGASIN)."""

import importlib.util, json, os, pathlib, subprocess, time, types  # noqa: E401

import pytest

from conftest import RUN_PY

_spec = importlib.util.spec_from_file_location("v3_noyau_codex_v8", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)
V3 = RUN_PY.parent
LOCK = json.loads((V3 / "ENGINES.lock").read_text())

def _faux_home(tmp_path):
    """Faux home utilisateur (chemin réel) : .codex/auth.json (le magasin) et un voisin .codex/autre.json."""
    u = pathlib.Path(os.path.realpath(tmp_path)) / "u"
    (u / ".codex").mkdir(parents=True)
    (u / ".codex/auth.json").write_text('{"tokens": "JETON"}')
    (u / ".codex/autre.json").write_text("voisin")
    return u

# ---- (a) profil : le seul fichier MAGASIN, lu+écrit en NET=1 seulement ----
def _sb(u, vide, net, prog, cible, magasin=None):
    p = {"ESPACE": vide, "RUN": vide, "TMP": vide, "HOME": vide, "NET": net, "CONTROL": vide, "USERHOME": u}
    if magasin:
        p["MAGASIN"] = magasin
    return subprocess.run(["/usr/bin/sandbox-exec", "-f", str(V3 / "bornage.sb"), *[f"-D{k}={v}" for k, v in p.items()],
                           prog, str(cible)], capture_output=True, timeout=30).returncode

def test_profil_magasin_literal_phase_moteur_seule(tmp_path):
    u = _faux_home(tmp_path)
    (vide := pathlib.Path(os.path.realpath(tmp_path)) / "vide").mkdir()
    reel, voisin, M = u / ".codex/auth.json", u / ".codex/autre.json", str(u / ".codex/auth.json")
    CAT, TOUCH = "/bin/cat", "/usr/bin/touch"
    # phase moteur + MAGASIN : le fichier exact est lisible et inscriptible (règle en fin de profil, prime sur le refus ~/.codex)
    assert _sb(u, vide, "1", CAT, reel, M) == 0, "phase moteur : le magasin déclaré doit être lisible"
    assert _sb(u, vide, "1", TOUCH, reel, M) == 0, "phase moteur : le magasin déclaré doit être inscriptible (jeton rafraîchi)"
    # literal, pas subpath : le reste de ~/.codex reste fermé
    assert _sb(u, vide, "1", CAT, voisin, M) != 0, "voisin du magasin lisible : la règle ouvre plus que le fichier"
    assert _sb(u, vide, "1", TOUCH, voisin, M) != 0, "voisin du magasin inscriptible"
    assert _sb(u, vide, "1", TOUCH, u / ".codex/neuf.json", M) != 0, "création possible dans ~/.codex"
    # jugement (NET=0) : même avec MAGASIN passé, le fichier reste fermé
    assert _sb(u, vide, "0", CAT, reel, M) != 0, "jugement : le magasin est lisible"
    assert _sb(u, vide, "0", TOUCH, reel, M) != 0, "jugement : le magasin est inscriptible"
    # sans MAGASIN : rien ne change (glm, claude, tests)
    assert _sb(u, vide, "1", CAT, reel) != 0, "sans MAGASIN, ~/.codex/auth.json lisible"
    assert _sb(u, vide, "1", TOUCH, reel) != 0, "sans MAGASIN, ~/.codex/auth.json inscriptible"
    assert json.loads(reel.read_text()) == {"tokens": "JETON"}

def test_profil_magasin_par_le_lien_du_home_moteur(tmp_path):
    """Le chemin réellement ouvert par codex : {home}/.codex/auth.json → lien vers le vrai magasin."""
    u = _faux_home(tmp_path)
    (h := pathlib.Path(os.path.realpath(tmp_path)) / "h" / ".codex").mkdir(parents=True)
    os.symlink(u / ".codex/auth.json", h / "auth.json")
    M = str(u / ".codex/auth.json")
    assert _sb(u, h.parent, "1", "/bin/cat", h / "auth.json", M) == 0, "phase moteur : lien vers le magasin illisible"
    assert _sb(u, h.parent, "0", "/bin/cat", h / "auth.json", M) != 0, "jugement : le lien donne accès au magasin"
    assert _sb(u, h.parent, "1", "/bin/cat", h / "auth.json") != 0, "sans MAGASIN : le lien donne accès au magasin"

# ---- (b) noyau ----
def _run(tmp_path, engine, **kw):
    r = noyau.Run.__new__(noyau.Run)
    (run := tmp_path / "run").mkdir(exist_ok=True)
    r.run, r.seq, r.m0, r.w0, r.uuid, r.jalon, r.enfant, r.lock, r.pings = run, 0, noyau.mono(), time.time(), "u", "M1", None, None, 0
    r.espace, r.espace_moteur, r.moteur, r.budget = tmp_path, None, None, 60
    r.R = {"phases": dict.fromkeys(noyau.PHASES), "etape": None, "temoin": None, "quota": None, "base_sha": "b" * 40,
           "hash_contrat": "h", "noyau": {"flags": []}}
    r.a, r.index = types.SimpleNamespace(keep=False, engine=engine, budget_s=None), tmp_path / "index.jsonl"
    for k, v in kw.items():
        setattr(r, k, v)
    return r

@pytest.fixture
def cli_simule(monkeypatch, tmp_path):
    """Aucun CLI réel : which → faux chemin, --version → version du lock ; HOME → faux home utilisateur."""
    for k in ("FACTORY_NO_REAL_ENGINE", "FACTORY_V3_FAKE_ENGINE", "CODEX_API_KEY", "ZAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    u = _faux_home(tmp_path)
    monkeypatch.setenv("HOME", str(u))
    monkeypatch.setattr(noyau.shutil, "which", lambda c, **k: f"/nulle/part/{c}")
    def version(argv, **k):
        assert argv[1:] == ["--version"], f"sous-processus inattendu : {argv}"
        cli = "claude" if "/claude/versions/" in argv[0] else os.path.basename(argv[0])  # profil claude épinglé par chemin
        return types.SimpleNamespace(returncode=0, stdout=f"x {LOCK[{'opencode': 'glm'}.get(cli, cli)]}\n", stderr="")
    monkeypatch.setattr(noyau.subprocess, "run", version)
    (v := tmp_path / "v3_actif" / "moteurs").mkdir(parents=True)  # R2 DECOR : codex livré inactif, activé ici
    (v.parent / "ENGINES.lock").write_text((V3 / "ENGINES.lock").read_text())
    for m in (V3 / "moteurs").glob("*.json"):
        (v / m.name).write_text(json.dumps(json.loads(m.read_text()) | {"actif": True}))
    monkeypatch.setattr(noyau, "V3", v.parent)
    return u

def test_profil_codex_declaratif():
    d = json.loads((V3 / "moteurs/codex.json").read_text())
    assert d["auth"] == {"env": [], "magasin_cli": ".codex/auth.json"} and "CODEX_API_KEY" not in d["env_allowlist"]
    assert d["env_fixe"]["CODEX_HOME"] == "{home}/.codex", "CODEX_HOME doit rester le home jetable du run"
    for n in ("glm", "claude"):
        assert "magasin_cli" not in json.loads((V3 / f"moteurs/{n}.json").read_text())["auth"], n

def test_admission_codex_sans_cle_avec_magasin(tmp_path, cli_simule):
    E = _run(tmp_path, "codex").resoudre_moteur()                 # CODEX_API_KEY absent : admis
    assert E["id"] == "codex" and E["auth"]["magasin_cli"] == ".codex/auth.json"

@pytest.mark.parametrize("cas", ["absent", "absolu", "remontee"])
def test_admission_codex_refuse_si_magasin_absent_ou_hors_home(tmp_path, cli_simule, monkeypatch, cas):
    """Le fichier ouvert par le profil est le magasin réel, relatif au home : absent, absolu ou « .. » → REFUS avant dépense."""
    if cas == "absent":
        (cli_simule / ".codex/auth.json").unlink()
    else:                                                          # profil moteur copié hors du noyau, magasin_cli détourné
        (v := tmp_path / "v3" / "moteurs").mkdir(parents=True)
        (v.parent / "ENGINES.lock").write_text((V3 / "ENGINES.lock").read_text())
        d = json.loads((V3 / "moteurs/codex.json").read_text())
        d["auth"]["magasin_cli"] = str(cli_simule / ".codex/autre.json") if cas == "absolu" else "../u/.codex/autre.json"
        d["actif"] = True                                          # R2 DECOR : seul le magasin est éprouvé ici
        (v / "codex.json").write_text(json.dumps(d))
        monkeypatch.setattr(noyau, "V3", v.parent)
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, "codex").resoudre_moteur()
    assert e.value.rc == 3 and "magasin" in e.value.motif, e.value.motif

@pytest.mark.parametrize("engine,cle", [("glm", "ZAI_API_KEY"), ("claude", "CLAUDE_CODE_OAUTH_TOKEN")])
def test_admission_glm_claude_inchangee(tmp_path, cli_simule, monkeypatch, engine, cle):
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, engine).resoudre_moteur()
    assert e.value.rc == 3 and cle in e.value.motif, e.value.motif
    monkeypatch.setenv(cle, "k")
    assert _run(tmp_path, engine).resoudre_moteur()["id"] == engine

def _lancer_moteur(tmp_path, monkeypatch, engine, apres=None):
    vus = []
    def faux(moi, argv, env, **k):
        lien = moi.run / "engine/home/.codex/auth.json"
        vus.append((argv, env, os.path.islink(lien) and os.readlink(lien), moi.bornage(argv, k["net"], k["tmp"], k["home"])))
        k["log"].write_text("")
        if apres:
            apres(lien)                                            # ce que le CLI a pu faire du lien pendant le run
        return 0, "fin", 0
    monkeypatch.setattr(noyau.Run, "lancer", faux)
    r = _run(tmp_path, engine)
    r.moteur = r.resoudre_moteur()
    r.lancer_moteur("consigne")
    return r, vus[0]

def test_lancer_codex_lien_vers_le_vrai_magasin_jamais_copie(tmp_path, cli_simule, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "fuite")                   # présente chez l'opérateur : jamais transmise
    r, (argv, env, lien, borne) = _lancer_moteur(tmp_path, monkeypatch, "codex")
    reel = os.path.realpath(cli_simule / ".codex/auth.json")
    assert lien == reel, f"auth.json du home moteur : pas un lien vers le vrai magasin ({lien!r})"
    assert "CODEX_API_KEY" not in env and env["CODEX_HOME"] == str(r.run / "engine/home/.codex")
    assert f"-DMAGASIN={reel}" in borne and "-DNET=1" in borne
    assert json.loads((cli_simule / ".codex/auth.json").read_text()) == {"tokens": "JETON"}, "magasin réel modifié"

def test_magasin_jamais_passe_au_jugement(tmp_path, cli_simule):
    r = _run(tmp_path, "codex")
    r.moteur = r.resoudre_moteur()
    assert not any(x.startswith("-DMAGASIN=") for x in r.bornage(["/usr/bin/true"], "0", tmp_path, tmp_path))
    assert any(x.startswith("-DMAGASIN=") for x in r.bornage(["/usr/bin/true"], "1", tmp_path, tmp_path))

def test_lancer_glm_sans_magasin(tmp_path, cli_simule, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "k")
    r, (argv, env, lien, borne) = _lancer_moteur(tmp_path, monkeypatch, "glm")
    assert lien is False and not (r.run / "engine/home/.codex").exists()
    assert not any(x.startswith("-DMAGASIN=") for x in borne)

# ---- ROUND 2 (revue Codex n°2) : Codex inactif par défaut ; lien du magasin vérifié AVANT le jugement ----
def test_r2_codex_inactif_par_defaut(tmp_path, cli_simule, monkeypatch):
    assert json.loads((V3 / "moteurs/codex.json").read_text())["actif"] is False
    monkeypatch.setattr(noyau, "V3", V3)                           # le vrai profil livré
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, "codex").resoudre_moteur()
    assert e.value.rc == 3 and "inactif" in e.value.motif, e.value.motif

def _remplace(lien):                                               # renommage d'un temporaire par-dessus le lien
    os.unlink(lien)
    lien.write_text('{"tokens": "JETON_NEUF"}')

def _redirige(lien):
    os.unlink(lien)
    os.symlink(lien.parent / "ailleurs.json", lien)

@pytest.mark.parametrize("apres", [_remplace, _redirige], ids=["remplace", "redirige"])
def test_r2_lien_magasin_remplace_abort_avant_jugement(tmp_path, cli_simule, monkeypatch, apres):
    with pytest.raises(noyau.Fin) as e:
        _lancer_moteur(tmp_path, monkeypatch, "codex", apres)
    assert e.value.verdict == "ABORT" and e.value.motif_echec == "magasin remplacé", e.value.motif
    assert json.loads((cli_simule / ".codex/auth.json").read_text()) == {"tokens": "JETON"}
