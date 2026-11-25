## Skill: The workflow (tickets)

A change is a tree of **tickets**. A ticket is its own work (by its type: `agent:<role>`, `human:signoff`, `controller:<action>`; `grouping` has none), then its **subtask groups** in order (a group's tickets run in parallel), then its **criteria** in order, stopping at the first failure. A criterion is a leaf ticket whose outcome is a verdict; its `severity` says what a failure does: `rework` (the ticket's work runs again with the findings) or `escalate` (a human decides). A grouping ticket's criteria always escalate. Risk labels are inherited by subtasks.

Tickets are added only after a ticket's completed groups, never under a passed ticket; a failed ticket is never changed: new tickets replace it.

### The skeleton

- `change` (grouping)
  - `workspace` (controller:sync): the change's base: the workspace synced with its source
  - `requirements` (grouping)
    - `intake` (agent:intake): the requirements and the risk assessment
      - criterion `H-1` (human:signoff, signs requirements, risk)
  - `plan` (grouping)
    - `design` (agent:planner): the spec, the oracle, the environment and the tickets
      - criterion `critique` (agent:critic)
      - criterion `H-E1` (human:signoff, signs spec, specs, oracle, environment, tickets)
    - `provision` (controller:provision): the signed-off environment, built
    - `scaffold` (agent:scaffolder): the project's layout and tooling, as the plan specifies
      - criterion `verify:scaffold` (controller:verify)
      - criterion `review:scaffold` (agent:reviewer)
      - criterion `can-merge:scaffold` (controller:can-merge)
  - `delivery` (grouping)
    - `oracle` (agent:oracle; tiers R2, R3): the acceptance tests, failing before any build
      - criterion `preflight` (controller:preflight)
      - criterion `H-2` (human:signoff, signs tests)
    - `baseline` (controller:baseline; tiers R3): the baseline: what passes and fails before any build
      - criterion `H-2b` (human:signoff, signs baseline)
    - `builds` (grouping)
    - criterion `verify:delivery` (controller:verify)
    - criterion `integration` (agent:integration)
  - `evaluate` (grouping)
    - `describe` (agent:describer): the pull request's description
    - criterion `H-3` (human:signoff, signs head, pr)
  - `release` (grouping)
    - `publish` (controller:publish): the change, published

Criteria that can never be waived: `controller:can-merge`, `controller:preflight`, `controller:verify`, `human:signoff`.

### Tier R0

- the tree contains `agent:intake`, `agent:planner`, `controller:provision`, `agent:integration`, `controller:publish`
- every `agent:implementer` comes after a `controller:provision` (finished before it starts)
- every `agent:implementer` comes before a `agent:integration`
- every `agent:implementer` ticket has criteria `controller:verify`, `controller:can-merge`
- every `agent:scaffolder` ticket has criteria `controller:verify`, `controller:can-merge`
- `agent:implementer` tickets have 3 attempts
- `agent:planner` tickets have 3 attempts
- `agent:scaffolder` tickets have 3 attempts

### Tier R1

- the tree contains `agent:intake`, `agent:planner`, `controller:provision`, `agent:integration`, `controller:publish`
- every `agent:implementer` comes after a `controller:provision` (finished before it starts)
- every `agent:implementer` comes before a `agent:integration`
- every `agent:implementer` ticket has criteria `controller:verify`, `agent:reviewer`, `controller:can-merge`
- every `agent:scaffolder` ticket has criteria `controller:verify`, `agent:reviewer`, `controller:can-merge`
- tickets labelled `security` have criteria `agent:security`
- `agent:implementer` tickets have 3 attempts
- `agent:planner` tickets have 3 attempts
- `agent:scaffolder` tickets have 3 attempts

### Tier R2

- the tree contains `agent:intake`, `agent:planner`, `controller:provision`, `agent:integration`, `controller:publish`, `agent:oracle`
- every `agent:implementer` comes after a `controller:provision` (finished before it starts)
- every `agent:implementer` comes before a `agent:integration`
- every `agent:implementer` comes after a `agent:oracle` (finished before it starts)
- every `agent:implementer` ticket has criteria `controller:verify`, `agent:reviewer`, `controller:can-merge`
- every `agent:scaffolder` ticket has criteria `controller:verify`, `agent:reviewer`, `controller:can-merge`
- every `agent:oracle` ticket has criteria `controller:preflight`, `human:signoff`
- tickets labelled `security` have criteria `agent:security`
- `agent:implementer` tickets have 3 attempts
- `agent:planner` tickets have 3 attempts
- `agent:scaffolder` tickets have 3 attempts

### Tier R3

- the tree contains `agent:intake`, `agent:planner`, `controller:provision`, `agent:integration`, `controller:publish`, `agent:oracle`, `controller:baseline`
- every `agent:implementer` comes after a `controller:provision` (finished before it starts)
- every `agent:implementer` comes before a `agent:integration`
- every `agent:implementer` comes after a `agent:oracle` (finished before it starts)
- every `agent:implementer` comes after a `controller:baseline` (finished before it starts)
- every `agent:implementer` ticket has criteria `controller:verify`, `agent:reviewer`, `controller:can-merge`
- every `agent:scaffolder` ticket has criteria `controller:verify`, `agent:reviewer`, `controller:can-merge`
- every `agent:oracle` ticket has criteria `controller:preflight`, `human:signoff`
- every `controller:baseline` ticket has criteria `human:signoff`
- tickets labelled `security` have criteria `agent:security`
- `agent:implementer` tickets have 3 attempts
- `agent:planner` tickets have 3 attempts
- `agent:scaffolder` tickets have 3 attempts
