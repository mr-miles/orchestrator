## Skill: Finding format

Report findings in `data.findings`, and mirror them in your markdown document:

```json
{"severity": "BLOCKER|WARNING|SUGGESTION", "confidence": "HIGH|MEDIUM|LOW",
 "dimension": "correctness|security|clarity|tests|performance|scope",
 "title": "short, specific", "observation": "what you saw", "impact": "why it matters",
 "fix": "concrete suggested change",
 "attestations": ["git:app/src/api/users.py@<sha>"]}
```

- HIGH ≥ 80 % confident, MEDIUM 50–79 %, LOW < 50 %. Only HIGH findings drive rework.
  Do not report anything below 50 % confidence.
- No vague findings ("looks good", grades). Each finding is observation → impact → fix.
- `data.verdict` is `PASS` only if there is no HIGH BLOCKER/WARNING. `data.checked` lists what you
  examined (required for a first-cycle PASS).
- Review the **output** (the diff at the candidate sha), not the plan.
