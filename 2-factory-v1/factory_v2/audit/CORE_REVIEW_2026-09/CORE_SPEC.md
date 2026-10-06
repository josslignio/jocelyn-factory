# CORE_SPEC — architecture cible unique

## Propriétaires

- **État et reprise** : un workflow Temporal par jalon. Son historique est la seule position d'exécution ; Git reste l'autorité du code et Strategy/Spec/Roadmap l'autorité d'intention.
- **Exécution agent** : mini-swe-agent exécute une boucle bornée ; SWE-ReX exécute les actions typées. Les adaptateurs CLI restent Factory tant que harness.lol n'a pas de commit et de licence gelés.
- **Validation** : `IndependentValidator`, réduit à l'adaptateur exact-SHA spécifique, décide sur le contrat gelé. Un tuple `(validator_version, contract_id, data_hash, environment_hash, sha)` vert possède un seul receipt immuable et n'est jamais rejoué — c'est `ONE_VALIDATION_PER_SHA`, invariant du **challenger** (retiré du champion le 23/09 : il contredit le scénario `IDLE_NO_PROGRESS` du banc chaos v1, réglé et intouchable). Harbor reste hors cible tant qu'il n'est pas épinglé.
- **Règlement** : une activité Factory spécifique vérifie les receipts puis avance le ref Git par CAS. Ni Temporal, ni Harbor, ni un LLM ne déclare `COMPLETE`.
- **Quota et engines** : un garde unique précède tout lancement. La review à un ou N moteurs emprunte le même chemin roster. Aucun provider non épinglé, y compris llama.cpp, n'entre dans le noyau.

## Invariants conservés

1. Builder différent du reviewer et writer unique par projet.
2. Même SHA, même contrat et mêmes données pour build, validation, review et settlement ; changement d'identité invalide le receipt.
3. Toute panne est une transition typée et bornée ; aucune relance équivalente sans changement d'entrée ou politique déclarative.

## Parcours de preuve obligatoire

Mission créée → workflow durable → worktree/action typée → candidat SHA → validation exact-SHA → review roster quota-guardée → settlement CAS. Injection : tuer le worker après candidat, puis après validation ; au redémarrage, le même candidat et le même receipt sont repris, aucun moteur n'est relancé, et un validator rouge bloque avant CAS. Le test doit aussi prouver que l'ancien `factoryd → CNP → quarantine_ladder` n'est plus appelable.

## Bloc ROADMAP prêt à coller

```yaml
- id: FACTORY_MINIMAL_CORE_CHALLENGER_V1
  goal: "Transférer reprise/retry à Temporal et boucle agent/actions à mini-swe-agent + SWE-ReX ; conserver une seule autorité Factory exact-SHA/settlement CAS ; supprimer les anciens chemins après E2E panne."
  depends_on:
    FACTORY_CORE_REVIEW_V1:
      required_state: COMPLETE
    LONG_RUN_PROOF_2:
      required_state: COMPLETE
  deliverables:
    - factory_v2/challenger/temporal_workflow.py
    - factory_v2/challenger/validation_adapter.py
    - factory_v2/receipts/FACTORY_MINIMAL_CORE_CHALLENGER_V1.json
  acceptance:
    - "Pins, licences et chemins upstream présents dans VENDOR_LOCK.json ; aucun composant 'inspiré de' sans dépendance ou code vendorisé."
    - "Une vraie mission traverse workflow, agent, action, validation, review et settlement au même SHA."
    - "Kill après candidat et kill après validation : reprise exacte, zéro double lancement, zéro double settlement."
    - "Quota refusé sur le seul reviewer : zéro execve ; état WAITING_INFRA typé."
    - "Les 5 boucles du 22/09 renvoyées au challenger (GATE_REPLAYS_OLD_SHA, HEAD_PROBE_SILENT, GREEN_CANDIDATE_DISCARDED_ON_ROTATION, QUARANTINE_NO_EXIT, PARK_IGNORES_OWNER_GRANT) sont chacune un scénario de panne rejoué : injectées sur le champion (rouge) et sur le challenger (vert), même mission, même SHA."
    - "ONE_VALIDATION_PER_SHA : un verdict vert par (contract_id, sha, validator_version) est un reçu immuable ; kill après validation puis reprise = zéro revalidation, zéro double settlement."
    - "Ancien chemin factoryd/CNP/quarantine non importé ni appelé ; fichiers supprimés et LOC contrôle en baisse d'au moins 2000."
    - "Champion inchangé si latence, false_acceptance ou coût d'exploitation régressent."
  product_e2e:
    test_command: >-
      python3 -m pytest tests/test_minimal_core_challenger.py -q -p no:cacheprovider
      && python3 factory_v2/verify_chaos_drill.py --project-repo .
      && python3 factory_v2/verify_chaos_drill.py --project-repo . --set v2
    timeout_s: 7200
  multi_ai_review:
    required: true
    min_distinct_accept: 2
    reviewers: [codex, glm]
  execution:
    steps:
      - kind: implement
        executor:
          type: autopilot
          task_file: factory/campaigns/factory-vnext/tasks/FACTORY_MINIMAL_CORE_CHALLENGER_V1.md
          test_cmd: >-
            python3 -m pytest tests/test_minimal_core_challenger.py -q -p no:cacheprovider
          spec_level: A
          budget_s: 10800
```

Le juge proposé n'introduit aucun script : il utilise pytest et les deux bancs chaos existants (`factory_v2/verify_chaos_drill.py`, sets v1 et v2). `tests/test_minimal_core_challenger.py` est le fichier de tests du produit challenger (les scénarios de panne ci-dessus), pas un juge ; les noms sous `factory_v2/challenger/` sont des livrables produit. Le jalon est refusé si `loc_control` (même recompte que `verify_core_review.py`) ne baisse pas d'au moins 2 000 lignes, mesuré au SHA final.
