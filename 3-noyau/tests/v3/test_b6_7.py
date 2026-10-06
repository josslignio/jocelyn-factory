"""TEST_7 — CORR_7 (mf.py) : ABORT/panne_moteur ne consomme pas d'essai.

Banc in-process, style test_b2bis_1.py : mf.py chargé sous un état jetable, le
noyau est un fichier factice jamais exécuté, subprocess.run remplacé par un
écrivain de reçus — aucun moteur réel (FACTORY_NO_REAL_ENGINE=1), aucune
fixture appelée directement, échec sur l'assertion métier. Rouge sur le mf.py
de référence (tests 1, 2, 4), vert après CORR_7 ; test 3 non-régression.
"""

import importlib.util, json, os, types
from pathlib import Path

os.environ.setdefault("FACTORY_NO_REAL_ENGINE", "1")
MF = Path(__file__).resolve().parents[3] / "mini_factory" / "mf.py"  # CORR_8 : racine X_CAND = parents[3]
_N = iter(range(10 ** 9))


def _mf(tmp_path, monkeypatch):
    (tmp_path / "noyau.py").write_text("")
    monkeypatch.setenv("MF_ETAT", str(tmp_path / "etat"))
    monkeypatch.setenv("MF_NOYAU", f"python3 {tmp_path / 'noyau.py'}")
    monkeypatch.setenv("MF_MOTEURS_RELIES", "glm")
    monkeypatch.delenv("MF_MOTEUR_FORCE", raising=False)
    sp = importlib.util.spec_from_file_location(f"mf_{next(_N)}", MF)
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    m.sha_noyau, m.hash_contrat = (lambda: "h"), (lambda t: t.get("hash_contrat"))
    return m


def _moteur(m, tmp_path, monkeypatch, verdict, rc, motif_echec=None):
    """Faux noyau : écrit un reçu recevable (uuid frais) au lieu d'appeler un moteur."""
    n = iter(range(1000))
    def run(argv, **kw):
        u = f"u{next(n)}"; d = tmp_path / u; d.mkdir(exist_ok=True)
        R = {"schema": "factory-v3-receipt/3", "milestone": "M1", "verdict": verdict, "rc": rc, "run_uuid": u,
             "hash_contrat": "hc", "motif_echec": motif_echec, "noyau": {}, "commit": {}, "engine": {}}
        if verdict == "VERT":
            R["commit"] = {"sha": "a" * 40}
        p = d / "recu.json"; p.write_text(json.dumps(R))
        return types.SimpleNamespace(returncode=rc, stdout=f"[v3] reçu {p}\n")
    monkeypatch.setattr(m, "subprocess", types.SimpleNamespace(run=run, TimeoutExpired=m.subprocess.TimeoutExpired))


def _travail(m, essais, demarrages=None):
    m.ecrire({"T1": {"id": "T1", "projet": "p", "jalon": "M1", "moteur": "glm", "essais": essais, "etat": "pret",
                     "demarrages": demarrages if demarrages is not None else [], "reglages": [], "hash_contrat": "hc"}})


def test_panne_repassse_pret_budget_intact(tmp_path, monkeypatch):
    """1 panne avec essais=1 : repasse pret sans consommer l'essai ; le ROUGE seul bloque."""
    m = _mf(tmp_path, monkeypatch)
    _moteur(m, tmp_path, monkeypatch, "ABORT", 3, "panne_moteur"); _travail(m, 1)
    assert m.suivant(m.lire()) == 0 and m.lire()["T1"]["etat"] == "pret"
    assert m.lire()["T1"]["demarrages"][-1].get("technique") is True
    _moteur(m, tmp_path, monkeypatch, "ROUGE", 1)
    m.suivant(m.lire())
    assert m.lire()["T1"]["etat"] == "bloque"  # seul l'essai ROUGE a compté


def test_troisieme_panne_bloque(tmp_path, monkeypatch):
    """1re et 2e pannes → pret ; la 3e → bloque : plafond = 3 au total (CORR_8)."""
    m = _mf(tmp_path, monkeypatch)
    _moteur(m, tmp_path, monkeypatch, "ABORT", 3, "panne_moteur"); _travail(m, 2)
    for _ in range(2):
        m.suivant(m.lire())
        assert m.lire()["T1"]["etat"] == "pret"
    m.suivant(m.lire())
    t = m.lire()["T1"]
    assert t["etat"] == "bloque" and sum(1 for d in t["demarrages"] if d.get("technique")) == 3


def test_abort_autre_motif_inconnu_comme_avant(tmp_path, monkeypatch):
    """ABORT/triche : inconnu, démarrage non marqué, code aide 2 — comportement inchangé."""
    m = _mf(tmp_path, monkeypatch)
    _moteur(m, tmp_path, monkeypatch, "ABORT", 3, "triche"); _travail(m, 2)
    assert m.suivant(m.lire()) == 2
    t = m.lire()["T1"]
    assert t["etat"] == "inconnu" and all("technique" not in d for d in t["demarrages"])


def test_historique_mixte_budget_non_technique(tmp_path, monkeypatch):
    """Historique hétérogène (entrées anciennes sans clé) : seuls les non techniques comptent."""
    m = _mf(tmp_path, monkeypatch)
    _moteur(m, tmp_path, monkeypatch, "VERT", 0)
    _travail(m, 2, demarrages=[{}, {"technique": True}, {"technique": True}])  # 1 seul essai réel consommé
    assert m.suivant(m.lire()) == 0
    t = m.lire()["T1"]
    assert t["etat"] == "accepte" and len(t["demarrages"]) == 4  # le lancement a bien eu lieu
