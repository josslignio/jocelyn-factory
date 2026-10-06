"""TEST_3 — CORR_3, levier 5 (refonte du NO-GO B2bis) : au 2e essai, la consigne porte les NOMS des tests rouges du
DERNIER essai ROUGE, jamais les réponses — source tests_apres.rouges du RECEIPT (champ « recu » de la ligne
d'index), jamais motif_echec (tronqué à 400) ; nom = id.split("::",1)[1].split("[",1)[0], dédoublonné, trié, ≤ 10 ;
défaut __init__ ; une seule ligne si des noms. Tests 1, 2, 4 ROUGES sur la référence ; 3, 5 témoins verts
avant/après. Hors ligne, in-process, décor complet (r.repo — les faux rouges B2bis), fixtures en paramètre."""
import json, pathlib, sys  # noqa: E401

BANC = pathlib.Path(__file__).resolve().parent
if not (BANC / "conftest.py").is_file():  # livré hors tests/v3 : retourner au banc
    BANC = BANC.parents[2] / "DURCI_20260928" / "W_CAND_V13_REEL" / "cand_v8" / "tests" / "v3"
sys.path.insert(0, str(BANC))

import test_durci_v8 as banc  # noqa: E402

_run, noyau = banc._run, banc.noyau


def _recu(tmp_path, rouges, u, fini, base="b" * 40):
    d = tmp_path / "runs" / u
    d.mkdir(parents=True)
    (d / "RECEIPT.json").write_text(json.dumps({"schema": noyau.SCHEMA, "run_uuid": u, "milestone": "M1", "hash_contrat": "h",
                                                "base_sha": base, "verdict": "ROUGE", "finished_at": fini,
                                                "tests_apres": {"rouges": rouges}}), encoding="utf-8")
    return str(d / "RECEIPT.json")


def _rouge(recu, motif, fini, base="b" * 40):
    return {"jalon": "M1", "hash_contrat": "h", "base_sha": base, "verdict": "ROUGE",
            "run_uuid": pathlib.Path(recu).parent.name, "recu": recu, "motif_echec": motif, "finished_at": fini}


def _admis(tmp_path, monkeypatch, lignes):
    """Decor complet : tout ce qu'admission()/consigne() lisent, r.repo compris (faux rouges B2bis)."""
    monkeypatch.setattr(noyau, "reconcilier", lambda *a: None)
    monkeypatch.setattr(noyau, "fichiers_base", lambda *a: [])
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")
    monkeypatch.setenv("FACTORY_V3_FAKE_DISK_FREE_GB", "50")
    (tmp_path / "index.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lignes), encoding="utf-8")
    r = _run(tmp_path, m={"produit": "p", "but": "b", "spec_ref": "s"}, liv=["src/mod.py"], agv=[],
             task=b"faire\n", repo=tmp_path, moteur=None)
    r.admission()
    return r


_R, _T = ["juge/test_x.py::test_a[input,fuite::REPONSE_ATTENDUE]", "juge/test_x.py::test_b"], "2026-10-01T01:00:00Z"


def test_dernier_rouge_noms_seuls_sans_parametres(tmp_path, monkeypatch):
    recu = _recu(tmp_path, _R, "u1", _T)
    r = _admis(tmp_path, monkeypatch, [_rouge(recu, ",".join(sorted(_R))[:400], _T)])
    assert r.R["essai"] == 2, "préparation morte : le précédent n'est pas compté comme essai 1"
    txt = r.consigne()
    assert "ces tests du juge étaient rouges : test_a, test_b" in txt
    assert "REPONSE_ATTENDUE" not in txt and "[" not in txt and "juge/test_x.py" not in txt


def test_deux_rouges_seuls_les_noms_du_dernier(tmp_path, monkeypatch):
    lignes = [_rouge(_recu(tmp_path, ["juge/test_x.py::test_vieux"], "u1", _T), "juge/test_x.py::test_vieux", _T),
              _rouge(_recu(tmp_path, ["juge/test_x.py::test_dernier"], "u2", "2026-10-01T02:00:00Z"),
                     "juge/test_x.py::test_dernier", "2026-10-01T02:00:00Z")]
    r = _admis(tmp_path, monkeypatch, lignes)
    assert r.R["essai"] == 3, "préparation morte : les deux précédents ne sont pas comptés"
    assert "test_dernier" in r.consigne() and "test_vieux" not in r.consigne()


def test_sans_precedent_consigne_identique(tmp_path, monkeypatch):
    r = _admis(tmp_path, monkeypatch, [])
    nu = _run(tmp_path, m={"produit": "p", "but": "b", "spec_ref": "s"}, liv=["src/mod.py"], agv=[], task=b"faire\n")
    assert r.R["essai"] == 1 and "Essai précédent" not in r.consigne() and r.consigne() == nu.consigne()


def test_noms_dedoupes_tries_plafonnes_a_dix(tmp_path, monkeypatch):
    ids = [f"f/t.py::t{i:02d}" for i in range(1, 12)]
    recu = _recu(tmp_path, ids + ids[:1], "u1", _T)
    r = _admis(tmp_path, monkeypatch, [_rouge(recu, ",".join(ids + ids[:1])[:400], _T)])
    txt = r.consigne()
    assert "rouges : " + ", ".join(f"t{i:02d}" for i in range(1, 11)) in txt
    assert "t11" not in txt and txt.count("t01") == 1


def test_rouge_autre_base_aucune_ligne(tmp_path, monkeypatch):
    r = _admis(tmp_path, monkeypatch, [_rouge(_recu(tmp_path, ["juge/test_x.py::test_a"], "u1", _T, base="a" * 40),
                                              "juge/test_x.py::test_a", _T, base="a" * 40)])
    assert r.R["essai"] == 1 and "Essai précédent" not in r.consigne()
