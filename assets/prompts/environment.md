# Assignment: specify the sandbox environment for change {{change_id}} (tier {{tier}})

Goal: {{goal}}

Inputs (read all of them first; the plan's commands are what the sandboxes must be able to run):
{{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}`.

Write `plan/environment.json` following the sandbox environment skill: named profiles composed
from the library below, the role → profile map, any proposed library entries, and a rationale
that ties every tool, network policy and non-default resource to a command or file it serves.
After approval the controller builds the images and network providers and attaches them; agents
and verifiers then run only with what you declared.

The library:

{{environment_library}}

Report `DONE` attesting `plan/environment.json` and the `git:` files your choices rest on (the
files that pin tool versions, the files that call the tools).

Write scope: {{write_scope}}. Required documents: {{required_docs}}.

Critique / human feedback to address (cycle {{cycle}}):
{{feedback}}
