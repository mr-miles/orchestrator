# Environment library

Reusable building blocks for sandbox environments (WORKFLOW.md §5.3a). The `environment` agent
composes a change's sandbox profiles from these by name; it may propose new entries in its spec,
which apply to that change once a human approves the spec (H-E1). Promote a useful proposal into
this library so later changes reuse it:

```bash
legion-orch -c orchestrator.yaml library list
legion-orch -c orchestrator.yaml library promote <change-id> tool ruff
legion-orch -c orchestrator.yaml library promote <change-id> policy internal-pypi
```

Promotion copies the entry from the approved spec version and never overwrites an existing
entry. Review promoted files like code: every change can then use them. The controller and
`legion-orch check` validate the whole library at start-up.

## `tools/<name>.yaml` – installed with mise (or apt)

```yaml
description: CPython (python, python3, pip)      # shown to the environment agent
mise: {python: "3.12"}                            # mise tool id -> default version
apt: [libpq-dev]                                  # optional Debian packages mise cannot provide
binaries: ["bin/python*", "bin/pip*"]             # the executables that use the network
network: []                                       # policies the tool needs (uv: [pypi])
env: {PIP_CACHE_DIR: /tmp/pip-cache}              # optional image ENV for sandboxes
```

* `mise` takes any tool mise can install: core tools (`python`, `node`, `go`, `java`, `rust`),
  and backends such as `npm:<pkg>`, `pipx:<pkg>`, `cargo:<crate>`, `aqua:<owner/repo>`,
  `ubi:<owner/repo>`. A spec may pin a single-tool entry with `name@version`. A tool from a
  system package has `apt` and no `mise` (see `git.yaml`, `curl.yaml`).
* Tools are installed at image build time into `/usr/local/share/mise` (read-only in every
  sandbox) and are on `PATH` through mise's shims; mise's cache and state are under `/tmp`.
* `binaries` is **the one place a tool's executable paths are written.** Paths are relative to
  the tool's mise install directory, `/usr/local/share/mise/installs/<tool id>/<any version>/`
  (`npm:prettier` installs to `npm-prettier`), or absolute for system packages; globs allowed.
  Relative paths need exactly one mise tool in the entry.
* `network` names the policies the tool needs to work. **A profile that selects the tool gets
  them**, open to this tool's binaries only, so choosing `uv` is all it takes for `uv sync`. This
  is the one place a tool is linked to its network. `python` deliberately brings none: packages
  come through uv, so pip cannot reach PyPI. Opt-in access (e.g. `github-read` for `git`) is
  added per profile in the spec: `"network": {"git": ["github-read"]}`.
* `env` is set in the image (`ENV`, after the tools are installed, so it affects sandboxes, not
  the build). Use it to keep a tool offline and predictable – the `uv` entry sets
  `UV_PYTHON_DOWNLOADS=never` and `UV_PYTHON_PREFERENCE=only-system`, so uv uses the profile's
  mise Python instead of trying to download one, and caches go to `/tmp`. Names may not be
  `PATH`, `MISE_*` or look like credentials (every agent can read the image environment); two
  tools in one profile may not set the same name to different values.

## `policies/<name>.yaml` – network access

```yaml
description: Python Package Index, read-only (pip, uv)
endpoints:
  - {host: pypi.org, port: 443, access: read-only}          # optional: path, protocol, enforcement
  - {host: files.pythonhosted.org, port: 443, access: read-only}
```

A policy is only endpoints; tools say who uses it. As attached to one profile it becomes a
credentialless OpenShell provider profile (`legion-net-<name>-<hash>`) whose `binaries` are those
of the profile's tools that brought it (or that the profile added it for). Endpoints are inspected as
`protocol: rest`, `enforcement: enforce`, `access: read-only` unless the entry says otherwise.
The provider is created in `sandbox.workspace` and attached to the sandboxes of every role in
that profile; OpenShell adds its endpoints to the sandbox's effective policy. Provider profiles
are content-addressed: editing a policy or a tool's binaries creates a new one, and sandboxes
that already run keep the one they were created with.

Policies cannot carry credentials and may not target the controller host or loopback (G-E2).
Name the narrowest hosts and paths that work. The library is checked as a whole at start-up and
by `legion-orch check`: every tool's `network` must name existing policies, and relative binaries
must resolve.

House conventions – which tools a project of each language should use, and how commands are
written – live in the `language-preferences` skill (`assets/skills/`), shared by every role.
