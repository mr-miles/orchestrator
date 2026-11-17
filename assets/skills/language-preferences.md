## Skill: Language preferences

The house conventions for each language. They apply to every plan, task, test, review and
sandbox environment in a project using that language. When the repository already does
something differently, follow the repository and say so; never mix both ways in one project.

### Python

* **uv for everything, never pip or `python -m venv`.** Dependencies are declared in
  `pyproject.toml` and locked in `uv.lock` (commit both). Add with `uv add <pkg>` (dev tools:
  `uv add --dev <pkg>`), install with `uv sync --locked`, run with `uv run <cmd>`.
* **ruff for linting and formatting** (no black, isort, flake8 or pylint). Configure it in
  `pyproject.toml` under `[tool.ruff]`. Run the ruff binary directly: `ruff check .` and
  `ruff format --check .` (they need no dependencies or network); `ruff check --fix .` and
  `ruff format .` to fix.
* **Tests with pytest via uv:** `uv run pytest -q <paths>`.
* **Verification commands** (task `verification`, oracle commands) for Python tasks use exactly
  these forms: `uv run pytest -q …`, `ruff check …`, `ruff format --check …`. Every Python task
  that changes code verifies with ruff as well as its tests.
* **Sandbox environment:** a Python profile has `python@<version matching requires-python>`,
  `uv` and `ruff` from the library. `uv` brings read-only PyPI access (`uv run` installs the
  locked dependencies first, so every profile that runs tests needs `uv`); `python` and `ruff`
  bring none, so pip cannot install anything and a lint-only profile needs just `ruff`.

### TypeScript and web frontends

* **React, TypeScript, Vite, Tailwind CSS v4 and shadcn/ui.** TypeScript is `strict`. shadcn/ui
  components are generated with its CLI (`bunx --bun shadcn@latest add <name>`), committed and
  kept on its default theme, light and dark.
* **bun for everything, never npm, pnpm, yarn or npx.** Dependencies are locked in `bun.lock`
  (commit it). Add with `bun add <pkg>` (dev tools: `bun add -d <pkg>`), install with
  `bun install --frozen-lockfile`.
* **Scripts run on bun, not Node:** a bun sandbox has no `node`, so a package's CLI is run as
  `bun --bun <cli>` (e.g. `bun --bun vite build`, `bun --bun prettier --check .`).
* **oxlint for linting, Prettier (with `prettier-plugin-tailwindcss`) for formatting, `bun test`
  for tests** (happy-dom and Testing Library for components). Use the `package.json` scripts
  `typecheck` (`tsc -b`), `lint`, `format:check` and `test`.
* **Verification commands** for frontend tasks use exactly these forms:
  `bun install --frozen-lockfile`, `bun run typecheck`, `bun run lint`,
  `bun run format:check`, `bun test …`. Every task that changes frontend code verifies with
  all of them.
* **The page must run under a Content Security Policy of `script-src 'self'`:** no inline
  `<script>` or `on*=` attributes, and no library that injects one. Never render raw HTML
  (`dangerouslySetInnerHTML`, `innerHTML`); show untrusted text as text. Credentials and tokens
  go in memory or `sessionStorage`, never `localStorage`.
* **Sandbox environment:** a frontend profile has `bun` from the library, which brings
  read-only npm registry access. Only a task that adds shadcn/ui components also needs the
  `shadcn-registry` policy (added per profile); nothing else does, since components are
  committed.
