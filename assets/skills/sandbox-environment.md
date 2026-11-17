## Skill: Sandbox environment spec

When your assignment asks for it, `plan/environment.json` declares the sandboxes that run or work
with the code; it is part of the plan, so the commands you plan decide what it needs. It is
critiqued with the plan, approved by a human (gate H-E1), then built by the controller: one
container image per profile (the base agent or verifier image plus your tools, installed with
**mise**), the network policies as rules of each sandbox's policy, and the resources (`--cpu`,
`--memory`; disk as driver config). On a rework, leave it as it is (attest its latest version)
unless the plan's needs changed.

```json
{
  "summary": "Python 3.12 service tested with pytest via uv; the UI lint needs node 22",
  "profiles": {
    "dev":   {"tools": ["python@3.12", "uv", "ruff", "node@22"],
              "resources": {"memory_mib": 8192, "vcpus": 4}},
    "check": {"tools": ["python@3.12", "uv", "ruff", "node@22"],
              "network": {"uv": ["internal-pypi"]}},
    "lint":  {"tools": ["ruff"]}
  },
  "roles": {"implementer": "dev", "oracle": "dev",
            "verifier": "check", "reviewer": "check", "baseline": "check", "integration": "check",
            "security": "lint", "architecture": "lint"},
  "proposed": {
    "policies": {"internal-pypi": {"description": "The company package index (read-only)",
                                   "endpoints": [{"host": "pypi.internal.example.com", "port": 443}]}}
  },
  "rationale": "pyproject.toml requires-python 3.12 and uses uv (uv.lock) and ruff ([tool.ruff]); T2's verification runs `npm run lint` (package.json). uv brings pypi and node brings npm-registry for adding and syncing dependencies. uv.lock pulls two packages from pypi.internal.example.com, which the library has no policy for, hence the proposed internal-pypi for uv in the profiles that run tests. Security and architecture reviewers only read and lint: ruff, no network. 8 GiB: the test suite builds a 3 GiB fixture database."
}
```

### Profiles

* `tools` – library tool names, optionally `name@version` to pin a single-tool entry to the
  version the project uses (`python@3.13`). Every command in the plan must find its tools here.
  Tools are installed with mise under `/usr/local/share/mise` and are on `PATH` (shims).
* **Network comes with the tools.** A library tool declares the network policies it needs to
  work (`uv` brings `pypi`, `node` brings `npm-registry`; the catalogue says "brings network").
  A profile gets them automatically, and each policy allows only the binaries of the tools that
  brought it. Do not restate them. The controller's own MCP and git endpoints are always
  available. A profile whose tools bring no network has none beyond the controller.
* `network` – only for what the tools do not already bring: extra policies per tool,
  `{"uv": ["internal-pypi"]}` (the tool's binaries may then use them too). Opt-in policies such
  as `github-read` are attached this way (`{"git": ["github-read"]}`).
* `offline: true` – drop the network the tools would bring (extras in `network` still apply);
  for profiles that only read or lint.
* `resources` – optional `memory_mib`, `vcpus`, `disk_mib`. Leave them out for the driver
  defaults; request more only with a reason in `rationale`. Configured caps apply.

### Roles

Map each role that runs or reads code to a profile: `implementer`, `oracle`, `baseline`,
`reviewer`, `security`, `architecture`, `integration`, `release`, and `verifier` (the
deterministic sandbox that runs every verification command). Unmapped roles get the default
sandbox. `intake`, `planner` and `critic` run before the environment exists and
cannot be mapped. Give judges and the verifier the same tools as the implementer when they run
the same commands (e.g. `uv run pytest` installs the locked dependencies, so it needs what uv
brings); give judges that only read or lint a smaller or `offline` profile.

### Proposing library entries

**Select from the library first; propose only what is missing.** A library tool already brings
its binaries, settings and network – choosing `uv` is all it takes to use uv with PyPI. Propose
an entry only when the library has no tool or policy that does the job, never a variant of an
existing one (names must not clash with the library). Each proposal is reviewed at H-E1 and may
be promoted into the library for every change, so keep it minimal and general.

* tool: `{"description": "...", "mise": {"<mise tool id>": "<version>"}, "apt": ["<debian
  package>"], "binaries": ["bin/<bin>"], "network": ["<policy>"], "env": {"<NAME>": "<value>"}}`
  – any tool mise can install (`python`, `node`, `go`, `npm:<pkg>`, `pipx:<pkg>`,
  `cargo:<crate>`, `aqua:<owner/repo>`, `ubi:<owner/repo>`); `apt` only for system libraries mise
  cannot provide. `binaries` are the executables that make network requests – relative to the
  tool's mise install directory (`<data>/installs/<tool>/<version>/`, e.g. `bin/*`) or
  absolute for system packages; globs allowed; the only place a tool's paths are written.
  `network` lists the policies the tool needs to work. `env` sets variables in the image so the
  tool behaves offline or reproducibly (the library's `uv` sets `UV_PYTHON_DOWNLOADS=never`).
  Never a credential, `PATH` or `MISE_*`.
* policy: `{"description": "...", "endpoints": [{"host": "pypi.internal.example.com", "port": 443,
  "path": "/simple/**", "access": "read-only"}]}` – HTTPS (`protocol: rest`, inspected) endpoints
  only; `access` is `read-only` (GET/HEAD) unless the tool must write. Attach it through a tool's
  `network` (a proposed tool) or a profile's `network` (a library tool). Name the narrowest
  hosts and paths that work. Never the controller host, `host.openshell.internal` or loopback.
