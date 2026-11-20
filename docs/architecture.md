# Architecture: layers and encapsulation

How the controller is put together: which layer decides what, which objects each layer is made
of, and what each object may know about the others. The workflow itself – stages, gates, guards
– is specified in [WORKFLOW.md](../WORKFLOW.md); the plan behind this structure is
"stages and steps" (WORKFLOW.md §4.1).

## 1. The layers

Each layer uses only the layers below it. The **workflow core** at the top of the engine is pure
(no I/O); everything that touches a sandbox, git or the store is below it.

```mermaid
flowchart TB
    subgraph entry["Entry points"]
        web["Web API + UI<br/>server/web.py, views.py"]
        mcp["MCP server (agents)<br/>server/mcp.py"]
        cli["CLI<br/>legion-orch"]
    end

    subgraph engine["ChangeEngine – one change (engine/change.py)"]
        direction TB
        subgraph wf["Workflow: the stage rules"]
            core["Workflow core – pure<br/>flow/core.py, states.py<br/><i>stages, gates, send-back, rounds</i>"]
            tree["Stage tree per tier<br/>workflow_tree.py"]
            rt["FlowRuntime<br/>runtime.py<br/><i>events in, effects out, persistence</i>"]
        end
        subgraph steps["Steps: what each kind of node is made of"]
            runner["StepRunner<br/><i>moves a node through its phases</i>"]
            types["STEP_TYPES<br/>steps.py<br/><i>the only per-kind table</i>"]
        end
        subgraph layers["The three layers of a step"]
            agent["Agent layer<br/>roles/*.py – RoleHandler<br/><i>prompt, guards, judging a report</i>"]
            phases["Orchestrator layer<br/>phases.py – AgentRun, Verify,<br/>Panel, Merge, BuildImages, Evaluate"]
            sbx["Sandbox layer<br/>sandbox_plan.py – SandboxPlan<br/>sandboxes.py – Sandboxes"]
        end
        subgraph svc["Services the layers call"]
            disp["Dispatcher<br/><i>a run: assignment, payload, launch</i>"]
            reports["Reports<br/><i>agent writes and reports, shared guards</i>"]
            acc["Acceptance<br/><i>verify / panel / merge results</i>"]
            prov["Provisioner<br/><i>environment images</i>"]
            life["RunLifecycle<br/><i>failure, timeouts, logs, retiring</i>"]
            gates["HumanGates<br/><i>what approving each gate decides</i>"]
            sched["Scheduler<br/><i>the tick</i>"]
        end
        state["ChangeCore + StateStore<br/><i>change, nodes, runs, events</i>"]
    end

    subgraph gov["Rules read by everyone (ChangeView only)"]
        governance["governance/*<br/>approvals, attestations, overrides"]
        ship["ship.py<br/>the ship gates"]
        guards["guards/*"]
    end

    subgraph infra["Infrastructure"]
        runnerx["Runner<br/>OpenShell / local"]
        git["GitRemote"]
        store["DocStore<br/><i>the only durable state</i>"]
        builder["EnvironmentBuilder<br/>podman"]
    end

    web --> engine
    cli --> web
    mcp --> reports
    rt <--> core
    rt --> tree
    rt --> runner
    sched --> runner
    gates --> rt
    runner --> types
    types --> agent & phases & sbx
    phases --> disp & acc & prov
    agent --> reports
    disp --> sbx
    acc --> sbx
    life --> sbx
    engine --> gov
    sbx --> runnerx
    disp & acc & gates --> git
    state --> store
    prov --> builder
```

| Layer | Decides | Knows nothing about |
| --- | --- | --- |
| **Workflow core** (`flow/`) | Between steps: what starts when, gates (approval + preconditions), send-backs and their rework policies, fix rounds, the end | Agents, sandboxes, git, the store, node kinds |
| **FlowRuntime** | How the core's effects become node changes; what the nodes' changes mean to the core; persistence by replay | What a step does inside |
| **Steps** (`steps.py`) | What each kind of node is made of | How a phase does its work |
| **Phases** | Their own work and its result: ok, rework or escalate | Which kind of node they serve (policies are passed in) |
| **Agent layer** (role handlers) | A role's guards, context, prompt variables, what its report records | Sandboxes, the next phase |
| **Sandbox layer** | Creating, preparing, pausing, holding and retiring sandboxes; verifiers | Why a sandbox is needed |
| **Infrastructure** | Running, storing, versioning | The workflow |

## 2. Who decides what: a build task, end to end

Between steps the core decides; within a step the engine does. They meet in two places: the
core's **effects** (`StartStep`, `StopStep`, `Evaluate`, `EndChange`, …), which `FlowRuntime`
carries out, and the engine's **saves**, on which `FlowRuntime.sync` tells the core what the
nodes did (accepted → `Completed`, escalated → `Escalated`, resolved → resumed).

```mermaid
sequenceDiagram
    autonumber
    participant Core as Workflow core
    participant RT as FlowRuntime
    participant Sch as Scheduler
    participant SR as StepRunner
    participant Ph as Phases
    participant Ag as Agent (role handler)
    participant Sb as Sandboxes

    Core->>RT: StartStep(build:T1, attempt)
    RT->>RT: node PENDING (a new cycle if it ran before)
    Sch->>SR: promote(node) – the core started it
    SR->>Ph: AgentRun.enter → READY
    Sch->>SR: dispatch(node)
    SR->>Ph: AgentRun.dispatch (waits, G-H1)
    Ph->>Sb: create, prepare (clone, branch, setup), start agent
    Ag-->>Ph: report accepted → step_done(node)
    Ph->>Ph: Verify.enter → VERIFYING
    Ph->>Sb: verifier sandbox: setup, commands
    Ph->>Ph: Verify.conclude: ok → Panel.enter → REVIEWING
    Note over Ph,Ag: judges' runs, each in a fresh sandbox
    Ph->>Ph: Panel.decide: ok → Merge.enter → MERGING
    Ph->>Ph: Merge.run (G-A1, G-H1, G-A2) → ACCEPTED
    Ph->>Sb: retire the implementer's sandbox (reflect, delete)
    RT->>Core: on save: Completed(build:T1)
    Core-->>RT: StartStep(next) … or Evaluate(H-3)
```

A rework (`Verify` fails, the panel finds a blocker) stays inside the step: the engine runs the
agent again in the same branch and sandbox, and the core only sees the step still running. An
escalation is the only within-step event the core is told about, because a human may answer it
by sending a stage back.

## 3. A step is a composition

A step type is a declaration, not code: an agent (the role of its first `AgentRun`), a sandbox
plan, an ordered list of phases configured by **policy objects**, the waits it respects and what
a reset undoes. Adding a kind of node means adding a row to `STEP_TYPES`.

```mermaid
classDiagram
    direction LR
    class StepType {
        phases: Phase[]
        sandbox: SandboxPlan
        promote_when: Wait[]
        dispatch_when: Wait[]
        on_reset: Reset[]
        rework_resume
        runs_after_end
        role()
        judges(change)
        current(node) Phase
        after(phase) Phase
    }
    class StepRunner {
        promote(node)
        dispatch(node)
        advance(node, phase)
        step_done(node)
        resume(node)
    }
    class Phase {
        statuses
        enter(runner, node, after)
        dispatch(runner, node)
        resume(runner, node)
    }
    class AgentRun {
        role
    }
    class Verify {
        commands: CommandSource
        expect_failures
        at_head
        on_rework: EscalateFailures
        on_setup_failed: SetupFailurePolicy
        notes: Notes
        judge(result) PhaseResult
        conclude(...)
    }
    class Panel {
        judges: JudgeSource
        decides
        decide(node) PhaseResult
    }
    class Merge {
        judges: JudgeSource
        run(node) PhaseResult
    }
    class BuildImages
    class Approval
    class SandboxPlan {
        checkout: Checkout
        keep: Keep
        pushes()
        rebase
        held(node)
    }
    class PhaseResult {
        <<Ok, Rework or Escalate>>
    }
    class Wait {
        holds(scheduler, node)
    }
    class Reset {
        applies(node)
        undo(node)
    }

    StepRunner --> StepType : asks
    StepType *-- Phase
    StepType *-- SandboxPlan
    StepType *-- Wait
    StepType *-- Reset
    Phase <|-- AgentRun
    Phase <|-- Verify
    Phase <|-- Panel
    Phase <|-- Merge
    Phase <|-- BuildImages
    Phase <|-- Approval
    Verify ..> PhaseResult
    Panel ..> PhaseResult
    Merge ..> PhaseResult
```

The table (`engine/steps.py`) – each kind is a list of phases:

| Kind | Sandbox plan | Phases |
| --- | --- | --- |
| intake | base, kept until H-1 | AgentRun |
| plan | base, kept until H-E1 | AgentRun → Panel(critic, decided by its handler) |
| provision | – | BuildImages (waits for a quiet environment) |
| oracle | branch (re-based each run), kept until H-2 | AgentRun (merges itself) |
| preflight | – | Verify(acceptance commands, expect failures, at head, a failed setup → oracle rework) |
| baseline | head | AgentRun → Verify(baseline commands, expect failures, G-BL2 notes) |
| build | branch, kept until accepted | AgentRun → Verify(task commands) → Panel(tier) → Merge |
| integration | head | AgentRun → Verify(oracle commands, a failure escalates) |
| release | head | AgentRun |
| retrospective | head | AgentRun (after the change ends, after the last reflections) |
| human gate | – | Approval |

Judges (reviewers, the critic) are not node kinds: they are the judges of a `Panel`, and get the
`JUDGE` sandbox plan – fresh, at the commit they judge (G-D4).

## 4. The workflow core

The change's structure, built once H-1 fixes the tier (R3 shown; lower tiers leave stages out
and approve H-3 automatically):

```mermaid
flowchart LR
    subgraph REQ["Requirements"]
        intake --> H1{{"◆ H-1"}}
    end
    subgraph PLAN["Plan"]
        subgraph DES["Design"]
            plan["plan (planner ⇄ critic)"] --> HE1{{"◆ H-E1 approve the plan"}}
        end
        subgraph ORC["Oracle"]
            provision --> scaffold["[scaffold]"] --> oracle --> preflight --> baseline --> H2{{"◆ H-2 approve the oracle"}}
        end
        HE1 --> provision
        H2 --> PG{{"◆ plan-approved (automatic)"}}
    end
    subgraph DEL["Delivery"]
        tasks["tasks (DAG)"] --> integration --> release --> H3{{"◆ H-3 · preconditions: ship gates"}}
    end
    H1 --> plan
    PG --> tasks
```

**Send back** is offered on a waiting gate or an escalated step for every stage its work was
built on. Requirements, Plan, Design and Oracle **start over**; Delivery **appends a round** – a
Round stage (its own Design, Oracle and tasks, ids `@n`) inserted before integration.

Every node is one of three small state machines; each status change is a named transition that
declares its source statuses (`flow/states.py`):

```mermaid
stateDiagram-v2
    direction LR
    state "Step" as S {
        [*] --> pending
        pending --> running : start
        running --> running : rework
        running --> escalated : escalate
        escalated --> running : resume (answer, retry)
        escalated --> done : override
        running --> done : complete
        done --> pending : reset
        running --> pending : reset
        escalated --> pending : reset
    }
    state "Gate" as G {
        [*] --> g_pending
        g_pending --> waiting : open
        waiting --> waiting : approve / evaluating / assessed
        waiting --> g_done : complete
        g_done --> g_pending : reset
    }
```

## 5. Encapsulation rules

| Boundary | Rule | Checked by |
| --- | --- | --- |
| Workflow core | Pure: no I/O, no engine imports; state only through its machines' transitions | `tests/test_flow.py` (invariants after every event, hypothesis), `test_flow_states.py` |
| FlowRuntime ↔ core | The core's state is its inputs; replaying them rebuilds it exactly | `tests/test_runtime.py` |
| Engine collaborators | Each gets only the parts it uses; imports form a DAG; nothing imports the `ChangeEngine` facade | `test_units.py::test_engine_collaborators_are_acyclic` |
| Role handlers | See the change only through `RoleContext`; finish their phase with `step_done()` | `test_units.py::test_role_handlers_use_only_the_role_context` |
| Governance, ship gates | Read the change only through `ChangeView` | `test_units.py::test_governance_and_ship_use_only_the_change_view` |
| Per-kind knowledge | Only `STEP_TYPES` (and gate approvals in `HumanGates`); no `node.kind` switches elsewhere | `tests/test_steps.py` (the declarations) |
| Sandboxes | One owner (`Sandboxes`): locks, creation, preparation, pause, hold, retirement | `tests/test_sandboxes.py` |
| UI | Renders the actions the core offers and the stages it lists; knows no node kinds for actions | `frontend/src/App.test.tsx` |

## 6. Testing each layer

| Layer | Tested with | Needs |
| --- | --- | --- |
| Workflow core | `ScriptedRuntime`: scripted step outcomes, invariants after every event, property tests | nothing |
| Stage tree | the core driven over each tier's tree (`test_workflow_tree.py`) | nothing |
| Phases, step types | scripted runner, builder and judge results (`test_phases.py`, `test_steps.py`) | fake runner |
| Sandboxes | the fake runner (`test_sandboxes.py`) | fake runner |
| FlowRuntime | the engine harness (`test_runtime.py`) | fake runner, real git and store |
| Whole workflow | the engine harness, scripted agents (`test_engine.py` …) and end to end with fake agents over MCP (`test_e2e.py`) | fake runner / local runner |
