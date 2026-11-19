---
description: "TypeScript and web frontend house conventions: bun, React, Vite, oxlint, Prettier, bun test with happy-dom (with a tested example of spawning a CLI), CSP, sandbox profile. Load before planning, writing, testing or reviewing TypeScript or frontend code."
---

## Skill: Bun, TypeScript and web frontends stack

House conventions for TypeScript and web frontend projects (see `legion-language-preferences`).

* **React, TypeScript, Vite, Tailwind CSS v4 and shadcn/ui.** TypeScript is `strict`. shadcn/ui
  components are generated with its CLI (`bunx --bun shadcn@latest add <name>`), committed and
  kept on its default theme, light and dark.
* **bun for everything, never npm, pnpm, yarn or npx.** Dependencies are locked in `bun.lock`
  (commit it). Add with `bun add <pkg>` (dev tools: `bun add -d <pkg>`), install with
  `bun install --frozen-lockfile`.
* **Scripts run on bun, not Node:** a bun sandbox has no `node`, so a package's CLI is run as
  `bun --bun <cli>` (e.g. `bun --bun vite build`, `bun --bun prettier --check .`). Run
  `bun install --frozen-lockfile` first: before it no package CLI exists.
* **A happy-dom test preload replaces DOM globals** (`Blob`, `fetch`, …) in every test file, so
  `Bun.spawn({ stdin: new Blob(…) })` fails with "stdio must be an array of 'inherit', 'ignore',
  or null". A test that runs a CLI as a process pipes its stdin and returns named results:

  ```ts
  async function runCli(args: string[], input: string) {
    const proc = Bun.spawn(["bun", "src/cli.ts", ...args], {
      stdin: "pipe", // never `new Blob(…)`: the preload replaces Blob
      stdout: "pipe",
      stderr: "pipe",
    });
    proc.stdin.write(input);
    proc.stdin.end();
    const [stdout, stderr, exitCode] = await Promise.all([
      Bun.readableStreamToText(proc.stdout),
      Bun.readableStreamToText(proc.stderr),
      proc.exited,
    ]);
    return { exitCode, stdout, stderr }; // named, not a positional array
  }

  test("rejects a bad move", async () => {
    const r = await runCli(["--fail"], "1");
    expect(r).toMatchObject({ exitCode: 2 });
    expect(r.stderr).toContain("bad move");
  });
  ```
* **Test timeouts:** bun 1.4 has no `[test] timeout` key in `bunfig.toml` (it is ignored): set the
  timeout with the CLI flag in the `test` script, `bun test --timeout=<ms>`.
* **React effects that schedule timers** depend on primitives, not on state objects recreated each
  render, or the timer restarts on every render. Set `globalThis.IS_REACT_ACT_ENVIRONMENT = true`
  in the test preload (the scaffold task writes it) so Testing Library's updates are not reported
  as `act(...)` warnings.
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
