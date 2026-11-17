# Operator UI

React 19, TypeScript (strict), Vite, Tailwind CSS v4 and [shadcn/ui](https://ui.shadcn.com)
(default theme, light and dark), built and checked with [bun](https://bun.sh).

```bash
bun install
bun run dev            # http://localhost:5173, proxying /api to a controller on :8765
                       # (LEGION_CONTROLLER=http://host:port for another)
bun run build          # type-check, then build into ../legion_orch/server/static/dist
bun run check          # typecheck, lint (oxlint), format check (prettier) and tests (bun test)
bun run format         # prettier, with Tailwind class sorting
```

The controller serves the build with a Content Security Policy that allows no inline script: the
UI displays agent-authored text, so nothing may inject markup (`dangerouslySetInnerHTML` is not
used; a unit test in the Python suite checks this). `public/theme.js` applies the saved theme
before the first paint as a same-origin script for the same reason.

## Layout

- `src/lib/` – the API client and types, the event streams (`streams.ts`: a change's state and a
  run's log, pushed over server-sent events), the task graph layout and status helpers.
- `src/components/change/` – one change: the tabs (Overview, Agents, Gates, Plan, Documents,
  Workflow) and the sheets opened over them (a node with its decisions, an agent log, a
  document).
- `src/components/ui/` – shadcn/ui components, added with `bunx --bun shadcn@latest add <name>`.
- `src/test/` – test setup (happy-dom) and fakes for `EventSource` and `fetch`.
