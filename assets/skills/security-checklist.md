---
description: "OWASP Top 10 and STRIDE checklist for reviewing a diff. Load when you review code for security."
---

## Skill: Security checklist (OWASP Top 10 + STRIDE)

For the diff under review, check: broken access control, cryptographic failures, injection
(SQL/command/template/path), insecure design, misconfiguration, vulnerable dependencies,
authentication failures, integrity failures (deserialisation, unsigned updates), logging and
monitoring gaps, SSRF. Then walk STRIDE over each new entry point: Spoofing, Tampering,
Repudiation, Information disclosure, Denial of service, Elevation of privilege. Scan the diff for
secrets. Every security finding needs an exploit sketch in `impact`.

For a web UI, also check that it still runs under `script-src 'self'` (no inline script or `on*=`
handlers, no library that injects one), that untrusted text is never rendered as HTML
(`dangerouslySetInnerHTML`, `innerHTML`, `v-html`), and that tokens are not kept in `localStorage`.
