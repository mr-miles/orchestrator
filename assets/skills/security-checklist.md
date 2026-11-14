## Skill: Security checklist (OWASP Top 10 + STRIDE)

For the diff under review, check: broken access control, cryptographic failures, injection
(SQL/command/template/path), insecure design, misconfiguration, vulnerable dependencies,
authentication failures, integrity failures (deserialisation, unsigned updates), logging and
monitoring gaps, SSRF. Then walk STRIDE over each new entry point: Spoofing, Tampering,
Repudiation, Information disclosure, Denial of service, Elevation of privilege. Scan the diff for
secrets. Every security finding needs an exploit sketch in `impact`.
