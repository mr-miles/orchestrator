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
└── grouping change              (the root ticket)
    └── <type> <ticket id>       (e.g. grouping delivery)
        ├── <type> <ticket id>   (e.g. agent:implementer T1, controller:verify verify:T1)
        └── agent <role>         (an agent run of the ticket)
```

One span per ticket, under its parent ticket's span (the root ticket's under the change span);
an agent run's span is under its ticket's. A ticket's span opens when it leaves `pending` and
ends when it is closed: `passed`, `failed` (a replan replaced it) or `cancelled`. A ticket that is
dropped before it ever started has no span; a passed criterion that runs again (its owner was reworked) gets a new span. One trace per change; the trace id is derived from
the change id, so a restart continues the same trace.

## 4. Attributes

Only these keys are recorded (strings are truncated to 128 chars).

| key | on | value |
|---|---|---|
| `legion.change.id` | every span | change id |
| `legion.workspace` | change | workspace name |
| `legion.ticket.id`, `legion.ticket.type` | ticket, agent run (id) | ticket id, ticket type |
| `legion.attempt` | ticket, agent run, events | the ticket's attempt (its round) |
| `legion.status`, `legion.waiting` | events | the ticket's new status; why it waits on a human |
| `legion.outcome` | change, ticket, agent run | change: `completed`, `abandoned`, `interrupted`; ticket: `passed`, `failed`, `cancelled`, `abandoned`, `interrupted`; agent run: its run status, lower case, or `interrupted` |
| `legion.run.id`, `legion.run.role` | agent run | run id, role |
| `legion.resumed` | spans opened after a restart | `true` |

Resource attributes: `service.name` and `legion.project` (the configured project name).

## 5. Events and status

Every status change of a ticket while its span is open is an event named after the new status
(`working`, `subtasks`, `checking`, `merging`, `paused`, `waiting`, `rejected`, `pending`), with
`legion.status`, `legion.attempt` and – `waiting` – `legion.waiting` (e.g. `blocked`,
`system_error`). A rework is the attempt going up; a criterion's next round is in the same span.

Status `ERROR` (the description is an identifier only) is set on: a change ended `abandoned`; a
ticket still waiting on a human when the change is abandoned (`failed`); an agent run that failed.
Everything else stays unset.

## 6. Endings

When the change ends, what is still open closes: tickets waiting on a human `failed` (if the
change was abandoned), the others `abandoned`; the root `completed` or `abandoned`.

## 7. Restarts

* The trace id is deterministic, so a restarted process continues the same trace.
* Spans opened after a restart for tickets under way carry `legion.resumed=true`, plus a new
  `change <id>` root. Tickets closed before it get no span again.
* On a graceful stop (SIGINT/SIGTERM) open spans are closed with outcome `interrupted`.
* A killed process exports no open span (an unended span is never sent).
* Agent runs that existed before a restart are not traced.
* Runs after the change ended (the retrospective) are children of the ended root in the same
  process; after a restart they are parentless spans of the same trace.

## 8. What is never recorded, and failure isolation

Never recorded: the goal, prompts, documents, summaries, escalation reason/question text, ticket goals, findings, tokens, sandbox names, session ids, commit shas.

Export is batched and asynchronous. Errors (unreachable or slow collector) are only logged and spans are dropped; a full queue drops spans. The engine is never affected.
