---
schema: authority-map/v1
produced_at: "2026-09-07T14:44:00Z"
authority: "Directive CTO 2026-09-07 — audit du cerveau projet : 1 STRATEGY + 1 SPEC + 1 ROADMAP canoniques par projet, contradictions = 0."
enforcement: "factory/cto/canonical/authority_audit.py — receipt: AUTHORITY_AUDIT_RECEIPT.json (relecture prouvée)"
---

# AUTHORITY MAP — qui est l'autorité, fichier par fichier

**Contrat produit canonique 2026-09-07** : par projet, exactement `STRATEGY.md` +
`SPEC.md` + `ROADMAP.yaml` (runtime authorities, format consommé par factory_v2) +
`PROJECT_STATE.json` (état runtime uniquement). Règle absolue : pour chaque domaine et
chaque projet, UN seul fichier fait autorité; tout autre fichier couvrant le même domaine
est soit gelé-historique (hash contrôlé), soit dérivé. Une contradiction détectée = défaut
bloquant.

## Projet `factory`

| Domaine | Autorité | Ancienne autorité (gelée, C9) |
|---|---|---|
| STRATEGY | `projects/factory/STRATEGY.md` | `factory/cto/CTO_STRATEGY.md` (champion lit encore) |
| SPEC | `projects/factory/SPEC.md` | `factory/cto/canonical/FACTORY_MASTER_SPEC.md` |
| ROADMAP | `projects/factory/ROADMAP.yaml` | `factory/cto/canonical/CANONICAL_ROADMAP.json` |
| PROJECT_STATE | `projects/factory/PROJECT_STATE.json` | — |
| ÉTAT missions (exécution) | `factory/campaigns/app007/APP007_CAMPAIGN_STATE.json` | — (source de vérité des statuts d'exécution champion) |
| ÉTAT chaîne | `factory/cto/CTOK_CHAIN_STATE.json` | — |
| BACKLOG champion | `factory/cto/CTO_BACKLOG.json` | — (doit rester purgé des complétés) |
| PORTFOLIO | `factory/cto/PORTFOLIO_STATE.json` | — (choix de PROJET uniquement, jamais de mission) |
| PRODUIT radar (vue champion) | `factory/cto/CTO_PRODUCT_STATE.json` | — |

## Projet `twitter_radar`

| Domaine | Autorité | Ancienne autorité (gelée, C9) |
|---|---|---|
| STRATEGY | `projects/twitter-radar/STRATEGY.md` | — (nouveau) |
| SPEC | `projects/twitter-radar/SPEC.md` | `factory/cto/canonical/TWITTER_RADAR_MASTER_SPEC.md`; repo radar: `APP007_PRODUCT_SPEC_V2.md` (SUPERSEDED) |
| ROADMAP | `projects/twitter-radar/ROADMAP.yaml` | — (nouveau, milestone-centrique) |
| PROJECT_STATE | `projects/twitter-radar/PROJECT_STATE.json` | — |

L'autorité produit du radar vit dans le repo factory (le factory orchestre, le repo radar
exécute). Les `PLAN*.md` du repo radar sont des documents de MISSION, jamais le cerveau.

## Copies vendues (repo radar)

`twitter-radar-foundation-v2/factory/cto/*.json` = snapshots vendus, DOIVENT rester
identiques aux originaux (check C5).

## Invariants vérifiés par `authority_audit.py`

- C1 : chaque fichier autoritaire existe
- C2 : aucun autoritaire marqué SUPERSEDED
- C3 : statuts roadmap ≠ contradiction avec l'état de campagne
- C4 : backlog sans mission complétée
- C5 : copies vendues radar == originaux
- C6 : portfolio sans pointer stale
- C7 : une seule SPEC active par projet
- C8 : relecture des autorités prouvée aux points du cycle de vie (refs code)
- C9 : intégrité des autorités gelées (hashes) + successeurs présents
- C10 : trio + PROJECT_STATE par projet (contrat §I)
