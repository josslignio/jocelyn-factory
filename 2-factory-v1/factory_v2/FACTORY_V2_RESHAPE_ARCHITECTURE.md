# FACTORY_V2_RESHAPE — ARCHITECTURE (challenger, Étape 6)

**Statut : CHALLENGER — le champion (factory/bin + factory/lib) reste FROZEN et runnable.**
**Preuves : T1-T15 15/15 (`tests/test_factory_v2.py`) + E2E réel multi-processus
(`factory_v2/receipts/FACTORY_V2_RESHAPE_E2E_RECEIPT.json`).**

## 1. Défaut central corrigé

Le champion possède une roadmap canonique MAIS le next-work runtime ne la lit jamais :
il dérive de `PORTFOLIO_STATE.next_mission_id` + campaign queue + CTO backlog
(3 autorités parallèles). Drift observé et prouvé le 2026-09-07 (spin 50 min sur un
pointer stale). En V2 :

```
DERIVED_ARTIFACT_OVERRIDES_CANONICAL_ROADMAP = 0
NEXT_WORK_NOT_DERIVED_FROM_CANONICAL_ROADMAP = 0
DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT = 0
```

## 2. Chaîne d'autorité (l'unique chemin runtime)

```
PORTFOLIO (choisit le PROJET)                      runtime.select_project
  └─ PROJECT AUTHORITY (trio + hashes)             project_authority.load_authority
       └─ ROADMAP RESOLVER (choisit le MILESTONE,  roadmap_resolver.resolve
            statut DÉRIVÉ — jamais stocké)
            └─ DEPENDENCY RESOLVER (depends_on/
                 required_state=COMPLETE)               (dans le resolver)
                 └─ VALIDATION CONTRACT (gelé AVANT     validation_contract
                      le build, lié aux hashes)
                      └─ MISSION COMPILER (missions     mission_compiler
                           dérivées, recalculables)
                           └─ BARRIÈRE CONTEXTE FRAIS   runtime.advance
                                (hashes re-vérifiés avant CHAQUE dispatch)
                                └─ EXECUTOR (local_command; adapters
                                     IA: même contrat)
                                     └─ INDEPENDENT VALIDATOR     authority_gate
                                          (black box, écrit ses preuves)
                                          └─ GATE AUTHORITATIF
                                               (settlement IMPOSSIBLE
                                                sans contrat vérifié)
                                               └─ MILESTONE SETTLED
                                                    (idempotent par contract_id)
                                                    └─ PROJECT STATE UPDATE
                                                         └─ NEXT ROADMAP ACTION
```

## 3. Modules (891 LOC)

| Module | Rôle | Décisions externes appliquées |
|---|---|---|
| `project_authority.py` | trio canonique + AuthorityIdentity (strategy/spec/roadmap sha) | ADAPT Spec Kit (résolution live) |
| `roadmap_resolver.py` | position milestone dérivée: next/blocked/why/final DoD; cycle-detection | ADAPT Temporal (décisions rejouables) |
| `validation_contract.py` | contrat gelé avant build, hash-lié; détection de drift | ADAPT Factory Missions |
| `mission_compiler.py` | milestone → missions dérivées (id `MID#M<n>`) | — |
| `authority_gate.py` | validateur indépendant + gate BLOQUANT + verdict persisté | ADAPT Factory Missions + Spec Kit converge |
| `executors.py` | adaptateur borné (local_command) — les workers IA seront des adapters | REJECT migrations majeures sans benchmark |
| `runtime.py` | la boucle `advance()` — un pas = rechargement frais complet | ADAPT Temporal (settlement idempotent) |

## 4. Sémantique clé

- **Mission PASS ≠ milestone COMPLETE** : le settlement exige le contrat gelé vérifié
  par le validateur indépendant (T6, T7, T15).
- **Restart-safe** : aucune mémoire runtime entre les pas ; PROJECT_STATE + contrats
  gelés + evidence = seule mémoire. Rejouer un settlement est sans effet (clé contract_id).
- **Autorité changeante** : tout changement de hash (strategy/spec/roadmap) après gel
  du contrat bloque le dispatch et le settlement (T8, T9) — `STALE_CONTEXT_DISPATCH_BLOCKED`
  / `AUTHORITY_DRIFT_BLOCKED`, événements persistés.
- **Le portfolio ne connaît plus les missions** : `select_project` filtre seulement
  les projets ayant du travail (T1, T14).

## 5. Ce que Étape 6 n'est PAS (conforme au GO)

Pas d'adaptive routing, pas de multi-worker, pas de self-learning, pas de migration
de framework, pas de self-modification live. Ces capacités viennent APRÈS benchmark
champion vs challenger (même workloads, restart, injection de failures, perte de contexte).

## 6. Qualification

Un milestone V2 critique exige : IMPLEMENTED / WIRED / RUNTIME_EXERCISED /
AUTHORITATIVE / CONTINUOUSLY_ENFORCED / PRODUCT_E2E_PASS / EVIDENCE_PRESENT —
le verdict du validateur persiste ces niveaux dans `evidence/<MID>/validator_verdict.json`.
