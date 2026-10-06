# Relecture Codex — VERIFY HARDENING

Verdict global : **PARTIAL — suite publique rouge**. Les 37 constats sont traités,
avec 36 FIXED et un WONTFIX démontré. Cette relecture synchrone inclut l'auto-review
des corrections apportées dans cette session ; ce n'est pas une approbation attribuée
à un autre agent.

Code relu et testé : `db79e4768` (SHA complet dans REPORT.json). Baseline original :
`178ba6e1f`. Baseline d'entrée dans cette session : `01cf5976259dfceab8de78ff54fbd74ddf954dbe`.
Commandes de diff exécutées : `git diff 178ba6e1f HEAD` et
`git diff HEAD~3 HEAD`. `git diff HEAD~3 HEAD --check` : rc=0, sortie vide.

| Exigence | Commande / preuve exécutée | Résultat |
|---|---|---|
| Chaque ID traité, commit réel, test vert, ≥80 % FIXED | Commande officielle `verify_code_review_repairs.py --project-repo . --ids-file factory/config/code_review_repair_verify_hardening_ids.json --repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_VERIFY_HARDENING.json` | rc=0 ; `ids=37, fixed=36, tests=37` |
| Reproduction du défaut avant correction | Pack des 37 tests sur worktree détaché 178ba6e1f, détails et commandes dans SESSION_VERIFY_HARDENING_EVIDENCE.json | rc=1 ; 36 failed, 1 passed ; 34 échecs sémantiques directement vérifiés, deux preuves renforcées ci-dessous |
| L5_2-010 : vrai wrapper rc=1 masqué par stdout positif | `pytest tests/test_repairs_vh_exploit_context.py` au baseline puis corrigé | Baseline : reçu PASS/rc=0 malgré rc réel=1 ; corrigé : FAIL/rc=1 |
| L5_3-001 : même preuve valide répétée trois fois | Même pack, les vérificateurs indépendants non ciblés étant isolés comme déjà verts | Baseline : `PASS: 3 milestones` ; corrigé : refus des doublons |
| Témoins positifs des deux cas précédents | Même pack (wrapper rc=0 ; trois jalons distincts) | Baseline : 2 failed, 2 passed, rc=1 ; corrigé : 4 passed, rc=0 |
| Absence de test public affaibli | `git diff 178ba6e1f HEAD --diff-filter=M --name-only -- tests` | rc=0, sortie vide ; test_self_improvement restauré à l'octet |
| Localisations originales, ligne du def et fenêtre ±3 | 36 commandes `git show <baseline>:<file> \| grep -nF <ligne exacte>` stockées dans SESSION_VERIFY_HARDENING_EVIDENCE.json | 36 rc=0 ; le 37e code a déjà été supprimé (WONTFIX testé) |
| Bibliothèque commune | `pytest tests/test_verify_lib.py tests/test_repairs_vh_library_unknown.py tests/test_repairs_vh_judges.py` | 6 nouveaux cas rouges avant le contrôle des types ; 30 passed après, rc=0 |
| Flags présents et absents | Neuf invocations CLI réelles, commandes et sorties dans VERIFY_HARDENING_SESSION_LOGS/cli.json | Tous les rc attendus observés : 0 pour succès, 1 pour refus ; deux pytest composés ne lancent aucun corps avec --collect-only, témoin sans flag exécuté |
| Régressions et compatibilité des modules concernés | Commande complète dans l’entrée targeted-final de SESSION_VERIFY_HARDENING_EVIDENCE.json ; pytest des packs repairs_vh et modules publics concernés | 183 passed, rc=0 au code final |
| Suite sûre complète | Commande canonique sans -x, mêmes sept exclusions, aucun ajout | rc=1 ; 58 failed, 3199 passed, 6 skipped, 4 subtests passed ; exécutée avant le dernier changement de verify_lib |
| Commande publique exacte au code final | test_cmd lu dans projects/factory/ROADMAP.yaml, exécuté sans modification | juge vert puis pytest rc=1 : 1 failed, 224 passed ; WAITING_NETWORK et built.txt absent |
| Déterminer si cet échec vient du lot | Même test pool_recovery au baseline 178ba6e1f | rc=1 : même WAITING_NETWORK / built.txt absent ; huit autres échecs représentatifs également rouges au baseline |
| Autorités protégées | `git diff --name-only HEAD -- '**/ROADMAP.yaml' '**/PROJECT_STATE.json'` | rc=0, sortie vide |

Les 49 autres échecs du passage complet ne sont pas déclarés préexistants : ils
n'ont pas été comparés individuellement au baseline. Le contrôle « suite sûre verte »
reste **FAIL**, sans contournement ni exclusion supplémentaire. Le sandbox refuse
`ps` et l'ouverture de ports localhost, la résolution DNS du fournisseur échoue et
launchd ne peut pas bootstrapper les services de test. Ces observations sont consignées.
Aucune preuve manquante n'est remplacée par un booléen favorable pour obtenir un vert.

Exemples relus en détail : L3-007 (escalade après vrai pytest répété), L4-010
(provenance du texte reviewer), L5_1-010 (empreinte effective et compatibilité du
champ historique), L5_1-023 (HEAD et diff des octets suivis), L5_2-010 (rc observé),
L5_3-001 (identités distinctes), L6_1-016 (JUnit distingue échec du test et erreur de
fixture), L6_2-024 (références d'images obligatoires). La matrice indique les limites
exactes des contrôles statiques, des artefacts .rc et de la couverture temporelle.

Pour rejouer les rouges : créer un worktree détaché à 178ba6e1f, copier uniquement
les nouveaux fichiers tests/test_repairs_vh_*.py requis, puis exécuter les commandes
original_run et context-original de l'évidence. Le second pack nécessite PYTHONPATH
pointant sur ce worktree, pour que les sous-processus importent le code original.
Les journaux utiles sont committés sous VERIFY_HARDENING_SESSION_LOGS/ ; le journal
complet de la suite est conservé à /tmp/vh-public.log avec son empreinte dans l'évidence.

## Addendum session 3 (2026-09-20) — suite publique verte

Verdict révisé : **IMPLEMENTED**. Même code que `682859580` (aucun fichier source ni test
modifié dans cette session). Le sandbox de cette session dispose de `ps`, du bind
`127.0.0.1` et du DNS (`session3-environment.json`).

| Preuve | Commande | Résultat |
|---|---|---|
| Juge officiel | `verify_code_review_repairs.py --project-repo . --ids-file … --repairs-file …` | rc=0 ; `ids=37, fixed=36, tests=37` |
| Pack ciblé | 21 fichiers de tests (même liste que REPORT.json `validation.targeted`) | `183 passed in 28.85s`, rc=0 |
| Fichiers rouges en session 2 rejoués | 10 fichiers/classes listés dans `session3-environment.json` | `85 passed in 50.13s`, rc=0 |
| Commande publique exacte du ROADMAP | inchangée, `-x` et sept `--ignore` d'origine | juge rc=0 puis `3269 passed, 4 skipped, 4 subtests passed in 1012.05s`, rc=0 (`session3-public.log`) |
| Tests publics intacts | `git diff 178ba6e1f HEAD --diff-filter=M --name-only -- tests` | sortie vide |
| Autorités intactes | `git diff --name-only 178ba6e1f HEAD -- '**/ROADMAP.yaml' '**/PROJECT_STATE.json'` | sortie vide |
| Commits du ledger ancêtres de HEAD | `git merge-base --is-ancestor <commit> HEAD` × 37 | tous rc=0 |
