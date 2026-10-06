# FACTORY_CONVERGENCE_RESHAPE

## Outcome

(verbatim `goal` du jalon `FACTORY_CONVERGENCE_RESHAPE` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Appliquer exactement les décisions REPLACE/DELETE/WRAP de l'audit en one challenger isolé. Objectif: moins de code, moins d'autorités, moins de chemins, runtime plus solide. Jamais de nouveau subsystem — uniquement les suppressions/remplacements identifiés.


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- factory_v2/receipts/CONVERGENCE_RESHAPE_RECEIPT.json

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- every DELETE decision applied (code actually removed)
- every REPLACE decision applied with the OSS wrapped
- LOC reduced, or an explicit justification when it is not
- the full test suite passes on the reshaped codebase
- no new subsystem outside the audit decisions

## Bounded scope
- Input is `factory_v2/audit/FACTORY_CONVERGENCE_LEDGER.json`, decision by decision.
- One commit per decision, each naming the ledger decision id.
- A decision you cannot apply safely stays UNAPPLIED in the receipt with its reason.
- The receipt carries the before/after test counts you actually ran.

## Stop conditions
- Budget exhausted → stop and leave the evidence written. A truthful incomplete run beats
  an authored receipt.
- Any acceptance clause you cannot produce independent evidence for → leave it unmet and
  say so in the receipt. `VERIFIER_NOT_IMPLEMENTED` / unmet is an outcome; a fabricated
  boolean is a defect.
- Never write a receipt field the verifier would grade unless a run produced it.

## Amendement 20/09 (opérateur) — zone gelée
`campaigns/RUN8/**` est une zone gelée (preuves du bench en pause, `factory/bin/frozen_zone_guard.py`, test
`tests/test_factoryctl_tombstone_wiring.py::ZoneGeleeIntacte`). La décision DC-003 du ledger est passée de
DELETE à **DEMOTE** : ne jamais supprimer ni modifier un fichier sous `campaigns/RUN8`. Régénérer
`factory_v2/receipts/CONVERGENCE_RESHAPE_RECEIPT.json` contre le ledger courant (sha256 recalculé) avec les
décisions réellement appliquées (2 : MilestoneController_V1 REPLACE + …) ; le juge et la suite sûre doivent être verts.
