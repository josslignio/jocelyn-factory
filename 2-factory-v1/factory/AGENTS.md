# AGENTS.md — Jocelyn App Factory / Job Radar

Lire `FACTORY_LAW.md`, la spec du milestone actif et les lessons validées avant toute action.

## Rôles (renvoie aux autorités canoniques — CTO DIRECTIVE 032 §7)

Les rôles opérationnels font autorité dans `projects/<project>/STRATEGY.md` et le runtime
`factory_v2/` (dispatch/leases/fencing), jamais dans ce fichier. En cas de divergence,
l'autorité canonique gagne et la divergence est enregistrée.

- Hermes : coordonne et mémorise ; ne modifie jamais le code.
- Writer unique PAR PROJET : GLM/Z Code (`zai-coding-plan/glm-5.3`) est le writer principal
  du control-repo Factory (Orders 024-026) ; aucun projet n'a jamais deux writers simultanés
  (locks, leases, génération, fencing).
- Claude : reviewer CTO read-only aux gates exact-SHA (jamais writer de routine ; lease writer
  borné uniquement après escalade typée Order 026).
- Codex/Sol : falsificateur adversarial exceptionnel, read-only, verdicts exact-SHA.
- factoryctl : valide les preuves et transitions.

## Job Radar

- NO-GO jusqu'à JR-M1 puis JR-M2 acceptés selon les gates.
- Apply Assist reste suspendu.
- GET-only pour le sourcing.
- Aucun auto-apply.
- Aucun paid API.
- Aucun scraping authentifié.
- Aucun PII réel dans les tests/canaries.
