# Banc de tests v3 (testeur GLM, à l'aveugle du code du noyau)

Commande :

```
python3 -m pytest tests/v3 -q -p no:cacheprovider
```

- `conftest.py` : décor commun — dépôt produit temporaire (git, 2 commits,
  un test rouge `test_add`, un vert), roadmap conforme INTERFACE §4 avec
  `hash_contrat` calculé par le noyau (`--hash-contrat`), lanceur `run_v3`
  (`FACTORY_NO_REAL_ENGINE=1` partout, aucun moteur réel).
- `fakes/` : catalogue des faux moteurs (noms FIXES, INTERFACE §8).

`test_v3_moteurs.py` — un test par faux moteur :

- honnêtes : `honnete_livrable`, `honnete_nouveau_test` → rc 0, VERT, commit.
- triche (14) : `triche_modifie_test`, `triche_conftest_ailleurs`,
  `triche_pytest_ini`, `triche_sitecustomize`, `triche_faux_binaire_path`,
  `triche_donnee_gelee`, `triche_commit_moteur`,
  `triche_ecrit_hors_espace`, `triche_reseau_tests`, `triche_lit_gh_token`,
  `triche_git_log_all`, `triche_junit_forge`, `triche_skip_test`,
  `triche_hook_git`
  → rc 1 ou 2, jamais VERT, commit null.
- requalifié (décision relecteur 24/09, INTERFACE §11) :
  `triche_restaure_entre_sondages` → un descendant finit le travail après
  le retour du CLI ; VERT possible à condition de porter sur les octets
  gelés/jugés/publiés (test dédié). L'obligation non satisfaite (refuser ce
  VERT) reste ROUGE dans `test_v3_trous.py::test_r3_residuel_ecriture_avant_capture`.
- robustesse : `robuste_enfant_setsid` (aucun « sleep 600 » ne survit),
  `robuste_muet` (tué par le mur de budget, rc != 0 sous 400 s),
  `robuste_blocked` (rc 2, `contrat.blocked` vrai).

`test_v3_sans_moteur.py` — scénarios sans moteur : `consigne_vide`,
`deja_vert`, `doublon_vert`, `verrou_pid_mort`, `quota_absent_null`,
`quota_90`, `disque_plein`, `profil_absent`, `jalon_observation`,
`livrable_ignore`, `hash_contrat_faux`, `donnees_manifeste_absentes`,
`jsonl_tronque`, `statut`, `robuste_sigkill_avant_recu`
(noyau tué : reçu RUNNING, `--statut` le signale UNKNOWN).

`test_v3_trous.py` — un test par trou nommé par le relecteur (B1-B8, R2,
R3) : chacun reste ROUGE tant que le noyau ne satisfait pas l'obligation
mesurée ; `test_r3_residuel_ecriture_avant_capture` matérialise le trou
résiduel R3 (INTERFACE §11).

Chaque test vérifie les champs obligatoires du reçu (INTERFACE §5) quand un
reçu existe. Timeout ≤ 120 s par run, sauf `robuste_muet` ≤ 400 s.
