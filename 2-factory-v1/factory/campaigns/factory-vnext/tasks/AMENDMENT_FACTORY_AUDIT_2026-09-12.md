# AMENDMENT v3 — FULL_CODEBASE_STATE_OF_ART_AUDIT split (apres fusion CODEX-LOT-C37, HEAD fb2e3808)
5 FINDINGS CORRIGES : (1) C37 fusionne+verifie ; REVIEWED fournit le SHA via
`multi_ai_review.review_sha_from_artifact` (jamais un local_command sans SHA) ; (2) producteur/
verifieur parametres par jalon (`build_observation(milestone=...)`, `verify_audit_receipt.py
<receipt> <milestone>`), ensemble EXACT {codex,claude,glm} (`REQUIRED_REVIEWER_ENGINES`), pas "3
quelconques" ; (3) `review_accounts.jsonl` scelle HMAC + recoupe `attempt_accounts` -- un JSONL
ecrit a la main ne compte plus ; (4) Codex relecteur via `codex_reviewer_enabled=True` + route
verifiee ; MIN_REVIEWERS reste 3, aucun seuil abaisse.
NOUVEAU FINDING (lu dans `authority_gate.py:44-48` + `runtime.py:5362-5537`) : `test_command` ne
peut PAS etre verify_audit_receipt.py en semantique SETTLED pour REVIEWED --
`milestone_complete` exige `product_e2e_pass` AVANT le bloc multi-AI (`:5537+`) -> verrou circulaire
garanti sinon. `test_command` reste le gate MECANIQUE FAIBLE (7 clauses) ; verify_audit_receipt.py
`<receipt> REVIEWED` devient la confirmation post-COMPLETE, pas le gate.
VERITE A HEAD (executee, pas projetee) : `build_observation(milestone='FULL_CODEBASE_AUDIT_REVIEWED')`
-> `unmet=['artifacts_bound_to_head']` : le receipt (control_sha 32362f77) est deja PERIME depuis
C37 (fb2e3808) ; RECEIPT_CURRENT doit rejouer les 8 artefacts avant toute revue.
`audit_observation_producer` n'a AUCUNE CLI `-m` -- appel via `python3 -c` (import direct), jamais
`-m`. `{executor: {type: local_command, task_file: ...}}` invalide : local_command exige `command`,
pas `task_file` -- corrige.
COUT/QUOTA : Codex+Claude+GLM lecture seule (~600s chacune), builder="local" n'exclut aucun des 3.
GLM ~8% hebdo : refus quota -> `REVIEW_ENGINE_QUOTA_REFUSED`, jalon honnetement incomplet (reset 15/09).
VALIDE : `git apply --check` OK + `yaml.safe_load` OK (27 milestones).
