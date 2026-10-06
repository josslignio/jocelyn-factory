# Amendement propose v4 — TR_UNIVERSE_VERIFIER_V1 + TR_UNIVERSE_EXPANSION_V1 (2026-09-12)

Corrige les 2 findings P1 restants (ACCEPT_WITH_FIXES) de `CODEX_COUNTER_AUDIT_TR_UNIVERSE_3.md`.
Rounds 1-2 restent acquis (source binding git show, SAMPLED par defaut, Frenchie_ exclu, biais de
graphe, gouvernance, livrable unique sous product_repo, aucun PYTHONPATH).

**Ce qui change vs v3** :
1. Suppression totale de l'exception MONITORED : tout compte AJOUTE doit avoir
   `lifecycle: SAMPLED` ; le verificateur rejette rc=1 (`NEW_ACCOUNT_MUST_BE_SAMPLED`) tout
   nouveau `MONITORED`, sans lire de justification ni chercher de fichier de preuve.
2. Le verificateur embarque EN DUR un snapshot fige des 14 comptes actuels (derive en lecture
   seule de `research/tr_kol_universe.json`) avec les six champs `handle`, `lifecycle`, `root`,
   `never_auto_prune`, `has_corpus`, `reason` ; toute divergence sur un seul champ d'un des 14 →
   rc=1 `EXISTING_ACCOUNT_FIELD_DIVERGED`.

**Ce qui ne change pas** : cardinalite 25-30, biais de graphe documente, 50 candidats sources,
gouvernance owner (OWNER_DIRECTIVE:18-20).

**Contre-audit Codex a relancer** :
```
codex exec --sandbox read-only "Contre-audit v4 de .context/factory-tightening-audit-2026-09-12/authority_change/{ROADMAP_tr_universe_expansion_v4.patch,TR_UNIVERSE_EXPANSION_V1.task_v4.md,VERIFIER_tr_universe_expansion.spec_v4.md} : l'exception MONITORED est-elle bien supprimee sans reste, et le snapshot fige des 14 comptes couvre-t-il tous les champs pertinents avec rejet rc=1 sur toute divergence ?"
```
