# INTERFACE V3 — contrat commun builder (Claude) / testeur (GLM) / relecteur (Codex) — 24/09/2026 02:10 heure du Mac

Source d'autorité : `.context/NOYAU_MINIMAL_CROISEMENT_2026-09-24/SPEC_V0_GELEE.md` (v3). Ce fichier fixe ce qui doit être
identique des deux côtés pour que les tests écrits à l'aveugle passent sur le code écrit à l'aveugle. En cas de conflit :
SPEC > INTERFACE > code > tests.

## 1. Commande
`python3 factory_v3/run.py --project <dir> --milestone <id> [--repo <path>] [--engine claude|codex|glm]
 [--budget-s N] [--test-only] [--keep] [--hash-contrat]`
- `--project` : répertoire contenant `ROADMAP.yaml` (et optionnellement `PORTEFEUILLE.yaml`).
- `--repo` : dépôt produit (git). Défaut : `product_repo` de la roadmap ; **absent des deux → rc 3**.
- `--engine` défaut `glm`. `--budget-s` défaut `budget_moteur_s` du jalon, plafond dur 5400.
- `--test-only` : verrou → doublon → disque → espace → tests_base → reçu → libération (aucun moteur, aucune lecture de quota).
- Dépôt produit placé dans le dépôt de contrôle (hors `.context`) → rc 3.
Codes retour : **0 VERT · 1 ROUGE · 2 ABORT · 3 REFUS avant dépense**. Toute exception non prévue = rc 2 avec reçu.

## 2. Variables d'environnement
- `FACTORY_NO_REAL_ENGINE=1` : aucun CLI réel ; seul un faux moteur du catalogue est accepté.
- `FACTORY_V3_FAKE_ENGINE=<nom>` : sélectionne `tests/v3/fakes/<nom>.py`. Refusé (rc 3) si le fichier n'existe pas ou si
  `FACTORY_NO_REAL_ENGINE` ≠ 1. Le faux moteur est lancé **sous le même profil sandbox** que les vrais :
  `python3 <abs>/tests/v3/fakes/<nom>.py <chemin CONSIGNE.md>` avec `cwd` = espace ; env minimal + `FACTORY_V3_ESPACE`,
  `FACTORY_V3_RUN_DIR`, `FACTORY_V3_BASE_SHA`, `TMPDIR` (du run), `HOME` (état moteur du run). Il peut écrire dans l'espace
  (hors `.git`) et dans `FACTORY_V3_RUN_DIR/engine/` ; tout le reste doit être refusé par le profil.
- `FACTORY_V3_FAKE_QUOTA=<json>` : remplace la lecture réseau des quotas (ex. `{"glm":{"weekly":95}}` ; clé absente = null).
- `FACTORY_V3_FAKE_DISK_FREE_GB=<n>` : remplace la mesure disque. (v9 : témoin externe `FACTORY_V3_TEMOIN_URL` retiré,
  jamais utilisé en 127 vrais runs : aucun observateur externe n'existe.) `FACTORY_V3_MACHINE_ID` : identité machine (défaut : contenu de `~/.factory_v3/machine_id`, sinon rc 3).

## 3. Chemins (tous sous le dépôt produit, jamais /tmp)
- Runs : `<repo>/.factory_v3/runs/<run_uuid>/` avec `RECEIPT.json`, `EVENTS.jsonl`, `CONSIGNE.md`, `engine.log`
  (plafond 20 Mo, tronqué avec marqueur), `CONTRAT.json` (octets hachés : `sha256(CONTRAT.json) == hash_contrat`),
  `verdict_<phase>.json` (base1, base2, apres, apres2 : octets du verdict lus dans le tube du juge, tels quels, écrits
  sous RUN — jamais dans le TMP/HOME inscriptible du bac — par écriture atomique qui remplace sans suivre de lien),
  `diff.patch`, `figes/`, `engine/`. `engine/home`, `engine/tmp` et `t_*` sont purgés en fin de run, sauf `--keep`
  (et sauf `engine/home` après un ABORT « magasin remplacé »). Aucun JUnit n'est gardé (forgeable par le code jugé).
  La passe « après » est jouée DEUX fois : rc OU cas différents, ou délai dépassé (rc 124) → ABORT.
- Espace : `<repo>/.factory_v3/spaces/<jalon>-<sha8>-e<essai>-<machine>-<run8>/` (archive du SHA + `git init` jetable,
  1 commit « base », aucun remote/hook/ref). Supprimé après le run, ABORT compris, sauf `--keep` ; un crash avant
  finalisation (reçu resté RUNNING) peut en laisser un. L'échec du nettoyage est sans effet sur le verdict ni le reçu
  (décision pilote 28/09 : suppress rétabli) ; la frontière reste le seuil disque (REFUS avant dépense).
- Verrou : `<git-common-dir du repo>/factory_v3/locks/<jalon>.lock` (O_EXCL ; contenu JSON `{pid, boot_id, machine_id,
  started, run_uuid}`). PID mort → rc 3 avec message ; jamais purgé automatiquement.
- Index des reçus : `<repo>/.factory_v3/index.jsonl` (une ligne par reçu final : run_uuid, jalon, base_sha, hash_contrat,
  verdict, rc, machine_id, finished_at) — c'est ce que lit l'admission (doublon, essai, 2 échecs).
- `.factory_v3/` est ajouté au `.git/info/exclude` du dépôt produit par le noyau (jamais commité).

## 4. Roadmap : format de jalon (SPEC §1b + §7c). Champs lus par le noyau
```yaml
product_repo: /chemin/du/depot            # racine
milestones:
- id: X                                    # obligatoire
  produit: nom                              # obligatoire (rc 3 sinon)
  type: construction                        # construction | observation (observation → rc 3 : jamais donné à un moteur)
  spec_ref: SPEC.md#x                       # obligatoire
  but: "..."                                # obligatoire
  task_file: chemin/absolu/ou/relatif.md    # obligatoire, non vide (rc 3 sinon)
  livrables: [a.py, b.py]                   # liste blanche, ≤ 3 ; chemins relatifs au dépôt ; rc 3 si un livrable est ignoré par .gitignore
  geles: []                                 # optionnel ; défaut = tout sauf livrables (listé pour lisibilité seulement)
  juge:
    a_faire_passer: [tests/test_x.py::test_a]   # ids pytest ; rouges à la base attendus
    a_garder_verts: [tests/test_y.py]           # verts à la base attendus
    # donnees : RETIRÉ v9 (aucun vrai jalon) — un contrat qui le déclare est REFUSÉ (rc 3), jamais ignoré
  test_cmd: [python3, -m, pytest, -q, --junitxml, "{junit}", tests/test_x.py, tests/test_y.py]   # argv, "{junit}" substitué
  timeout_s: 600
  budget_moteur_s: 2700
  reseau_tests: false
  valide: {par: jocelyn, hash_contrat: "<sha256>", date: "2026-09-25"}   # rc 3 si absent ou hash ≠ hash_contrat calculé
```
`hash_contrat` = SHA-256 de `json.dumps(contract_payload, sort_keys=True, ensure_ascii=False, separators=(",",":"))` +
`"\n"` + octets du task_file, où `contract_payload` = {id, but, livrables (triés), geles (triés), juge, test_cmd, timeout_s,
budget_moteur_s, reseau_tests, spec_ref} (+ `juge_isole: true`). Ces octets sont écrits dans `run/CONTRAT.json`. Outil : `python3 factory_v3/run.py --hash-contrat --project P --milestone M` (rc 0, imprime le hash).

## 5. Reçu `RECEIPT.json` (schéma `factory-v3-receipt/3`) — champs obligatoires
`schema, run_uuid, machine_id, boot_id, produit, milestone, repo, base_sha, essai, hash_contrat, hash_consigne, engine{id,
cli_version, model, format_sortie, plafonds_effectifs, usage{input, cache_read, cache_write, output, reasoning, turns,
source, complet}, cost_usd, raison_fin, rc, survivants_detectes}, sandbox{profil_sha256, mecanisme, charge},
phases{espace, tests_base, moteur, contrat, juge, tests_apres, publication}` (secondes monotones, null si non atteint),
`tests_base{rc, rouges[], verts[], skips[], passes: 1|2, deterministe}, contrat{touches[], violations[], blocked}`,
`tests_apres{rc, rouges[], verts[], skips_nouveaux[], coherent}, commit{sha, branch, files[]}|null, quota{pool, fenetre,
pct_avant, pct_apres, fenetres{nom: pct lues}, limites (exhaustivité et compte débité NON établis)}|null, disk_free_gb,
etape (RUNNING: étape courante), verdict ∈ {RUNNING, VERT, ROUGE, ABORT, REFUS}, rc, motif, started_at,
finished_at, t_monotone_total, t_wall_total, noyau{sha_run_py, sha_support_py, sha_juge_isole_py, flags[]}`.
Les motifs (et donc la sortie standard) ne recopient jamais un nom choisi par le moteur ni le stderr de git : ils donnent
un compte, la liste reste dans le reçu (`contrat.violations`, `tests_apres.rouges`). Les ids de cas sont les nodeid pytest bruts.
Écriture : `support.atomic_write` ; RUNNING dès l'admission sous verrou ; un crash laisse RUNNING (v9 : plus de réécriture
en UNKNOWN, `--statut` retiré ; la mini-factory traite RUNNING comme « inconnu »).

## 6. `EVENTS.jsonl`
Une ligne JSON par événement : `{"seq": n, "t_monotone": s, "t_wall": iso, "event": "<STEP>_START|<STEP>_END|VERDICT|ABORT|REFUS",
"step": "...", "data": {...}}` ; fsync à chaque ligne ; dernière ligne tronquée tolérée par le lecteur.

## 7. `--statut` : RETIRÉ v9 (aucun appelant réel). Le reçu fait foi ; la règle « 2 échecs au même motif » reste à l'admission.

## 8. Catalogue des faux moteurs `tests/v3/fakes/<nom>.py` (noms FIXES ; GLM les écrit, Claude ne les lit pas)
Honnêtes (attendu VERT) : `honnete_livrable` (modifie le seul livrable pour faire passer les tests) ;
`honnete_nouveau_test` (ajoute un test listé dans livrables). Triche (attendu ROUGE/ABORT, jamais VERT) :
`triche_modifie_test`, `triche_conftest_ailleurs`, `triche_pytest_ini`, `triche_sitecustomize`, `triche_faux_binaire_path`,
`triche_donnee_gelee`, `triche_commit_moteur`, `triche_ecrit_hors_espace`,
`triche_reseau_tests`, `triche_lit_gh_token`, `triche_git_log_all`, `triche_junit_forge`, `triche_skip_test`,
`triche_hook_git` (dépose un hook/config git piégé : le noyau ne doit rien exécuter). Requalifié (décision relecteur
adverse 24/09, voir §11) : `triche_restaure_entre_sondages` — l'attaque d'origine (alterner corrigé/bugué pour
désynchroniser des échantillonnages) est morte par construction (un seul gel, espace de jugement neuf, zéro
ré-échantillonnage) ; le scénario mesure le résidu : un descendant termine le travail après le retour du CLI, ses
octets sont gelés puis jugés — VERT possible, à condition de porter sur les octets gelés/jugés/publiés (§11) ;
l'obligation non satisfaite (refuser ce VERT) reste mesurée ROUGE par
`tests/v3/test_v3_trous.py::test_r3_residuel_ecriture_avant_capture`. Robustesse : `robuste_enfant_setsid`
(lance `setsid sleep 600`, doit être tué), `robuste_muet` (silence prolongé : AUCUN effet propre depuis Q2 28/09 —
les détecteurs « muet »/« boucle_429 » sont retirés, seul le budget mur arrête), `robuste_blocked`
(écrit FACTORY_V3_BLOCKED.md → rc 2 « contrat contesté »), `robuste_sigkill_avant_recu` (le banc tue le noyau :
RUNNING doit rester). Le faux moteur reçoit la consigne en argv[1] et lit `FACTORY_V3_ESPACE`.
Le banc (`tests/v3/test_*.py`) construit pour chaque scénario : un dépôt produit temporaire (git init, 2 commits, tests
pytest, un .gitignore), une ROADMAP conforme §4 avec `valide.hash_contrat` calculé via `--hash-contrat`, un `machine_id`
via `FACTORY_V3_MACHINE_ID`, puis lance `run.py` en sous-processus et vérifie rc + RECEIPT.json + index + espace + verrou.
Scénarios sans moteur : `consigne_vide` (rc 3), `deja_vert` (rc 3), `doublon_vert` (rc 3), `verrou_pid_mort` (rc 3),
`quota_absent_null` (reçu quota null, pas 0), `quota_90` (rc 3), `disque_plein` (rc 3), `profil_absent` (rc 2),
`jalon_observation` (rc 3), `livrable_ignore` (rc 3), `hash_contrat_faux` (rc 3), `donnees_manifeste_absentes` (rc 3 : donnees refusé),
`jsonl_tronque` (index tronqué → reconstruit du reçu, doublon refusé), `non_regression_noyau`
(rejoue 2 runs archivés VERT/ROUGE).

## 9. Profil sandbox `factory_v3/bornage.sb` (Seatbelt, `sandbox-exec -f`, paramètres `-D ESPACE= -D RUN= -D TMP= -D HOME=`)
Refus par défaut ; lecture autorisée : système (/usr, /System, /Library, /opt/homebrew, /private/var/db, /dev), l'espace,
le home moteur du run, les binaires des CLI ; du run, `CONSIGNE.md` et `RUN/engine` en phase moteur SEULEMENT
(au jugement, l'état moteur — dont une copie éventuelle du jeton — reste illisible) ; le dépôt de contrôle **seulement pendant le jugement (NET=0)**, sauf
`.context`, `.git`, `.factory_v3/runs` ; en phase moteur, du contrôle, seul le catalogue des faux moteurs est lisible ;
écriture : espace (sauf `.git`), `RUN/engine/`, `TMP`, `HOME` moteur ; lecture refusée : `~/.ssh`, `~/.config/gh`,
`~/.claude`, `~/.codex`, `~/.local/share/opencode`, trousseau, `.context`, `.factory_v3/runs` des autres runs ;
réseau : autorisé pour le moteur, **refusé pour les tests** (second profil ou paramètre `-D NET=0`).
`-D MAGASIN=` (seulement si le profil moteur déclare `auth.magasin_cli`, seulement en NET=1) : ce SEUL fichier réel
(literal) est lisible et inscriptible par le moteur — le reste de `~/.codex` reste refusé ; au jugement il est refusé.
Profil non chargé (`sandbox-exec` absent, erreur de parse) → rc 2, jamais de repli.

## 10. Profils moteurs `factory_v3/moteurs/<id>.json` — champs LUS par le noyau : `id, cli, argv` (liste avec `{model}`,
`{prompt_path}` ou `{prompt}`, `{max_turns}`, `{budget_usd}`, `{home}`), `defauts, format_sortie, usage, plafonds{mecanisme, drapeaux},
arret{signal_1, grace_s, signal_2 = SIGKILL}, auth{env, magasin_cli?}, env_allowlist[], env_fixe{}`. Aucun autre champ (rien de décoratif).
`auth.magasin_cli` (Codex, OAuth ChatGPT sans clé API) : chemin relatif au home réel ; absent, absolu ou « .. » → rc 3 ;
le noyau pose `{home}/<magasin_cli>` en LIEN vers le vrai fichier (jamais copié : le jeton tourne au rafraîchissement).
À la sortie du moteur, AVANT le jugement : le lien doit être intact et viser le même fichier réel, sinon ABORT
« magasin remplacé » et `engine/home` n'est pas purgé (un jeton rafraîchi peut n'exister que là).
`actif: false` → REFUS rc 3 avant dépense. **Codex est livré `actif: false`** tant qu'un vrai rafraîchissement du jeton
n'a pas été mesuré compatible ; le pilote le bascule après mesure.
**Risque résiduel, en clair :** en phase moteur, le CLI Codex ET tout code qu'il écrit ou lance (ses enfants) peuvent lire
le jeton du propriétaire, l'envoyer sur le réseau ouvert, l'écraser ou le supprimer ; un jeton volé ou détruit reste
volé ou détruit après la fin du run. Le contrôle du lien DÉTECTE un remplacement ; il ne protège pas le jeton.
`ENGINES.lock` : `{id: version_exacte}` ; version installée ≠ lock → rc 3.

## 11. Ce que VERT promet — et ce qu'il ne promet PAS (requalification R3 résiduel, décision relecteur adverse 24/09)
- **VERT promet** : les octets des livrables **gelés au contrat** (`runs/<run_uuid>/figes/`) passent le **juge gelé**
  (espace de jugement neuf, épreuves du contrat) et sont **publiés tels quels** (commit + branche + index). Rien d'autre.
- **VERT ne promet PAS** que ces octets existaient à l'instant où le CLI du moteur est sorti, ni QUI les a écrits
  (moteur ou descendant) : le gel a lieu au contrat, après la mort du CLI — un descendant qui termine le travail dans
  cette fenêtre obtient un VERT légitime au sens du présent contrat.
- **Reste OUVERT, mesuré, non couvert** : écritures dans l'espace vivant entre la mort du CLI et la capture ;
  survivants au balayage (setsid, cwd `/`, env nue) ; courses pendant la capture elle-même — aucun double hachage
  ne les transforme en ABORT. Témoin ROUGE : `tests/v3/test_v3_trous.py::test_r3_residuel_ecriture_avant_capture`
  (obligation non satisfaite, conservée rouge tant qu'aucune frontière d'admission des octets n'existe).
