# factory_v3 — noyau V0 (un jalon, un espace, un moteur borné, le juge gelé, un reçu)

Contrat : `INTERFACE.md` ; autorité : `.context/NOYAU_MINIMAL_CROISEMENT_2026-09-24/SPEC_V0_GELEE.md` (v3).

V3 est le candidat minimal destiné à remplacer V2 : elle n'importe aucun module `factory_v2`. `run.py` + `support.py`
sont son unique noyau. Tant que la porte de `EVOLUTION.md` n'est pas verte, V2 reste gelée comme repli ; elle sera ensuite
supprimée, jamais reliée par un adaptateur.

## Commande
    python3 factory_v3/run.py --project <dir avec ROADMAP.yaml> --milestone <id> [--repo <dépôt produit>]
        [--engine glm|claude|codex] [--budget-s N] [--test-only] [--keep] [--runs-dir D]
    python3 factory_v3/run.py --hash-contrat --project P --milestone M   # hash à recopier dans valide.hash_contrat
Identité machine : `FACTORY_V3_MACHINE_ID` ou `~/.factory_v3/machine_id` (sinon rc 3).

## Codes retour
0 VERT (tests du juge verts, contrat intouché) · 1 ROUGE · 2 ABORT (contrat touché/contesté, juge incohérent, profil
non chargé, exception) · 3 REFUS avant dépense (verrou, doublon, quota ≥ 90 %, disque, jalon non déclarable, consigne vide).

## Chemins (dans le dépôt produit, exclus de git via .git/info/exclude)
- `.factory_v3/runs/<run_uuid>/` : RECEIPT.json (RUNNING dès l'admission), EVENTS.jsonl, CONSIGNE.md, engine.log (≤ 20 Mo),
  CONTRAT.json (sha256 = hash_contrat), verdict_<phase>.json (verdicts lus dans le tube, sous RUN), diff.patch, figes/ ;
  engine/home, engine/tmp et t_* purgés en fin de run sauf --keep (aucun JUnit gardé : forgeable)
- `.factory_v3/spaces/<jalon>-<sha8>-e<essai>-<machine>-<run8>/` : espace jetable (gardé si --keep ; un crash avant
  finalisation peut en laisser un)
- `.factory_v3/index.jsonl` : une ligne par reçu final, partagée par tous les worktrees du dépôt ;
  `<git-common-dir>/factory_v3/locks/<jalon>.lock` : verrou
- Publication d'un VERT : branche `v3/<jalon>-<sha8>-e<essai>-<machine>-<run8>` du dépôt produit (jamais main)

## Bac à sable
`bornage.sb` (Seatbelt) pour le moteur ET les tests : refus par défaut ; paramètres ESPACE RUN TMP HOME NET CONTROL
USERHOME (+ MAGASIN, phase moteur, profil à `auth.magasin_cli` seul) ; réseau ouvert pour le moteur (NET=1), coupé pour les tests (NET=0) ; le dépôt de contrôle n'est lisible que
pendant le jugement (NET=0), et un dépôt produit rangé dedans (hors .context) est refusé. Profil non chargé = ABORT, jamais de repli.

## Ajouter un moteur (1 fichier, 0 ligne de noyau)
1. Écrire `moteurs/<id>.json` sur le modèle de `glm.json` : `cli`, `argv` (gabarits `{model}` `{prompt}` `{prompt_path}`
   `{max_turns}` `{budget_usd}` `{home}`), `defauts`, `format_sortie`, `usage` (type d'événement JSON, cumul, champs →
   chemins), `plafonds`, `arret` (signal_1, grace_s, signal_2 = SIGKILL), `auth.env` (ou `auth.magasin_cli`, magasin du CLI exposé par lien — Codex), `env_allowlist`, `env_fixe`.
   `actif: false` → REFUS avant dépense ; Codex est livré inactif (rafraîchissement du jeton non mesuré, INTERFACE §10).
2. Ajouter `"<id>": "<version exacte>"` à `ENGINES.lock` (version ≠ lock → rc 3) ; `--engine <id>` le prend seul.
3. Rejouer le banc, puis quelques essais réels sur un jalon connu avant la production (procédure humaine : rien ne l'impose).
La lecture de l'usage est déclarative (bloc `usage`) : aucun code à ajouter.

## Banc
`FACTORY_NO_REAL_ENGINE=1` + `FACTORY_V3_FAKE_ENGINE=<nom>` (tests/v3/fakes).
Fumée : tests/v3_smoke retiré v9 (rouge depuis F1, doublon de test_v3_moteurs).
