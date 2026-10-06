# FACTORY_PORTFOLIO_ISOLATION_V1 — un projet illisible ne bloque pas le portefeuille

## Objectif
`select_project` refuse UN projet dont l'autorité/l'état est illisible (incident typé `PROJECT_AUTHORITY_UNREADABLE` écrit dans `projects/<p>/evidence/progress_incidents.jsonl`, une fois par cause) et continue la rotation sur les projets valides. `CONTROL_PLANE_ERROR` global uniquement si AUCUN projet n'est valide.

## À construire
`factory_v2/runtime.py` (select_project) : conserver la validation de toutes les entrées ; les entrées invalides deviennent des incidents par projet (dédupliqués par empreinte d'erreur) ; sélection parmi les valides. Tests : mauvais roadmap + bon projet → bon projet sélectionné ; tous mauvais → CONTROL_PLANE_ERROR.

## Juge épinglé
`portfolio-isolation` : drill réel (portfolio bad+good → good sélectionné, incident durable côté bad).
