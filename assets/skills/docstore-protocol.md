## Skill: Versioned document store

The document store is immutable and versioned. Every write creates a new version; nothing is
ever overwritten or deleted.

- `docs_read(path)` returns the latest version with its `version` and `attestation`.
- To create a new document: `docs_write(path, content)` (no `base_version`).
- To revise a document: `docs_write(path, content, base_version=<the version you read>)`.
  If someone wrote in between, the write is refused – re-read, merge, retry.
- Writes outside your write scope are refused. `control/**` belongs to the controller.
- `docs_write` returns the attestation `file:<path>@<version>`; put it in your report.
- Prefer markdown for prose and JSON for anything the controller parses.
