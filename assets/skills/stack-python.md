---
description: "Python house conventions: uv, ruff, pytest, verification commands, sandbox profile. Load before planning, writing, testing or reviewing Python code."
---

## Skill: Python stack

House conventions for Python projects (see `legion-language-preferences`).

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
