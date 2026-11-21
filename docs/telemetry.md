# Telemetry (OpenTelemetry spans)

The orchestrator can export one trace per change as OpenTelemetry spans over OTLP/HTTP (protobuf). It is off unless configured.

## 1. Turning it on

Install the optional packages:

```
uv sync --extra telemetry
pip install 'legion-orchestrator[telemetry]'
```

Config section `telemetry:` (all keys optional):

| key | meaning |
|---|---|
| `enabled` | `true` forces on, `false` forces off, omitted = auto |
| `endpoint` | OTLP/HTTP base URL, e.g. `http://localhost:4318` (`/v1/traces` is appended unless already present; must start `http://` or `https://`) |
| `service_name` | resource `service.name`; default `OTEL_SERVICE_NAME` or `legion-orchestrator` |
| `timeout_s` | exporter timeout in seconds, default 5, at least 1 |

Environment variables: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` (credentials go here, never in the config file), `OTEL_SERVICE_NAME`, `OTEL_SDK_DISABLED`, `OTEL_TRACES_EXPORTER`.

Rules:

* **off**: `enabled: false`. Nothing is imported, no thread, no socket.
* **explicit**: `enabled: true` is on regardless of the environment.
* **auto** (`enabled` omitted): on only if `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is non-empty, `OTEL_SDK_DISABLED` is not `true` and `OTEL_TRACES_EXPORTER` is not `none`.
* Without `endpoint`, the SDK uses the OTEL endpoint variables or its default `http://localhost:4318/v1/traces`.
* **Missing packages**: explicit mode fails to start with an error naming the install command; auto mode logs a warning and runs without tracing.

## 2. Viewing in otel-front

Run otel-front, then either set

```yaml
telemetry: {enabled: true, endpoint: http://localhost:4318}
```

or `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`. Spans go over OTLP/HTTP protobuf to `/v1/traces`. Start `legion-orch serve`, start a change, and open otel-front. A span appears only once it has ended, so the `change <id>` root shows when the change ends (or at graceful shutdown).

## 3. The span hierarchy

```
change <id>
└── stage <title>
    └── group <id>
        └── step <node id>
            ├── phase <PhaseClass>
            └── agent <role>
```

Names: `change <id>`, `stage <title>` (e.g. `stage Design`, `stage Design (round 2)`), `group <id>`, `step <node id>` (e.g. `step build:T1`), `phase <PhaseClass>` (e.g. `phase Verify`), `agent <role>`. Groups are optional; a span's parent is its enclosing stage/group, else the change span. Phase and agent spans are children of their step. One trace per change; the trace id is derived from the change id, so a restart continues the same trace.

## 4. Attributes

Only these keys are recorded (strings are truncated to 128 chars).

| key | on | value |
|---|---|---|
| `legion.change.id` | every span | change id |
| `legion.workspace` | change | workspace name |
| `legion.stage.id`, `legion.stage.title` | stage | flow id / title |
| `legion.group.id` | group | flow id |
| `legion.node.id`, `legion.node.kind` | step, phase, agent run | node id, node kind |
| `legion.cycle` | step, phase, agent run | cycle number |
| `legion.phase` | phase | phase class name |
| `legion.result` | phase | `ok`, `rework`, `escalate`, `sent_back`, `failed`, `interrupted` |
| `legion.outcome` | change, stage, group, step, agent run | change: `completed`, `rejected`, `abandoned`, `interrupted`; stage/group/step: `accepted`, `rework`, `failed`, `abandoned`, `interrupted`; agent run: `reported`, `accepted`, `superseded`, `failed`, `interrupted` |
| `legion.run.id`, `legion.run.role` | agent run | run id, role |
| `legion.escalation.kind`, `legion.expected` | events | escalation kind; true when a gate's decision is due |
| `legion.resumed` | spans opened after a restart | `true` |

Resource attributes: `service.name` and `legion.project` (the configured project name).

## 5. Events and status

Step events: `awaiting_human` (a gate's decision is due) or `escalated`, both with `legion.escalation.kind` and `legion.expected`; `resumed` when the wait ends. A status change inside a phase adds an event `status <STATUS>` (e.g. `status RUNNING`) without attributes.

Status `ERROR` (description is an identifier only) is set on: a change with outcome `rejected` or `abandoned`; a step with outcome `failed`; a phase with result `failed`, or result `escalate` that is not expected; an agent run with outcome `failed`. Everything else stays unset.

## 6. Send-backs, rework, waits, and endings

A send-back closes the phase `sent_back`; rework closes spans with outcome `rework`, and the step reopens as a new span on rerun. A wait on a human closes the phase with `escalate` (ERROR only for an escalated step, not for a gate) and adds the `awaiting_human`/`escalated` event on the step.

A change ends by *reject at H-3* or *abandon at an escalated step*. The waiting step ends `failed` (ERROR), other open steps `abandoned`, and the root `rejected` or `abandoned` (ERROR). E.g. reject at H-3: `step H-3` failed, `phase Approval` result `escalate` (no ERROR), root `rejected`. A completed change closes the root `completed`.

## 7. Restarts

* The trace id is deterministic, so a restarted process continues the same trace.
* Spans opened after a restart for running/waiting work carry `legion.resumed=true`, plus a new `change <id>` root.
* On a graceful stop (SIGINT/SIGTERM) open spans are closed with outcome/result `interrupted`.
* A killed process exports no open span (an unended span is never sent).
* No link to the earlier process's spans is persisted.
* Agent runs that existed before a restart are not traced.
* The retrospective runs after the change ended: its spans are children of the ended root in the same process; after a restart they are parentless spans of the same trace with `legion.resumed=true`.

## 8. What is never recorded, and failure isolation

Never recorded: the goal, prompts, documents, summaries, escalation reason/question text, task titles, feedback, tokens, sandbox names, session ids, commit shas.

Export is batched and asynchronous. Errors (unreachable or slow collector) are only logged and spans are dropped; a full queue drops spans. The engine is never affected.
