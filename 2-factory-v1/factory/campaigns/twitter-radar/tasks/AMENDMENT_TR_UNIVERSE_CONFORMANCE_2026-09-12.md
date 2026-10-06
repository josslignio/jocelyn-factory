# AMENDMENT_TR_UNIVERSE_CONFORMANCE_2026-09-12 — proprietaire

TR_UNIVERSE_VERIFIER_V1 est COMPLETE (commit produit `80989bbb`), son propre `pytest` est vert.
Mais son acceptance etait auto-referente : `product_e2e.test_command` EST le pytest ecrit par le
meme builder que le code qu'il teste, incapable par construction de prouver la conformite a la
spec v4 contre-auditee (4 rounds Codex). Resultat : le livrable diverge sur 6 points (schema,
snapshot des 14 comptes non fige en dur, provenance a plat au lieu de `source{file,commit,dated}`,
sous-chaine brute au lieu du parsing de `candidates`, `category`/`justification` ignores, mauvais
chemin/schema de recu) — et ces ecarts contredisent l'acceptance du jalon suivant
`TR_UNIVERSE_EXPANSION_V1`, qui s'appuie sur ce verificateur comme seul juge.

Amendement : ajout de `TR_UNIVERSE_VERIFIER_CONFORMANCE_V1`, qui exige une suite de test
INDEPENDANTE du builder d'origine avant que `TR_UNIVERSE_EXPANSION_V1` ne puisse dependre du
verificateur. `TR_UNIVERSE_EXPANSION_V1.depends_on` bascule de `TR_UNIVERSE_VERIFIER_V1` vers ce
nouveau jalon.

Lecon retenue : tout jalon dont le livrable EST un verificateur (verdict pour un jalon aval) doit
avoir, dans son acceptance meme, une preuve de conformite ecrite par une main independante du
builder — jamais seulement le pytest du builder lui-meme.
