# FACTORY_LEDGER_ENGINE_KNOWN_V1 — plus de moteur « UNKNOWN » dans le ledger

## Objectif
Chaque ligne TERMINAL du ledger porte un moteur vrai : `none` (refus avant execve : preflight, quota, budget) avec `refusal_reason`, ou le moteur projeté depuis l'argv du reçu (`opencode run … glm-5.3` → glm, `codex exec` → codex, `claude -p` → claude). `UNKNOWN` n'est plus jamais écrit après cette date.

## À construire
`factory_v2/launch_gate.py` : `engine_for_close(receipt_or_refusal: dict) -> str` (argv → moteur ; refusal → 'none') et son usage dans `close_account` ; constante `ENGINE_NONE_REFUSED`. `factory_v2/resource_accounting.py` : fixture avec moteur connu. Le routage adaptatif ignore les lignes `none`.

## Juge épinglé
`ledger-engine-known` : drill sur `engine_for_close` + aucune ligne UNKNOWN postérieure au 17/09 12:00Z.
