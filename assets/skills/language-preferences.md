---
description: "Index of the house conventions per language stack. Load before planning, writing, testing or reviewing code, to find the stack skill to read."
---

## Skill: Language preferences

The house conventions for each language. They apply to every plan, task, test, review and
sandbox environment in a project using that language. When the repository already does
something differently, follow the repository and say so; never mix both ways in one project.

Each stack has its own skill. Before you plan, write, test or review code in a stack, or write a
sandbox environment for it, load its skill:

| Stack | Skill |
| --- | --- |
| Python (uv, ruff, pytest) | `legion-stack-python` |
| TypeScript and web frontends (bun, React, Vite, oxlint, Prettier) | `legion-stack-bun-typescript` |

A stack with no skill here has no house conventions: follow the repository.
