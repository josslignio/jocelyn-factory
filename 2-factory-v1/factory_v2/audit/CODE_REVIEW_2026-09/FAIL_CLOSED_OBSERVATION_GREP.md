# FAIL-CLOSED-OBSERVATION — grep systématique des replis « exception/absence ⇒ succès »

Livrable de synthèse du jalon FACTORY_REPAIR_FAIL_CLOSED_OBSERVATION_V1 (thème 2 de SYNTHESIS.md).
Baseline scannée : `a0c94470c` (les 11 constats du lot + les 11 du lot 1 déjà FIXED à cette base).

## Commande (reproductible)
```
python3 factory_v2/audit/CODE_REVIEW_2026-09/scan_fail_open_sites.py > FAIL_CLOSED_OBSERVATION_GREP.tsv
```
Scan AST de `factory_v2/`, `factory/bin/`, `factory/lib/` (tests exclus) : handlers `except` dont le corps
est `pass` / `continue` / `return <valeur bénigne>` (None, True, False, 0, [], {}, "", set()) et gardes
`if not <path>.exists()/is_file(): return <valeur bénigne>`.

| Motif | Sites |
|---|---|
| EXCEPT_PASS | 155 |
| EXCEPT_RETURN (valeur bénigne) | 159 |
| EXCEPT_CONTINUE | 93 |
| EXCEPT_LOG_RETURN | 17 |
| MISSING_FILE_RETURN | 64 |
| **Total** | **488** |

Périmètre du jalon (« lecteurs d'état, preflights et juges ») : 104 sites dans
`verify_*`, `*preflight*`, `safety_quarantine`, `worker_lease`, `runrecords`, `launch_gate`,
`escalation_ladder`, `resource_accounting`, `process_control`, `worker_heartbeat`, `long_run_truth`,
`mission_evidence`, `quarantine_ladder`, `authority_gate`, `completion_detector`, `progress_watchdog`,
`review_dispatch`, `factory_verifiers`, `audit_loop`. Chaque site a été lu avec son appelant.

## Classification (périmètre)

Légende : **FAIL_CLOSED** = le repli refuse/bloque déjà (direction sûre) · **BÉNIN** = le repli ne porte
aucun verdict (nettoyage, best-effort non décisionnel, valeur sentinelle traitée comme inconnue par
l'appelant) · **FAIL_OPEN → RÉPARÉ** = repli favorable corrigé dans ce jalon (rangée `FCO_SYNTH-nnn`)
· **FAIL_OPEN (suivi)** = repli favorable démontré par lecture, non réparé ici (borné par le budget),
à absorber par gravité.

| Site (baseline a0c94470c) | Repli | Verdict | Pourquoi |
|---|---|---|---|
| `factory_v2/launch_gate.py:949` `_is_live` | `except Exception: return False` | **FAIL_OPEN → RÉPARÉ** FCO_SYNTH-001 | une sonde en panne = « mort » ⇒ tentative OPEN fermée KILL/PROCESS_LOST ; contournait `LivenessObservationUnknown` (L2_runtime_core-012) à son seul appelant de production |
| `factory_v2/mission_evidence.py:150` `read_heartbeat` | `except Exception: return None` | **FAIL_OPEN → RÉPARÉ** FCO_SYNTH-002 | heartbeat tronqué/illisible = « aucun worker » ⇒ `is_worker_alive` False ⇒ second writer possible ; désormais `HeartbeatObservationUnknown`, absent reste None |
| `factory/bin/factoryd.py:1099` `_live_engine_lease_ids` | `except Exception: continue` | **FAIL_OPEN → RÉPARÉ** FCO_SYNTH-002 | appelant : liveness inconnue ⇒ appartenance non établie (None), la purge d'orphelins retombe sur le plafond d'âge au lieu de tuer |
| `factory/bin/factoryd.py:2721` `v2_status_summary` | (propagation brute) | **RÉPARÉ** FCO_SYNTH-002 | liveness inconnue rapportée `LIVENESS_UNKNOWN` + compteur `liveness_unknown`, jamais fondue dans IN_FLIGHT/BLOCKED |
| `factory_v2/advance/milestone_selection.py:158,264` | (propagation brute / `except Exception: pass`) | **RÉPARÉ** FCO_SYNTH-002 | inconnu ⇒ livrables réservés ; inconnu ⇒ la garde d'inactivité ne cède pas |
| `factory_v2/escalation_ladder.py:441` `read_pending_recovery_work` | `except Exception: continue` | **FAIL_OPEN → RÉPARÉ** FCO_SYNTH-003 | enregistrement de recovery corrompu = « rien en attente » ⇒ jalon redispatché comme si l'échelle n'avait jamais tiré ; désormais `RecoveryLedgerUnknown` |
| `factory_v2/runtime.py:3448` `_cnp_peek`, `:2690` `_run_quarantine_cycle`, `advance/mission_dispatch.py:187`, `advance/milestone_guards.py:61` | `except Exception: pending = None` / propagation | **RÉPARÉ** FCO_SYNTH-003 | appelants : `CycleResult("RECOVERY_LEDGER_UNKNOWN")` typé et bloquant + événement |
| `factory_v2/runtime.py:3257` `_cnp_regrant_stranded_diagnose` | `except Exception: return None` | BÉNIN | None = « rien de stranded » ; la lecture suivante (mission_dispatch) bloque désormais |
| `factory_v2/runtime.py:466` (sélection portefeuille) | `except Exception` ⇒ garder le projet | FAIL_CLOSED | « unknown keeps » documenté |
| `factory_v2/recovery_allocation.py:627` | `except Exception: return "RECOVERY_UNREADABLE"` | FAIL_CLOSED | refus typé |
| `factory_v2/escalation_ladder.py:416` `_recovery_is_stale` | `except: return ""` | FAIL_CLOSED | "" = pas périmé ⇒ la recovery est conservée |
| `factory_v2/escalation_ladder.py:467,536,622` | `continue` sur JSON corrompu | BÉNIN | ne rendent qu'un sur-ensemble « rien supersédé / rien migré » ; l'enregistrement corrompu est ensuite attrapé par `read_pending_recovery_work` (INCONNU) |
| `factory_v2/escalation_ladder.py:675,694,830` | écriture best-effort | BÉNIN | échecs d'écriture non décisionnels ; 694 renvoie False = non supersédé |
| `factory_v2/resource_accounting.py:369` `read_rows` | ledger absent ⇒ [] | FAIL_CLOSED | absent = aucun compte (projet neuf) ; corrompu ⇒ `AccountingLedgerUnknown` (test_corrupt_durable_markers) |
| `factory_v2/resource_accounting.py:453` | `is_live` lève ⇒ `LivenessObservationUnknown` | FAIL_CLOSED | L2_runtime_core-012 |
| `factory_v2/worker_lease.py:211-213` `_read_nolock` | absent ⇒ None ; corrompu ⇒ `LeaseObservationUnknown` | FAIL_CLOSED | trinité déjà appliquée aux leases |
| `factory_v2/worker_lease.py:172` `is_expired` | `except: return True` | FAIL_CLOSED | date illisible = expiré (refus) |
| `factory_v2/worker_lease.py:140,188` `_is_pid_alive`, `is_process_group_alive` | `PermissionError ⇒ False` | **FAIL_OPEN (suivi)** | PermissionError = le processus EXISTE (pas le nôtre) ; `completion_detector.is_worker_dead` et `runrecords._worker_still_alive` le lisent VIVANT — incohérence : un lease vivant peut être réputé mort à l'adoption (G+1 accordé). Hors budget ici ; à traiter avec un test `os.kill → PermissionError` |
| `factory_v2/safety_quarantine.py:1091-1093` `_read_quarantine` | absent ⇒ None ; corrompu ⇒ bloquant | FAIL_CLOSED | test_corrupt_durable_markers |
| `factory_v2/safety_quarantine.py:167,495` `from_dict` | `TypeError ⇒ None` | BÉNIN | None = contrat/baseline absent ⇒ la quarantaine RESTE posée (aucune levée sans contrat) |
| `factory_v2/safety_quarantine.py:656` `SafetyClearReceipt.load` | ⇒ None | FAIL_CLOSED | pas de reçu = pas de levée |
| `factory_v2/mission_evidence.py:57,68,95` `get_process_birth_time` | `pass` ⇒ "" | FAIL_CLOSED | "" ⇒ `is_worker_alive` False (identité de naissance REQUISE, F-5) — direction : re-dispatch/fence, jamais deux writers |
| `factory_v2/mission_evidence.py:173,181` | ts illisible / os.kill échoue ⇒ False | FAIL_CLOSED (ts) / cf. suivi PermissionError | un ts absurde écrit par notre propre writer n'est pas une corruption disque ; PermissionError même remarque que worker_lease |
| `factory_v2/mission_evidence.py:209-321` | nettoyage / sidecar best-effort | BÉNIN | non décisionnel |
| `factory_v2/runrecords.py:126,227,271` | ligne de queue tronquée ⇒ `continue` | BÉNIN (documenté L5_rest_2-007) | tolérance délibérée d'UNE ligne d'append interrompu ; L1_advance-001 couvre la panne durable |
| `factory_v2/runrecords.py:186-195` `_worker_still_alive` | PermissionError ⇒ True, OSError ⇒ False | FAIL_CLOSED | vivant par défaut ⇒ la prédiction n'est PAS fermée (conservateur) |
| `factory_v2/process_control.py:*` | ProcessLookupError/PermissionError ⇒ pass/False | BÉNIN | primitives de kill : « groupe déjà vide » n'accorde rien |
| `factory_v2/launch_gate.py:161,675,731-773,888,916,940,967` | télémétrie / projection best-effort | BÉNIN | aucune de ces valeurs ne décide d'un règlement ; 888 : projection illisible ⇒ rejouée depuis le ledger (idempotent) |
| `factory_v2/long_run_truth.py:32` `_read_jsonl` | absent ⇒ [] | FAIL_CLOSED | l'appelant lève `Unproven` sur couverture nulle / corruption (L5_rest_1-022, test_long_run_journal_corruption_is_unknown) |
| `factory_v2/review_dispatch.py:205,209` `verify_supervised_launch` | ⇒ False | FAIL_CLOSED | refus |
| `factory_v2/review_dispatch.py:396,403` `_builder_engine_from_runrecords` | ⇒ None | **FAIL_OPEN (suivi)** | None ⇒ `exclude_engines=None` : un ledger illisible laisse le routeur choisir l'engine du builder comme relecteur (indépendance non garantie) ; à traiter : refus typé quand le ledger existe mais est illisible |
| `factory_v2/verify_reshape_receipt.py:166,175` | guard non chargeable ⇒ None | FAIL_CLOSED | l'appelant refuse (« cannot be machine-established ») |
| `factory_v2/verify_scale_n_benchmark.py:52,56` | reçu canonique illisible ⇒ None | FAIL_CLOSED | rc=2 « cannot re-observe » |
| `factory_v2/verify_controlled_long_run.py:347,357` `_has_real_artifact` | ⇒ False | FAIL_CLOSED | absence d'artefact = refus |
| `factory_v2/verify_factory_hygiene.py:*`, `verify_deep_research.py:151` | lignes/fichiers illisibles ignorés | BÉNIN | contrôles d'hygiène additifs (un site ignoré ne rend jamais un contrôle « vert » : chaque check exige une observation positive) |
| `factory_v2/factory_verifiers.py:673-834` | `except <Refus attendu>: pass` | BÉNIN | contre-preuves : l'exception EST le résultat attendu ; 731-754 collectent des pids indépendants (sur-ensemble) |
| `factory_v2/audit_loop.py:524` `verify_integrity` | ⇒ False | FAIL_CLOSED | refus |
| `factory/lib/completion_detector.py:60-91` | ProcessLookupError ⇒ mort, PermissionError ⇒ vivant, heartbeat absent ⇒ stale | FAIL_CLOSED | trinité correcte ; `json.loads` d'un heartbeat corrompu PROPAGE (bloquant) |
| `factory/lib/progress_watchdog.py:70-124` | heartbeat corrompu ⇒ None ⇒ possible STALLED | BÉNIN (plan de contrôle V1, `com.factory.autopilot.watchdog` désactivé 2026-09-13, un seul plan de contrôle = factoryd) | hors chemin de production ; noté pour le retrait V1 (thème 12) |
| `factory/bin/receipt.py:189` | wait timeout ⇒ pass | FAIL_CLOSED | `infra_failure` émis juste après |

## Trinité VIVANT / MORT / INCONNU — état après ce jalon
| Contrôle | VIVANT | MORT | INCONNU (typé, bloquant) |
|---|---|---|---|
| heartbeat mission (`mission_evidence`) | marqueur frais + pid + identité de naissance identique | marqueur absent, périmé, pid inexistant, identité différente | `HeartbeatObservationUnknown` (marqueur illisible/corrompu) — FCO_SYNTH-002 |
| tentatives OPEN au boot (`launch_gate` → `resource_accounting`) | `is_live` True ⇒ reste OPEN | `is_live` False ⇒ TERMINAL/KILL | `LivenessObservationUnknown` ⇒ `LOST_ATTEMPT_RECONCILE_UNKNOWN` — FCO_SYNTH-001 |
| lease worker (`worker_lease`) | ACTIVE non expiré | absent / EXPIRED / FENCED | `LeaseObservationUnknown` (corrompu) — lot précédent |
| journal long-run (`long_run_truth`) | événements couvrant la fenêtre | — | `Unproven` (corrompu / couverture nulle) — L5_rest_1-022 |
| quarantaine (`safety_quarantine`) | fichier valide `state=SAFETY_QUARANTINE` | absent | corrompu ⇒ bloquant (`is_in_quarantine` True) — lot précédent |
| ledger recovery (`escalation_ladder`) | enregistrement pending lisible | aucun enregistrement | `RecoveryLedgerUnknown` ⇒ `RECOVERY_LEDGER_UNKNOWN` — FCO_SYNTH-003 |
| ledger comptes (`resource_accounting.read_rows`) | lignes valides | ledger absent | `AccountingLedgerUnknown` — lot précédent |

## Tests d'injection d'erreur par lecteur
`tests/test_fail_closed_observation_repairs.py` (11 constats + marqueurs durables) et
`tests/test_fail_closed_observation_synthesis.py` (FCO_SYNTH-001..003 : sonde qui lève, heartbeat
`{broken` / `[1, 2]`, enregistrement de recovery `{broken` / forme invalide, appelants advance/factoryd).
