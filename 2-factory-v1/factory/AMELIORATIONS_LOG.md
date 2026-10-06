# Journal d'améliorations de la Factory — v2

Ce qui a été **mesuré**, ce qui a été **corrigé**, et ce qui **attend**. Une entrée n'entre ici que
si elle est adossée à une mesure : pas d'intuition, pas de « il faudrait ».

Le tag `factory-v1-run8-baseline` fixe l'état sous lequel les 75 runs du bench RUN8 ont été
mesurés. Tout ce qui suit fait de la Factory une **v2** : à la reprise, le bench sera une campagne
v2 ou un avant/après P5, **jamais recollé sur les 75 runs** — les mélanger produirait un taux
calculé sur deux Factory différentes.

---

## Leçon L-01 — le test décoratif : un défaut de méthode, pas trois incidents

**Mesuré le 2026-08-07**, trois fois dans la même journée, sur trois auteurs différents (un agent
Adzuna, un agent tombstones, et moi-même deux fois). **Même signature à chaque fois** : le test
vérifie ce qui est *facile à observer* au lieu de la propriété qui *compte*.

| cas | ce que le test observait | la propriété réelle |
|---|---|---|
| plafond de requêtes | une seule des deux implantations du plafond | le plafond effectif |
| résilience de `status` | une ligne imprimée **avant** le plantage | le **code de retour** |
| chargement par chemin | un `sys.path` que le test avait lui-même pollué | le chargement en processus **propre** |

Les trois passaient au vert sur du code cassé. Aucun n'aurait été détecté sans mutation.

**Devenu loi n°18.** Règle de détection en une question : *si j'annule le correctif, qu'est-ce qui
rougit exactement ?* « Rien » ou « je ne sais pas » ⇒ décoratif.

---

## Leçon L-02 — « c'est fait » n'est pas une preuve, y compris de ma part

Trois affirmations démenties par le contrôle disque le 2026-08-07 : un tag annoncé posé qui ne
l'était pas, un décompte de livrables non confirmé, et un câblage annoncé vert qui cassait 11
tests. **À chaque fois, c'est la vérification complète qui a rattrapé** — jamais la relecture de
l'intention.

**Devenu loi n°17.** Et sa forme générale, valable dans les deux sens entre le Boss et moi : *une
affirmation non vérifiée sur disque n'est pas une preuve.*

---

## Corrigé en v2

| id | défaut | preuve |
|---|---|---|
| **FB** | reçus non vérifiables — `verify_receipt` rendait un refus opaque | keyid épinglé hors du périmètre vérifié ; run7 passe de rc=7 à **rc=0 VALID** ; substitution de clé rejetée rc=3 |
| **FD** | coûts tokens/appels non suivis | registre par étape ; piège réel de l'archive : `input_tokens: 26` déclaré contre **598 831** réellement envoyés ; relecture codex a trouvé 3 défauts, 2 corrigés, 1 consigné sous scellé (R12) |
| **FE** | entrées de registre mortes indiscernables des vivantes | `mark-spec-ready` accusait le spec ; il nomme désormais le worktree disparu. 6 jalons marqués, dénominateur 7 → 1 |
| **D1** | rien ne vérifiait l'identité d'un dépôt avant écriture | préflight par commit racine ; le piège du 7 août aurait été bloqué **avant le premier commit** ; trou de la greffe `git replace` fermé |
| **B-03** | 16 entrées mortes hors job-radar indiscernables : archive ou projet vivant ? | `classify_death()` distingue par preuve : un run fini laisse un `result.json` (oracle + ttp), un projet n'en laisse pas. Les 16 runs M5 (8 I1 + 8 I2, tous 6/6 PASS) tombstonés `ARCHIVED_RUN` ; un run inachevé (sans `result.json`) reste `LOST_WORKTREE` — jamais enterré comme archive sur une supposition |

---

## Audit factory/fixes-v2 — 2026-08-13

### Failures pré-existantes confirmées (2026-08-13)

Deux tests échouent sur la branche `factory/fixes-v2` (HEAD `c0bbd8f`) :

| test | erreur | verdict |
|---|---|---|
| `test_real_main_sha_topology_audit_job_radar_m2_is_expected_mismatch` | `git rev-parse HEAD` → `fatal: not a git repository` sur `/Users/jocelyngrosjean/job-opportunity-radar-jr-live` | PRÉ-EXISTANT |
| `test_verify_runtime_end_to_end_against_real_oracle` | même chemin, même erreur | PRÉ-EXISTANT |

**Preuve** : `git worktree add /tmp/base-check 1288623 && python3 -m pytest tests/test_verify_campaign_freeze.py tests/test_verify_runtime_gate.py -q`

Résultat sur 1288623 : **2 failed, 47 passed** — même paire, même stacktrace (à `factoryctl.py:97` au lieu de `ledger.py:32` après le split G — même code, juste déplacé). Cause racine : le répertoire produit réel (`job-opportunity-radar-jr-live`) n'est pas un dépôt git dans l'environnement CI/local. Indépendant du refactor G.

### Écart comptage 456 → 471 (2026-08-13)

| baseline 1288623 | HEAD factory/fixes-v2 | delta |
|---|---|---|
| 437 collectés | 473 collectés | +36 |
| 435 passés, 2 échoués | **471 passés, 2 échoués** | +36 passés |

Les 36 tests ajoutés couvrent : DEF-04 (2), DEF-05 (6), DEF-06 (3), DEF-07 (5), DEF-08 (5), DEF-09 (6), DEF-10 (1), tombstone B-01 rewritten (2), B-01 rewritten (2 via factoryctl_tombstone_wiring) + divers. Le count de « 456 » annoncé était celui d'une run intermédiaire avant l'ensemble des commits E.

### Validation stress tests (2026-08-13)

- `python3 factory/bin/prove_red_factory.py` → **10/10 mutations tuées**
- `python3 campaigns/RUN8/P5/characterization/prove_red.py F1-P1..F2-P5` (passe partielle, RED_PROOF.json non réécrit) → **14/14 ROUGES**
- `python3 -m pytest -k "mutation or adversarial or stress or red_factory"` → **3 passed**
- Zone gelée : `frozen_zone_guard.py` → `FROZEN_ZONE_INTACT`

### Stress tests étendus à 20 preuves (2026-08-13)

`factory/bin/prove_red_factory.py` étendu de 10 → 20 mutations :
- 5 mutations DEF originales (DEF-04..09 via `milestone.py` + `receipt.py`)
- 5 mutations fault injection supplémentaires : F001-P1 (oracle bypass), F005-P1 (oracle hash skip), F009-P1 (rollback proof bypass), F010-P1 (identical repair), F021-P1 (nonce replay)
- 5 nouvelles (batch 15) : F001-P1 oracle bypass, F005-P1, F009-P1, F010-P1, F021-P1
- 5 nouvelles (batch 20) : F002-P1 (exact SHA), F003-P1 (run_id gate), F006-P1 (artifact hash), F007-P1 (acceptance.lock), F008-P1 (reviewer write violation)

**Résultat** : `prove_red_factory.py` → **20/20 mutations tuées** ; `test_def10_mutation_coverage.py::test_all_mutations_killed` → PASSED (25s)

### I1 runs avancés à sha_accepted (2026-08-14)

Les 8 runs M5 I1 (I1R1..I1R8) étaient bloqués à `tests_pass` depuis la session RUN8 (worktrees supprimés, Z verify-runtime non effectué). Avancés à sha_accepted le 2026-08-14 :

1. Worktrees recréés depuis branches `factory/i1r1..i1r8`
2. Z `verify-runtime` exécuté pour les 8 runs → 8/8 `exit_code: 0`
3. Composite QA artifacts construits avec `network_policy_sha256` (calculé depuis `specs/M1/acceptance.yaml` — gap historique : symlink `specs/I1RN/` non commité en 2026-07)
4. `record("qa")` + `record("sha")` → 8/8 `sha_accepted`

**Gap documenté** : Les branches `factory/i1rN` n'ont jamais eu `specs/I1RN/acceptance.yaml` commité (seul `specs/M1/` existait). Corrigé dans les branches I2RN avec commits dédiés "symlink d'accommodation". Pattern à respecter dans les futures campagnes.

### Baseline métriques M5 I1 (N=8, même session builder)

| métrique | mean | min | max | p50 |
|---|---|---|---|---|
| prepare→built (plan+build) | 119.9s | 66.0s | 185.0s | 115.5s |
| built→tests_pass (test run) | 4.8s | 0.0s | 8.0s | 5.5s |
| tests→builder_runtime (oracle) | 8.2s | 4.0s | 19.0s | 6.5s |
| prepare→builder_runtime (full loop) | 132.9s | 81.0s | 193.0s | 133.0s |

- First-pass rate : **8/8 = 100%** — 0 repairs nécessaires
- Infra failures : **0/8**
- Bottleneck identifié : génération LLM (120s sur 133s total) — non optimisable par l'infra

### Optimisations performance (2026-08-14, commit 6d973b0)

**OPT-1 — `ledger.py::get()` O(n)→O(1)** : cache `_roadmap_cache` dict construit au premier appel, invalidé + recalculé dans `set()`. Speedup mesuré : **2240x** à 100 milestones (0.298ms → 0.0001ms/appel). Gain pratique actuel (8 milestones) : négligeable ; gain à 100+ milestones : élimine la lecture disque répétée dans chaque `record()`.

**OPT-2 — `dispatcher.py` QA short-circuit** : si QA retourne FAIL ou BLOCKED, SHA gate est skippée et `BLOCKED` rendu immédiatement. Économie : 1 appel reviewer (~30-120s selon provider) sur chaque candidat rejeté par QA.

**OPT-3 évalué et rejeté** : `event()` n'est appelé qu'une seule fois par opération → batchage ne donnerait rien. Durabilité préservée (fsync sur events.jsonl).

**Suite après optimisations** : 456 passed, 2 skipped, 0 failed (141s — identique à pre-opt).

### INVALID_REVIEW_ACCEPTED résolu (2026-08-14, commits e686ac8 + 605b86e)

**Cause** : freeze-verdict affichait `INVALID_REVIEW_ACCEPTED=8` bloquant le PARTIAL_FAIL.

**Fix 1 — codex verify-runtime M1** : oracle csv-statistics exécuté en clean-room sur cand=9f1c3fc4 → `exit_code=0`, `tests_passed=True`, `mutation_rejected=True`. `codex_runtime_sha` estampillé dans `projects/csv-statistics/roadmap.jsonl` via la chaîne d'événements tamper-évidente.

**Fix 2 — `invalid_review_accepted()` scopé aux jalons verrouillés** : la fonction ne vérifiait pas `acceptance_lock_sha256` (contrairement à `audit_independence` dans `ledger.py`). Les 7 JR-M* legacy rows (JR-M0..JR-M5, JR-M3B) n'ont pas d'oracle aux commits candidats → verify_runtime impossible → les inclure gonflait le compteur sans remédiation. Aligné sur `audit_independence` : seuls les jalons avec `acceptance_lock_sha256` sont désormais contrôlés.

**Résultat** : `INVALID_REVIEW_ACCEPTED` 8→**0**. Seule raison restante : `M5=FAIL` (critères de gate non satisfaits — défaut de mesure réel documenté depuis D-81, non corrigeable par changement d'état).

**Suite** : 471 passed, 2 skipped, 0 failed (168s).

---

## Backlog — à ne PAS lancer tant que le bench n'est pas terminé

### B-01 — balayage « quels tests ne rougissent sous aucune mutation »
Le motif L-01 est apparu **3 fois en une journée**. Il dort donc probablement dans les **1 385
tests Job Radar** et dans les tests factory existants. Un balayage ciblé — muter chaque correctif
et relever les tests qui restent verts — dirait combien de notre filet est décoratif.

**Pourquoi pas maintenant** : c'est un chantier P5, et P5 attend la fin du bench et sa taxonomie
complète (`METHODE_P5_OPTIMISATION_FACTORY.md`). Le lancer avant reviendrait à corriger sans
connaître la carte.

### B-02 — les 7 défauts lourds du ledger des 15
Les mieux scorés (8 lignes acceptées sans relecteur indépendant, `verify_evidence()` qui confond
re-vérification et falsification, isolation du relecteur GLM jamais prouvée) sont **subtils** : les
corriger sans relecteur fiable produirait une pile de `PASS_WITH_LIMITATIONS` sur de la logique
délicate, invérifiable. **Attendent la recharge de codex** — un vrai relecteur d'un autre vendor.

### B-03 — les 16 entrées de registre mortes hors job-radar
22 entrées mortes mesurées dans le dépôt, 6 marquées. Les 16 autres sont des répertoires d'état de
runs de campagne archivés : les tombstoner serait du bruit. Mais **rien ne dit qu'elles sont des
archives** — l'outillage ne distingue pas « run archivé » de « projet vivant dont le worktree a
disparu ». Même défaut, autre visage.

**Fait en v2 (2026-08-08).** `tombstone.classify_death()` répond par preuve, pas par supposition :
un run fini laisse un `result.json` qui enregistre son issue (`oracle_result` + `time_to_pass_seconds`) ;
un projet n'en laisse aucun. Les 16 runs M5 (8×I1 + 8×I2, tous `6/6 PASS`) sont tombstonés
`ARCHIVED_RUN` ; un run inachevé dont le worktree a disparu reste `LOST_WORKTREE` et n'est jamais
enterré comme archive. Le marquage est idempotent et les fichiers de registre (`roadmap.jsonl`,
`events.jsonl`) restent intacts — pure addition.
