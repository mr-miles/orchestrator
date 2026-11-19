import { ChevronDownIcon, TriangleAlertIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useFetch } from '@/hooks/use-fetch'
import { getAcceptance } from '@/lib/api'
import type {
  Acceptance,
  ChangeState,
  FileStat,
  TaskTrace,
  Traceability,
  TraceWarning,
} from '@/lib/types'
import { humanize } from '@/lib/status'
import { docsVersion, summaryLine, useTrace } from '@/lib/trace'
import { Muted, Sha, StatusDot, Verdict } from '../status'
import { useNav } from './nav'

const rel = (path: string) => path.split('/').slice(2).join('/')

/** What the human should weigh first; each links to the document, node or diff involved. */
export function TraceWarnings({ warnings }: { warnings: TraceWarning[] }) {
  const nav = useNav()
  if (!warnings.length) return null
  return (
    <ul className="space-y-1" aria-label="Traceability warnings">
      {warnings.map((w, i) => (
        <li key={i} className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <TriangleAlertIcon className="size-3.5 shrink-0 translate-y-0.5 text-amber-600" />
          <span>{w.message}</span>
          {w.kind === 'stale' && w.path && w.version ? (
            <Button
              variant="link"
              className="h-auto p-0 text-xs"
              onClick={() => nav.openDiff(w.path ?? '', w.version ?? 0)}
            >
              Changed since approved
            </Button>
          ) : w.task ? (
            <Button
              variant="link"
              className="h-auto p-0 text-xs"
              onClick={() => nav.openNode(`build:${w.task}`)}
            >
              {w.task}
            </Button>
          ) : (
            w.path && (
              <Button
                variant="link"
                className="h-auto p-0 font-mono text-xs"
                onClick={() => nav.openDoc(w.path ?? '')}
              >
                {rel(w.path)}
              </Button>
            )
          )}
        </li>
      ))}
    </ul>
  )
}

function TaskCell({ task }: { task?: TaskTrace }) {
  const nav = useNav()
  if (!task) return null
  const label = (
    <>
      <StatusDot status={task.status === 'PLANNED' ? 'PENDING' : task.status} />
      <span className="font-mono">{task.id}</span>
    </>
  )
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      {task.node ? (
        <Button
          variant="link"
          className="h-auto gap-1.5 p-0 text-xs"
          title={task.title}
          onClick={() => nav.openNode(task.node ?? '')}
        >
          {label}
        </Button>
      ) : (
        <span className="inline-flex items-center gap-1.5" title={task.title}>
          {label}
        </span>
      )}
      {task.scaffold && <Badge variant="outline">scaffold</Badge>}
      {task.check && <Verdict ok={task.check.ok} label={task.check.ok ? 'verified' : 'failed'} />}
      {task.reviews.map((r) => (
        <Badge key={r.role} variant={r.verdict === 'PASS' ? 'secondary' : 'destructive'}>
          {r.role} {r.verdict}
        </Badge>
      ))}
    </div>
  )
}

/** One row per requirement: its delta spec, the tasks covering it, the criteria testing it. */
export function TraceMatrix({ trace }: { trace: Traceability }) {
  const nav = useNav()
  const tasks = new Map(trace.tasks.map((t) => [t.id, t]))
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Requirement</TableHead>
          <TableHead>Delta spec</TableHead>
          <TableHead>Tasks</TableHead>
          <TableHead>Acceptance criteria</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {trace.requirements.map((r) => (
          <TableRow key={r.id} className="align-top">
            <TableCell className="max-w-64 whitespace-normal">
              <span className="font-mono font-medium">{r.id}</span>{' '}
              <span className="text-muted-foreground">{r.text}</span>
            </TableCell>
            <TableCell>
              {r.spec ? (
                <Button
                  variant="link"
                  className="h-auto gap-1.5 p-0 text-xs"
                  onClick={() => nav.openDoc(r.spec?.path ?? '')}
                >
                  <StatusDot status={H2_DOT[r.spec.status]} />
                  {r.spec.status}
                </Button>
              ) : (
                '—'
              )}
            </TableCell>
            <TableCell className="space-y-1">
              {r.tasks.map((id) => (
                <TaskCell key={id} task={tasks.get(id)} />
              ))}
              {!r.tasks.length && <Muted>none</Muted>}
            </TableCell>
            <TableCell className="max-w-md space-y-1.5 whitespace-normal">
              {r.criteria.map((c) => (
                <div key={c.id} className="text-xs">
                  <span className="font-mono font-medium">{c.id}</span> {c.text}
                  {c.result && (
                    <Badge
                      className="ml-1.5"
                      variant={c.result === 'pass' ? 'secondary' : 'destructive'}
                    >
                      {c.result}
                    </Badge>
                  )}
                  <div className="text-muted-foreground">
                    {[...c.tests, ...c.commands].length ? (
                      <>
                        {c.tests.map((t) => (
                          <code key={t} className="mr-2">
                            {t}
                          </code>
                        ))}
                        {c.commands.map((x) => (
                          <code key={x} className="mr-2">
                            $ {x}
                          </code>
                        ))}
                      </>
                    ) : (
                      <span className="text-amber-700 dark:text-amber-500">no linked test</span>
                    )}
                  </div>
                </div>
              ))}
              {!r.criteria.length && <Muted>none</Muted>}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

const H2_DOT = { approved: 'ACCEPTED', stale: 'ESCALATED', pending: 'REVIEWING' } as const

/** For a gate card: the summary line and the warnings, expanding to the whole matrix. */
export function TraceSummary({ state }: { state: ChangeState }) {
  const { data, error } = useTrace(useNav().cid, state)
  if (error) return <p className="text-sm text-destructive">Traceability: {error}</p>
  if (!data) return <Muted>Loading traceability…</Muted>
  return (
    <Collapsible className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Traceability</span>
        <span className="text-sm text-muted-foreground">{summaryLine(data)}</span>
        <CollapsibleTrigger render={<Button variant="ghost" size="sm" className="ml-auto" />}>
          Matrix <ChevronDownIcon />
        </CollapsibleTrigger>
      </div>
      <TraceWarnings warnings={data.warnings} />
      <CollapsibleContent>
        <TraceMatrix trace={data} />
      </CollapsibleContent>
    </Collapsible>
  )
}

/** The Gates tab's card: the whole matrix, its warnings and, after integration, the results. */
export function TraceabilityCard({ state }: { state: ChangeState }) {
  const { data, error } = useTrace(useNav().cid, state)
  const res = data?.results
  return (
    <Card>
      <CardHeader>
        <CardTitle>Traceability</CardTitle>
        <CardDescription>
          {data
            ? summaryLine(data)
            : (error ?? 'Requirements, the tasks that cover them, the criteria that test them.')}
        </CardDescription>
      </CardHeader>
      {data && (
        <CardContent className="space-y-3">
          <TraceWarnings warnings={data.warnings} />
          {data.requirements.length ? (
            <TraceMatrix trace={data} />
          ) : (
            <Muted>No accepted requirements yet.</Muted>
          )}
          {res && (
            <div className="space-y-1 text-sm">
              <p>
                At integration head <Sha sha={res.head} />: verifier{' '}
                {res.verdict ? <Verdict ok={res.verdict === 'PASS'} label={res.verdict} /> : '—'}
                {res.ok != null && (
                  <>
                    {' '}
                    · controller{' '}
                    <Verdict ok={res.ok} label={res.ok ? 'commands pass' : 'commands fail'} />
                  </>
                )}
              </p>
              <ul className="font-mono text-xs">
                {res.commands.map((c) => (
                  <li key={c.command}>
                    <span className={c.exit_code === 0 ? '' : 'text-destructive'}>
                      exit {c.exit_code}
                    </span>{' '}
                    $ {c.command}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}

function lines(f: { added?: number | null; deleted?: number | null }) {
  return f.added == null ? (
    <Muted>binary</Muted>
  ) : (
    <span className="font-mono text-xs">
      <span className="text-green-700 dark:text-green-500">+{f.added}</span>{' '}
      <span className="text-red-700 dark:text-red-500">−{f.deleted}</span>
    </span>
  )
}

function Files({ files }: { files: FileStat[] }) {
  return (
    <ul className="space-y-0.5">
      {files.map((f) => (
        <li key={f.path} className="flex gap-3 text-xs">
          <code className="min-w-0 flex-1 break-all">{f.path}</code>
          {lines(f)}
        </li>
      ))}
    </ul>
  )
}

/** H-3: what is being accepted – the diff per task, protected paths, gates not yet satisfied. */
export function AcceptancePreview({ state }: { state: ChangeState }) {
  const { cid } = useNav()
  const head = state.change?.integration_head ?? ''
  const { data, error } = useFetch<Acceptance>(
    () => getAcceptance(cid),
    `${cid}:${head}:${docsVersion(state)}`,
  )
  if (error || data?.error)
    return <p className="text-sm text-destructive">{error ?? data?.error}</p>
  if (!data?.diff) return <Muted>Loading the diff…</Muted>
  const d = data.diff
  const open = Object.entries(data.readiness?.gates ?? {}).filter(
    ([, g]) => g.status !== 'satisfied',
  )
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <p className="text-sm">
          <b>Diff</b> <Sha sha={d.base} /> → <Sha sha={d.head} />: {d.files.length} files,{' '}
          {lines(d)}
        </p>
        <Table>
          <TableBody>
            {d.tasks.map((t) => (
              <TableRow key={t.id} className="align-top">
                <TableCell className="w-0 font-mono text-xs">{t.id}</TableCell>
                <TableCell className="whitespace-normal">
                  <Collapsible>
                    <CollapsibleTrigger
                      render={<Button variant="link" className="h-auto p-0 text-xs" />}
                    >
                      {t.title} · {t.files.length} files
                    </CollapsibleTrigger>
                    <CollapsibleContent className="pt-1">
                      <Files files={t.files} />
                    </CollapsibleContent>
                  </Collapsible>
                </TableCell>
                <TableCell className="w-0 text-right">{lines(t)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {d.protected.length > 0 && (
        <div className="space-y-1 text-sm">
          <b>Protected paths changed</b>
          <ul className="text-xs">
            {d.protected.map((p) => (
              <li key={p.path}>
                <code>{p.path}</code>{' '}
                {p.approved_by.length ? (
                  <Muted className="inline">
                    approved by {p.approved_by.join(', ')} for {p.tasks.join(', ')} (G-PP1)
                  </Muted>
                ) : (
                  <span className="text-destructive">no approval</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data.readiness && (
        <div className="space-y-1 text-sm">
          <b>Ship gates, evaluated now</b>{' '}
          <Muted className="inline">
            {open.length
              ? `${open.length} of ${Object.keys(data.readiness.gates).length} not satisfied yet`
              : 'every gate is satisfied'}
          </Muted>
          <ul className="text-xs">
            {open.map(([g, r]) => (
              <li key={g}>
                <StatusDot status={r.status === 'unsatisfied' ? 'ESCALATED' : 'REVIEWING'} />{' '}
                <code>{g}</code> {humanize(r.status)}: {r.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
