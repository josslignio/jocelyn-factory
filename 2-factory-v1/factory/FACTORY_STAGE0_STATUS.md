# FACTORY STAGE 0 — RAPPORT FINAL
Date : 2026-08-09
Auteur : Claude CTO (GLM a stallé, CTO a pris la main)

## SHA de référence

| Point | SHA |
|-------|-----|
| Base Stage 0 | `7295ff2` |
| Final Stage 0 | `3fb1312` |

## Commits Stage 0

| SHA | Étape | Message |
|-----|-------|---------|
| `f695051` | S0-A | factory(s0-A): fix test collection — 217 errors → 0 |
| `068e70a` | S0-B | factory(s0-B): remove dead code — 5 lines deleted |
| `59b96e2` | S0-C / B-03 | factory(v2): tombstone archived run entries (B-03) |
| `3fb1312` | S0-F | factory(s0-F): clean root — 7 MD files moved to docs/ |

## Avant / Après

| Métrique | Avant | Après |
|---------|-------|-------|
| Erreurs de collection pytest | 217 | 0 |
| Tests collectés | 170 (hors RUN8) | 434+ |
| Dead code (fonctions/constantes) | 3 définitions mortes | 0 |
| Entrées de registre mortes non classifiées | 16 | 0 (tombstonées ARCHIVED_RUN) |
| Fichiers MD à la racine | 11 | 3 (les 3 avec deps runtime) |
| Capacités nouvelles | — | quota_router, auto_lessons |

## Étapes exécutées

### A ✅ — Fix 217 erreurs de collection
`pytest.ini` ajouté avec `testpaths` explicites. 215 erreurs dans `campaigns/RUN8/`
(zone gelée), 2 dans bootstrap fixtures. Baseline : 384 passed / 3 failed (env).

### B ✅ — Dead code supprimé
Audit AST complet de `factory/bin/` (21 modules). 3 définitions mortes supprimées :
- `Factory.read_art` (factoryctl.py, 3 lignes)
- `NEVER_RETRY` (review_gate.py)
- `MIN_REASON_NOTE_CHARS` (tombstone.py)

### C ✅ — B-03 : 16 entrées archivées tombstonées
`tombstone.classify_death()` distingue ARCHIVED_RUN (result.json présent) de
LOST_WORKTREE (pas de preuve). 16 runs M5 (I1×8 + I2×8, tous 6/6 PASS) tombstonés
comme ARCHIVED_RUN avec preuve. Idempotent.

### D ⏸ — B-01 : balayage tests décoratifs — DIFFÉRÉ
AMELIORATIONS_LOG.md section Backlog : "c'est un chantier P5, et P5 attend la fin
du bench et sa taxonomie complète." RUN8 est en pause à 75/492. Lancer B-01
maintenant serait hors-séquence.

### E ⏸ — B-02 : 7 défauts lourds du ledger — DIFFÉRÉ
AMELIORATIONS_LOG.md : "Attendent la recharge de codex — un vrai relecteur d'un
autre vendor." Ces défauts (verify_evidence, isolation GLM, 8 lignes sans relecteur)
sont subtils et requièrent Codex comme second reviewer.

### F ✅ — Nettoyage racine
7 fichiers MD déplacés vers docs/ :
- `ARCHITECTURE_PROUVEE_A_REPRODUIRE.md` → `docs/architecture/`
- `BUILD_DELIVERY_HARD_RULES.md` → `docs/governance/`
- `METHODE_P5_OPTIMISATION_FACTORY.md` → `docs/campaigns/`
- `PATCH_P0_FINAL_REPORT.md` → `docs/reports/`
- `CHECKPOINT_RUN.md` → `docs/archive/`
- `FACTORY_STAGE0_GLM.md` → `docs/`
- `FACTORY_STAGE0_AUDIT.md` → `docs/`
- `README_FIRST.md` → `README.md`

Conservés à la racine (dépendances runtime) :
- `DECISIONS_AUTONOMOUS.md` — préfixe dans `BOOKKEEPING_SAFE_PREFIXES` (stop_gate.py:50)
- `MASTER_ORDER_FACTORY_V1_FINAL.md` — paramètre par défaut (verify_campaign_freeze.py:344)
- `FACTORY_VENDORED_CODE.md` — référence docstring (stop_gate.py:9)

### G ⏸ — Compaction factoryctl.py — DIFFÉRÉ
factoryctl.py : 1195 lignes, classe Factory avec 40+ méthodes fortement couplées.
Split ledger/milestone/receipt faisable mais risqué sur le core de la Factory.
Décision : différer à un run dédié avec Codex en reviewer.

### H ✅ — Audit 3 schémas JSON
- `plan_verdict.schema.json` : actif (PLAN_ACCEPT/PLAN_BLOCK dans tests et code)
- `qa_verdict.schema.json` : actif (QA_ACCEPT/QA_BLOCK)
- `sha_verdict.schema.json` : actif (MILESTONE_ACCEPT/MILESTONE_BLOCK)
- Aucun schéma supprimé (tous utilisés)
- Limitation documentée : vocabulaire utilisé en string literals, pas de
  `jsonschema.validate()` en code de production. Validation live = RUN futur.

## Suite finale

```
python3 -m pytest -q --tb=no
# 434+ passed, 3 failed (pré-existants, env-dépendants)
# test_real_main_sha_topology_audit_job_radar_m2_is_expected_mismatch
# test_verify_runtime_end_to_end_against_real_oracle
# test_bootstrap_dod::test_full_run_accepts_good_candidate_and_writes_pass_proof
```

```
python3 factory/bin/factoryctl.py status   # répond sans erreur
git diff --check                           # 0 erreurs whitespace
```

## Limitations connues

- D/E différés (B-01/B-02) : hors-séquence per AMELIORATIONS_LOG
- G différé : compaction factoryctl.py requiert session dédiée + Codex reviewer
- 3 fichiers MD restent à la racine (dépendances runtime à câbler avant déplacement)
- Schémas JSON non enforced programmatiquement en production

GLM FACTORY STAGE0: READY_FOR_CLAUDE_REVIEW
