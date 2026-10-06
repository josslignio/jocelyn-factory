# FACTORY_V2_RESHAPE — AUTHORITY MAP (challenger)

Une seule autorité par domaine. Tout le reste est dérivé, gelé-à-l'instant-T, ou
evidence — jamais une source de direction produit.

## Autorités de DIRECTION (le cerveau)

| Domaine | Fichier autoritaire | Qui le lit |
|---|---|---|
| WHY / doctrine | `<project>/STRATEGY.md` | resolver (hash) + contrat + barrière contexte |
| WHAT / comportement | `<project>/SPEC.md` | idem |
| HOW / ordre + DoD | `<project>/ROADMAP.yaml` (milestones, depends_on, acceptance, product_e2e) | resolver — UNIQUE source de next-work |
| Choix de PROJET | `<portfolio_dir>/portfolio.json` | `select_project` — projets seulement, aucune mission |

## Autorités d'ÉTAT (progression, jamais direction)

| Domaine | Fichier | Règle |
|---|---|---|
| Progress projet | `<project>/PROJECT_STATE.json` | completed_milestones + contrats + missions dérivées + events append-only |
| Contrat gelé | `<project>/validation_contracts/<id>.json` | écrit UNE fois avant build; immuable ensuite |
| Evidence | `<project>/evidence/<MID>/` | validator_verdict.json + logs — preuve, pas décision |

## Invariants (testés T1-T15)

```
DERIVED_ARTIFACT_OVERRIDES_CANONICAL_ROADMAP = 0     (T2, T3, T11)
NEXT_WORK_NOT_DERIVED_FROM_CANONICAL_ROADMAP = 0     (T1, T4, T12)
DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT = 0 (T8, T9)
MILESTONE_SETTLED_WITHOUT_VERIFIED_CONTRACT = 0      (T6, T7, T15)
MISSION_PASS_EQUALS_MILESTONE_COMPLETE = 0           (T6, T7)
```

## Démotions décidées (détail dans FACTORY_V2_RESHAPE_PRUNE_LEDGER.json)

- `PORTFOLIO_STATE.next_mission_id` : **DELETE** (le portfolio choisit le projet, jamais la mission)
- `long_run_supervisor` next-work : **DEMOTE** (remplacé par le resolver)
- `cto_planner` dual backlog : **DEMOTE** (missions compilées depuis le milestone)
- `backlog_refiller` : **DELETE** (queue matérialisée par le resolver, T12)
- gate milestone advisory : **ADAPT → BLOQUANT**
