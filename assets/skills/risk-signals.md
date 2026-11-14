## Skill: Risk signals

Write `intake/risk.json` as:

```json
{"signals": [{"name": "security", "score": 0, "rationale": "…", "source": "file:intake/requirements.md@1"}, …]}
```

All twelve names are required: security, authorization, data_migration, external_side_effect,
public_api, ui, performance, infrastructure, irreversible_action, scope_breadth,
novelty_uncertainty, verification_quality. Scores: 0 none, 1 low/internal, 2 material
(user data, permissions, persistent state, public API, user-facing, staging infra, multi-module),
3 critical (secrets/payments/regulated data, auth boundary, destructive migration, production
side effects, breaking public contract, release-channel UX, production SLO, no executable oracle).
Score honestly – the controller derives the risk tier and gates from these numbers and a human
reviews them. Under-scoring is a finding against you.
