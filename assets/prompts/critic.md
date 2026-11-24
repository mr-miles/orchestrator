# Assignment: critique the plan of change {{change_id}} (attempt {{attempt}})

The plan under review – the design's outputs – and what it is based on: {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}` – verify assumptions against it.

Run the pre-mortem skill against the plan format and the workflow skills. Write
`tickets/{{ticket_id}}/critique.md` (on a later attempt it exists: `docs_read` it and pass its
version as `base_version`; your predecessor's critique is its earlier version). Report with
`data.verdict` (`PASS`/`REWORK`) and `data.findings`, `outputs: {"critique": …}`, attesting the
**latest** version of every design output (`spec`, each delta spec, `oracle`, `environment`,
`tickets`) – G-C1 – and every attestation your findings cite. Check with
`report(..., dry_run=true)` before reporting.

{{scaffold}}

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).
