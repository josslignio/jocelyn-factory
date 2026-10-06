"""Tests manquants v10 — les 52 instructions survivantes de l'élagage 2e passe (M_CAND_V9/ELAGAGE2.md,
mesuré 29/09 01:13) rejouées contre le noyau v10 EN SERVICE (S_CAND_V10/cand_v8). Chaque test cible la
garantie décrite par la relecture Codex n°5 (L_CODEX_ELAGAGE/sortie.txt) pour cette instruction : refus,
verrou, reçu, jugement, publication. L'affichage pur (le print final de terminer()) est ignoré : ce n'est
pas une garantie, rien ne le lit. Aucun moteur réel, aucun /tmp en dur, aucun signal réel envoyé à un vrai
processus ; mêmes conventions que test_durci_v8.py (noyau chargé une seule fois, réutilisé par import).
Chaque test ROUGIT si l'instruction qu'il protège est remplacée par `pass` (mordant vérifié un par un via
_reverif/reverif.py, preuve dans BILAN_TESTS_V10.md)."""

import json
import os
import signal
import subprocess
import time
import types

import pytest

from conftest import make_roadmap
from test_durci_v8 import MF_PY, V3, VRAI_RUN, _charger, _mf, _recu, _run, noyau


# ==== run.py : boot_id, lire_quota, task_path (survivants 0-2) ============================================
def test_boot_id_sysctl_simule_et_vide_donne_none(monkeypatch):
    monkeypatch.setattr(noyau.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="ABCD-1\n"))
    assert noyau.boot_id() == "ABCD-1"
    monkeypatch.setattr(noyau.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=""))
    assert noyau.boot_id() is None


def test_lire_quota_ignore_valeurs_non_numeriques(monkeypatch):
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")
    fake = json.dumps({"glm": {"five_hour": 10, "bogus": "texte", "weekly": None}})
    monkeypatch.setenv("FACTORY_V3_FAKE_QUOTA", fake)
    q = noyau.lire_quota("glm")
    assert q["fenetres"] == {"five_hour": 10.0} and q["fenetre"] == "five_hour", q


def test_task_path_absent_ou_non_texte_retourne_none(tmp_path):
    proj = tmp_path / "projet"; proj.mkdir()
    assert noyau.task_path(proj, None, {}) is None
    assert noyau.task_path(proj, None, {"task_file": None}) is None
    assert noyau.task_path(proj, None, {"task_file": 7}) is None


# ==== run.py : contrat_octets (survivants 3-4) =============================================================
def test_contrat_octets_stable_au_reordonnancement_livrables_geles():
    m1 = {"id": "M1", "livrables": ["b.py", "a.py"], "geles": ["z", "y"]}
    m2 = {"id": "M1", "livrables": ["a.py", "b.py"], "geles": ["y", "z"]}
    assert noyau.contrat_octets(m1, b"t") == noyau.contrat_octets(m2, b"t")
    m3 = {"id": "M1", "livrables": ["a.py", "c.py"], "geles": ["y", "z"]}
    assert noyau.contrat_octets(m1, b"t") != noyau.contrat_octets(m3, b"t")


def test_contrat_octets_juge_isole_change_empreinte():
    base = {"id": "M1", "livrables": [], "geles": []}
    sans = noyau.contrat_octets(base, b"t")
    avec = noyau.contrat_octets({**base, "juge_isole": True}, b"t")
    assert sans != avec


# ==== run.py : main() — dépôt refusé, signaux réarmés avant derouler (survivants 5-6) ======================
def test_main_refuse_repo_absent_fichier_ou_non_git(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_V3_MACHINE_ID", "m1")
    (proj := tmp_path / "projet").mkdir()
    (proj / "ROADMAP.yaml").write_text("milestones: []\n", encoding="utf-8")
    fichier = tmp_path / "f.txt"; fichier.write_text("x", encoding="utf-8")
    non_git = tmp_path / "vide"; non_git.mkdir()
    for repo in (tmp_path / "absent", fichier, non_git):
        rc = noyau.main(["--project", str(proj), "--milestone", "M1", "--repo", str(repo)])
        assert rc == 3, repo
        assert "dépôt produit absent ou non git" in capsys.readouterr().err


def test_main_reinstalle_defaut_avant_derouler(produit_repo, tmp_path, monkeypatch):
    """SIGTERM/SIGHUP remis sur default_int_handler AVANT derouler() : un arrêt demandé mi-course devient
    une KeyboardInterrupt captée par derouler (ABORT + reçu), jamais un signal ignoré ou fatal au noyau."""
    vus = []
    monkeypatch.setattr(noyau.signal, "signal", lambda s, h: vus.append((s, h)))
    monkeypatch.setattr(noyau.Run, "derouler",
                         lambda self: vus.append("derouler") or (_ for _ in ()).throw(noyau.Fin("ABORT", 2, "x")))
    monkeypatch.setenv("FACTORY_V3_MACHINE_ID", "testmachine")
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    noyau.main(["--project", str(contexte["projet"]), "--milestone", contexte["milestone"]])
    avant = vus[:vus.index("derouler")]
    assert (signal.SIGTERM, signal.default_int_handler) in avant
    assert (signal.SIGHUP, signal.default_int_handler) in avant


# ==== run.py : Fin.__str__ (survivant 7) ====================================================================
def test_fin_str_restitue_motif_seul():
    assert str(noyau.Fin("ROUGE", 1, "tests rouges : 3")) == "tests rouges : 3"


# ==== run.py : ev() — numéros croissants, contenu écrit (survivants 8-9) ===================================
def test_ev_numeros_croissants_et_contenu_ecrit(tmp_path):
    r = _run(tmp_path)
    r.ev("A_START", "a", {"x": 1})
    r.ev("A_END", "a", {"s": 0.1})
    lignes = noyau.lire_jsonl(r.run / "EVENTS.jsonl")
    assert [x["seq"] for x in lignes] == [1, 2]
    assert lignes[0]["event"] == "A_START" and lignes[0]["step"] == "a" and lignes[0]["data"] == {"x": 1}
    assert lignes[1]["event"] == "A_END" and lignes[1]["data"] == {"s": 0.1}
    assert lignes[0]["schema_version"] == 1 and "t_monotone" in lignes[0] and "t_wall" in lignes[0]


# ==== run.py : etape() — nom avant effet, reçu persisté, START avant / END après (survivants 10-13) ========
def test_etape_nom_persiste_avant_effet_start_avant_end_apres(tmp_path):
    r = _run(tmp_path)
    vu = {}
    with r.etape("espace"):
        vu["etape_pendant"] = r.R["etape"]
        vu["recu_pendant"] = json.loads((r.run / "RECEIPT.json").read_text())["etape"]
        vu["events_pendant"] = [e["event"] for e in noyau.lire_jsonl(r.run / "EVENTS.jsonl")]
    assert vu["etape_pendant"] == vu["recu_pendant"] == "espace"
    assert vu["events_pendant"] == ["ESPACE_START"]
    fin = [e["event"] for e in noyau.lire_jsonl(r.run / "EVENTS.jsonl")]
    assert fin == ["ESPACE_START", "ESPACE_END"]
    assert isinstance(r.R["phases"]["espace"], float) and r.R["phases"]["espace"] >= 0


# ==== run.py : verrou() — exclude ajouté une fois, fd fermé (survivants 14-15) ==============================
def _repo_git(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "base"], check=True)
    return repo


def test_verrou_ajoute_exclude_une_seule_fois(tmp_path):
    repo = _repo_git(tmp_path)
    r1 = _run(tmp_path, repo=repo, jalon="M1", boot="b")
    r1.R["machine_id"] = "m1"
    r1.verrou()
    excl = repo / ".git" / "info" / "exclude"
    assert excl.read_text().count(".factory_v3/") == 1
    r2 = _run(tmp_path, repo=repo, jalon="M2", boot="b")
    r2.R["machine_id"] = "m1"
    r2.verrou()
    assert excl.read_text().count(".factory_v3/") == 1, "ajouté une deuxième fois : ligne dupliquée"


def test_verrou_tient_le_descripteur_ouvert(tmp_path, monkeypatch):
    """v13 : espionner os.close globalement attraperait les tubes de subprocess (faux positifs) ; on capture
    le fd du VERROU et on vérifie qu'il reste OUVERT après verrou() : c'est lui qui détient le flock."""
    repo = _repo_git(tmp_path)
    vrai_open, vus = os.open, []

    def open_espion(path, *a, **k):
        fd = vrai_open(path, *a, **k)
        if str(path).endswith(".lock"):
            vus.append(fd)
        return fd
    monkeypatch.setattr(noyau.os, "open", open_espion)
    r = _run(tmp_path, repo=repo, jalon="M1", boot="b")
    r.R["machine_id"] = "m1"
    r.verrou()
    assert vus, "verrou() n'a pas ouvert de fichier .lock"
    os.fstat(vus[0])  # v13 : OUVERT — le fd détient le flock tant que le run vit (rendu par terminer())


# ==== run.py : lire_jalon() — famille de refus (survivants 16-21) ==========================================
def _jalon_ok(tmp_path, **over):
    (tache := tmp_path / "T.md").write_text("faire\n", encoding="utf-8")
    m = {"id": "M1", "produit": "p", "spec_ref": "s", "but": "b", "task_file": str(tache),
         "livrables": ["src/mod.py"], "juge": {"a_faire_passer": ["tests/t.py::a"]},
         "test_cmd": ["python3", "-m", "pytest", "--junitxml", "{junit}", "tests"], "juge_isole": True}
    m.update(over)
    m["valide"] = {"hash_contrat": noyau.sha256(noyau.contrat_octets(m, tache.read_bytes()))}
    return m


@pytest.mark.parametrize("champ", ["produit", "spec_ref", "but"])
def test_lire_jalon_refuse_champ_absent(tmp_path, monkeypatch, champ):
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    m = _jalon_ok(tmp_path); del m[champ]
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
    assert e.value.rc == 3 and champ in e.value.motif, e.value.motif


def test_lire_jalon_refuse_jalon_introuvable(tmp_path):
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, m=None, proj=tmp_path, repo=tmp_path).lire_jalon()
    assert e.value.rc == 3 and "introuvable" in e.value.motif


def test_lire_jalon_refuse_consigne_absente_ou_vide(tmp_path, monkeypatch):
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    for tf in (None, ""):
        m = _jalon_ok(tmp_path)
        if tf is None:
            (tmp_path / "T.md").unlink()
        else:
            (tmp_path / "T.md").write_text("", encoding="utf-8")
        with pytest.raises(noyau.Fin) as e:
            _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
        assert e.value.rc == 3 and "consigne vide" in e.value.motif


def test_lire_jalon_refuse_test_cmd_mal_type(tmp_path, monkeypatch):
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    for tc in (None, [], ["python3", 7]):
        m = _jalon_ok(tmp_path, test_cmd=tc)
        with pytest.raises(noyau.Fin) as e:
            _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
        assert e.value.rc == 3 and "test_cmd" in e.value.motif


def test_lire_jalon_refuse_test_cmd_sans_python_m_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    m = _jalon_ok(tmp_path, test_cmd=["python3", "-m", "pytest_autre", "--junitxml", "{junit}", "tests"])
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
    assert e.value.rc == 3 and "juge isolé" in e.value.motif


def test_lire_jalon_refuse_reseau_tests_true(tmp_path, monkeypatch):
    monkeypatch.setattr(noyau, "git", lambda *a, **k: types.SimpleNamespace(returncode=1))
    m = _jalon_ok(tmp_path, reseau_tests=True)
    with pytest.raises(noyau.Fin) as e:
        _run(tmp_path, m=m, proj=tmp_path, repo=tmp_path).lire_jalon()
    assert e.value.rc == 3 and "reseau_tests" in e.value.motif


# ==== run.py : resoudre_moteur() — CLI réel refusé sous FACTORY_NO_REAL_ENGINE=1 (survivant 22) ============
def test_resoudre_moteur_refuse_cli_reel_sous_no_real_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")
    monkeypatch.delenv("FACTORY_V3_FAKE_ENGINE", raising=False)
    r = _run(tmp_path, a=types.SimpleNamespace(engine="glm", budget_s=None, keep=False))
    with pytest.raises(noyau.Fin) as e:
        r.resoudre_moteur()
    assert e.value.rc == 3 and "FACTORY_NO_REAL_ENGINE=1" in e.value.motif


# ==== run.py : remettre_a_base() — restaure les suivis, purge les non suivis (survivant 23) ================
def test_remettre_a_base_restaure_suivis_et_purge_non_suivis(tmp_path):
    repo = _repo_git(tmp_path)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=t",
                    "commit", "-qm", "a"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    (repo / "a.txt").write_text("modifie\n", encoding="utf-8")
    (repo / "b_non_suivi.txt").write_text("intrus\n", encoding="utf-8")
    r = _run(tmp_path, espace=repo, base_commit=base)
    r.remettre_a_base()
    assert (repo / "a.txt").read_text() == "v1\n"
    assert not (repo / "b_non_suivi.txt").exists()


# ==== run.py : jouer_tests() — un test livrable pas encore écrit est omis à la base (survivant 24) =========
def test_jouer_tests_base_omet_livrable_test_absent(tmp_path, monkeypatch):
    vus = []
    monkeypatch.setattr(noyau.Run, "lancer", lambda moi, argv, env, **k: vus.append(argv) or (0, "fin", None))
    (esp := tmp_path / "e").mkdir()
    (esp / "tests_ok.py").write_text("x", encoding="utf-8")
    r = _run(tmp_path, espace=esp, liv=["tests_nouveau.py"], timeout=60,
             test_cmd=["python3", "-m", "pytest", "tests_nouveau.py", "tests_ok.py"])
    r.jouer_tests("base1")
    assert "tests_nouveau.py" not in vus[0] and "tests_ok.py" in vus[0]
    (esp / "tests_nouveau.py").write_text("x", encoding="utf-8")
    r.jouer_tests("base2")
    assert "tests_nouveau.py" in vus[1]


# ==== run.py : tests_apres() — rc et cas cohérents entre eux, pas seulement entre les 2 passes (survivant 25) =
def test_tests_apres_incoherent_rc_zero_mais_cas_rouge_abort(tmp_path, monkeypatch):
    cas = {"t.py::a": "fail"}
    monkeypatch.setattr(noyau.Run, "jouer_tests", lambda moi, ph: (0, dict(cas)))
    monkeypatch.setattr(noyau.Run, "copier_livrables", lambda *a: None)
    r = _run(tmp_path, afp=["t.py::a"], agv=[], base_cases={})
    with pytest.raises(noyau.Fin) as e:
        r.tests_apres()
    assert e.value.verdict == "ABORT" and "incohérent" in e.value.motif


# ==== run.py : consigne() — fichier, empreinte, texte retourné concordent (survivants 26-28) ================
def test_consigne_fichier_empreinte_et_retour_concordent(tmp_path):
    (V3 / "CONSIGNE.md.tmpl")  # existe déjà dans le noyau réel ; on l'utilise tel quel
    m = {"produit": "p", "but": "corriger", "spec_ref": "s.md#x"}
    r = _run(tmp_path, m=m, jalon="M1", liv=["a.py"], agv=[])
    r.task = b"la tache"
    txt = r.consigne()
    assert (r.run / "CONSIGNE.md").read_text(encoding="utf-8") == txt
    assert r.R["hash_consigne"] == noyau.sha256(txt.encode("utf-8"))
    assert "la tache" in txt and "a.py" in txt


# ==== run.py : derouler() — machine_id lu/validé, admission tracée avant l'espace (survivants 29-34) =======
def test_derouler_machine_id_fichier_local_si_env_absente(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_V3_MACHINE_ID", raising=False)
    monkeypatch.setattr(noyau.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".factory_v3").mkdir()
    (tmp_path / ".factory_v3" / "machine_id").write_text("machine-fichier\n", encoding="utf-8")
    r = _run(tmp_path, repo=tmp_path / "hors")
    monkeypatch.setattr(noyau.Run, "verrou", lambda s: (_ for _ in ()).throw(noyau.Fin("ABORT", 2, "stop")))
    with pytest.raises(noyau.Fin):
        r.derouler()
    assert r.R["machine_id"] == "machine-fichier"


def test_derouler_refuse_machine_id_invalide(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_V3_MACHINE_ID", "invalide avec espaces")
    r = _run(tmp_path, repo=tmp_path / "hors")
    with pytest.raises(noyau.Fin) as e:
        r.derouler()
    assert e.value.rc == 3 and "machine_id" in e.value.motif


def test_derouler_admission_start_avant_verrou_ecrit_running_avant_espace(tmp_path, monkeypatch):
    ordre = []
    monkeypatch.setenv("FACTORY_V3_MACHINE_ID", "m1")
    monkeypatch.setattr(noyau.Run, "verrou", lambda s: ordre.append("verrou"))
    monkeypatch.setattr(noyau.Run, "sonder_profil",
                         lambda s: ordre.append("sonder") or (_ for _ in ()).throw(noyau.Fin("ABORT", 2, "x")))
    vraie_ev = noyau.Run.ev
    monkeypatch.setattr(noyau.Run, "ev", lambda s, e, *a, **k: (ordre.append(e), vraie_ev(s, e, *a, **k))[1])
    vraie_ecrire = noyau.Run.ecrire
    def ecrire(s):
        ordre.append("ecrire")
        vraie_ecrire(s)
        assert s.R["verdict"] == "RUNNING"
    monkeypatch.setattr(noyau.Run, "ecrire", ecrire)
    r = _run(tmp_path, repo=tmp_path / "hors")
    r.R["verdict"] = "RUNNING"
    with pytest.raises(noyau.Fin):
        r.derouler()
    assert ordre == ["ADMISSION_START", "verrou", "ecrire", "ADMISSION_END", "sonder"], ordre
    assert r.R["machine_id"] == "m1"


# ==== run.py : terminer() — enfant arrêté, événement verdict conforme (survivants 35-36) ====================
def test_terminer_arrete_enfant_reste_sans_le_tuer_deux_fois(tmp_path, monkeypatch):
    arrets = []
    monkeypatch.setattr(noyau.signal, "signal", lambda s, h: None)
    monkeypatch.setattr(noyau, "reconcilier", lambda *a: None)
    monkeypatch.setattr(noyau.Run, "arreter", lambda s, p, a: arrets.append((p, a)))
    faux_p, faux_arret = object(), {"signal_1": "SIGTERM"}
    r = _run(tmp_path, enfant=(faux_p, faux_arret))
    r.terminer(noyau.Fin("ABORT", 2, "coupe"))
    assert arrets == [(faux_p, faux_arret)]


@pytest.mark.parametrize("verdict,evenement", [("REFUS", "REFUS"), ("ABORT", "ABORT"),
                                               ("VERT", "VERDICT"), ("ROUGE", "VERDICT")])
def test_terminer_evenement_suit_le_verdict(tmp_path, monkeypatch, verdict, evenement):
    monkeypatch.setattr(noyau.signal, "signal", lambda s, h: None)
    monkeypatch.setattr(noyau, "reconcilier", lambda *a: None)
    r = _run(tmp_path)
    r.terminer(noyau.Fin(verdict, 0, "m"))
    events = [e for e in noyau.lire_jsonl(r.run / "EVENTS.jsonl")]
    assert events[-1]["event"] == evenement and events[-1]["data"]["verdict"] == verdict


# ==== support.py : now(), _get(), quota() (survivants 38-42) ===============================================
from factory_v3 import support  # noqa: E402  — module autonome, import direct comme test_v3_support.py


def test_support_now_format_utc(monkeypatch):
    monkeypatch.setattr(support.time, "gmtime", lambda: time.struct_time((2026, 9, 29, 12, 0, 0, 1, 272, 0)))
    assert support.now() == "2026-09-29T12:00:00Z"


def test_support_get_url_entetes_et_delai_sans_reseau(monkeypatch):
    vus = {}

    class FauxReponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    def faux_urlopen(request, timeout):
        vus["url"], vus["headers"], vus["timeout"] = request.full_url, dict(request.headers), timeout
        return FauxReponse()
    monkeypatch.setattr(support.urllib.request, "urlopen", faux_urlopen)
    d = support._get("https://x.test/y", {"Authorization": "Bearer z"})
    assert d == {"ok": True} and vus["url"] == "https://x.test/y" and vus["timeout"] == 20
    assert vus["headers"]["Authorization"] == "Bearer z" and vus["headers"]["User-agent"] == "factory-v3/1"


def test_support_quota_claude_fenetres_generale_et_par_modele(monkeypatch):
    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout=json.dumps({"claudeAiOauth": {"accessToken": "tok"}})))
    reponses = {
        "https://api.anthropic.com/api/oauth/usage": {
            "five_hour": {"utilization": 40}, "seven_day": {"utilization": 10},
            "limits": [{"kind": "weekly_scoped", "scope": {"model": {"display_name": "Sonnet"}}, "percent": 25}]},
    }
    monkeypatch.setattr(support, "_get", lambda url, headers: reponses[url])
    w = support.quota("claude")
    assert w == {"five_hour": 0.4, "seven_day": 0.1, "weekly_sonnet": 0.25}


def test_support_quota_fournisseur_inconnu_leve():
    with pytest.raises(ValueError, match="inconnu"):
        support.quota("bogus")


# ==== juge_isole.py : fd non hérité, crochet posé avant pytest (survivants 43-44) ===========================
def test_juge_isole_fd_non_herite_et_crochet_avant_pytest(monkeypatch, tmp_path):
    ji = _charger("juge_isole_manquants_v10", V3 / "juge_isole.py")
    ordre = []
    monkeypatch.setattr(ji.os, "set_inheritable", lambda fd, v: ordre.append(("inheritable", fd, v)))
    monkeypatch.setattr(ji.sys, "addaudithook", lambda h: ordre.append(("audithook",)))
    monkeypatch.setattr(pytest, "main", lambda *a, **k: ordre.append("pytest_main") or 0)
    monkeypatch.setattr(ji.os, "write", lambda fd, data: None)
    monkeypatch.setattr(ji.sys, "argv", ["j", "9", str(tmp_path / "l.py"), "--", "-q"])
    with pytest.raises(SystemExit):
        ji.main()
    assert ordre == [("inheritable", 9, False), ("audithook",), "pytest_main"]


# ==== mini_factory/mf.py : ecrire() atomique, hash_contrat() heureux/mal formé (survivants 47-48) ===========
def test_mf_ecrire_est_atomique_et_relisible(tmp_path, monkeypatch):
    mf, _ = _mf(tmp_path, monkeypatch)
    mf.ecrire({"A": {"id": "A"}})
    assert mf.lire() == {"A": {"id": "A"}}
    assert list(mf.ETAT.glob("*.tmp")) == []


@pytest.mark.parametrize("sortie,rc,attendu", [
    ("a" * 64 + "\n", 0, "a" * 64), ("PAS_UN_HASH\n", 0, None), ("a" * 64 + "\n", 1, None)])
def test_mf_hash_contrat_heureux_et_mal_forme(tmp_path, monkeypatch, sortie, rc, attendu):
    mf, _ = _mf(tmp_path, monkeypatch)
    monkeypatch.setattr(mf.subprocess, "run",
                         lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=sortie))
    assert mf.hash_contrat({"projet": "p", "jalon": "M1"}) == attendu


# ==== mini_factory/mf.py : suivant() — noyau introuvable, en_cours orphelin, file vide (survivants 49-51) ===
def test_mf_suivant_refuse_si_noyau_introuvable(tmp_path, monkeypatch):
    mf = _charger("mf_manquants_v10", MF_PY)
    monkeypatch.setattr(mf, "NOYAU", ["python3", str(tmp_path / "absent" / "run.py")])
    assert mf.suivant({}) == 3


def test_mf_suivant_reconcilie_en_cours_orphelin_en_inconnu(tmp_path, monkeypatch):
    mf, k = _mf(tmp_path, monkeypatch)
    t = {"id": "T", "jalon": "M1", "projet": "p", "moteur": "glm", "essais": 2, "demarrages": [],
         "etat": "en_cours", "hash_contrat": "a" * 64}
    F = {"T": t}
    assert mf.suivant(F) == 2
    assert t["etat"] == "inconnu"
    assert mf.lire()["T"]["etat"] == "inconnu", "reconciliation non persistée sur disque"


def test_mf_suivant_aucun_travail_pret_aide_rc2(tmp_path, monkeypatch, capsys):
    mf, _ = _mf(tmp_path, monkeypatch)
    assert mf.suivant({}) == 2
    assert "aucun travail prêt" in capsys.readouterr().out
