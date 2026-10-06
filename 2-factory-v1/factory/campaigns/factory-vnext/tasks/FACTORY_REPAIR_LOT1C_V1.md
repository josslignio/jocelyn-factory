# FACTORY_REPAIR_LOT1C_V1 — réparer les défauts qui font boucler la factory

## Objectif
Corriger, avec test rouge→vert pour chacun, les constats HIGH/MEDIUM de la relecture complète du code
(`factory_v2/audit/CODE_REVIEW_2026-09/*.json`) des catégories **RACE** et **ERROR_HANDLING**, plus les BUG HIGH d'effort S.
Ce sont les familles qui produisent les boucles et les états bloqués (exception avalée → état jamais mis à jour → poll infini).

## Contrat
Pour CHAQUE id ci-dessous : (1) test qui reproduit le défaut (rouge), (2) correctif minimal au fichier/ligne cités, (3) test vert,
(4) ligne dans `factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS.json` : `{"id", "status": "FIXED|WONTFIX", "commit", "test", "note"}`.
`WONTFIX` autorisé seulement avec une justification vérifiable (faux positif démontré par un test). Le juge : chaque id présent dans REPAIRS.json,
chaque FIXED avec un test existant qui passe, suite sûre verte.

## Constats (id · fichier:ligne · catégorie/sévérité · pourquoi) — 15 constats, budget 3 h : viser 100 % FIXED, sinon ≥ 80 %
- `L6_bin_lib_products_2-010` · `products/twitter_radar/tr_call.py:104` · ERROR_HANDLING/MEDIUM · _parse_dt accepte à la fois les dates-heures avec et sans fuseau. La comparaison intrajournalière mélange ensuite datetime aware et naive et lève TypeError, interrompant tout build_ledger dès qu’un corpus mixte présente 
- `L6_bin_lib_products_1-004` · `factory/bin/factoryctl.py:169` · ERROR_HANDLING/LOW · --state-dir est optionnel pour argparse, mais les commandes du cœur convertissent immédiatement None en Path. L’appel status --repo . provoque un traceback TypeError au lieu d’une erreur d’usage indiquant le paramètre ma
- `L3-018` · `factory/bin/model_router.py:616` · BUG/HIGH · Le fallback lié aux exclusions ne préserve le mode restreint Claude/GLM que pour REVIEW. Une tâche A diagnose, initialement en lecture seule, routée avec Codex exclu obtient Claude read_only=False et un argv contenant --
- `L5_factory_v2_rest_1-013` · `factory_v2/continuation_controller.py:192` · BUG/HIGH · Les fichiers de pause/arrêt sont interprétés par vérité logique. Un JSON valide mais vide {} est donc traité comme une absence ; PAUSE.json={} et SAFETY_STOP.json={} conduisent tous deux à CONTINUE. La protection fail-cl
- `L5_factory_v2_rest_1-018` · `factory_v2/fault_injection.py:61` · BUG/HIGH · Le paramètre require_in_pgrp=True est inutilisé : la fonction vérifie seulement l’existence du PID puis envoie SIGKILL. Un PID fourni par erreur hors du groupe reste ciblé. La reproduction lance uniquement son propre enf
- `L5_factory_v2_rest_1-020` · `factory_v2/long_run_truth.py:66` · BUG/HIGH · Si aucun événement ne correspond au run_id demandé, la vérification se rabat sur tout le journal. Un run TARGET sans aucune preuve est ainsi déclaré récupéré à partir des décès/naissance/progrès d’un run OTHER. Le fallba
- `L5_factory_v2_rest_2-006` · `factory_v2/run_matched_benchmark_3arm.py:787` · BUG/HIGH · verify_only compare les champs matched au reçu, mais n’exige jamais que ces contrôles soient vrais. Un jeu d’observations dont les tâches diffèrent entre bras, accompagné de son reçu correctement recalculé, sort rc=0 et 
- `L6_bin_lib_products_1-001` · `factory/bin/benchmark.py:37` · BUG/HIGH · Le point d’entrée documenté ne démarre pas lorsque PYTHONPATH n’inclut pas déjà la racine du dépôt : l’import factory_v2 précède les sys.path.insert censés le rendre disponible. Même --help échoue ; les modes run et comp
- `L6_bin_lib_products_1-005` · `factory/bin/frozen_zone_guard.py:89` · BUG/HIGH · L’exception destinée aux nouveaux résultats append-only exclut tous les chemins sous evidence/P2_runs, sans distinguer ajout, modification et suppression. Une preuve existante peut être modifiée puis commitée et le garde
- `L6_bin_lib_products_1-008` · `factory/bin/i2_gate_verdict.py:171` · BUG/HIGH · La définition gelée de manual_continue_count exige zéro pour chaque run, mais la gate compare seulement la médiane I2 à zéro. Un jeu I2 [0,0,1] obtient ici overall_gate=PASS avec toutes les autres conditions satisfaites.
- `L6_bin_lib_products_1-014` · `factory/bin/milestone.py:315` · BUG/HIGH · Une deuxième infra_failure pendant waiting_infra remplace resume_stage par waiting_infra, au lieu de garder le stade productif initial. recover(force=True) restaure ensuite waiting_infra et efface retry_after ; les appel
- `L6_bin_lib_products_1-026` · `factory/lib/candidate_registry.py:121` · BUG/HIGH · La vérification de SHA utilise le SHA demandé comme valeur par défaut de getattr. Un objet evidence avec passed=True mais sans attribut sha suffit donc à promouvoir n’importe quel QRTS, en contradiction avec l’invariant 
- `L6_bin_lib_products_1-030` · `factory/lib/convergence_gate.py:185` · BUG/HIGH · _check_artifact_missing s’arrête dès qu’un seul required_artifact existe. Avec present.json et missing.json requis, analyze retourne gaps=[] et is_converged=True. Le commentaire de module promet pourtant de contrôler cha
- `L6_bin_lib_products_2-009` · `products/twitter_radar/tr_call.py:369` · BUG/HIGH · La liste peers contient les valeurs de secteur s, puis le code les utilise comme clés market[tk]. Avec AAA et BBB dans Technology, le calcul sans carte sectorielle fonctionne mais la carte valide déclenche KeyError: Tech
- `L6_bin_lib_products_2-023` · `products/twitter_radar/tr_presentation_verifiers.py:775` · BUG/HIGH · Le juge du keepalive dit prouver les trois derniers jours, mais n’exige que trois chaînes day distinctes parmi les dernières entrées. Trois dates de l’an 2000 espacées d’un mois, avec script_sha non valide, sont VERIFIED

## Interdits
Modifier un test existant pour le faire passer. Toucher ROADMAP.yaml / PROJECT_STATE.json.

## Juge
`python3 factory_v2/verify_code_review_repairs.py --project-repo . --ids-file factory/config/code_review_repair_lot1c_ids.json --repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_LOT1C.json` puis suite sûre. Relecture Codex. Niveau A, budget 3 h.
