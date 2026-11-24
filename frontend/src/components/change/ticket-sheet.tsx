import { ScrollTextIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { isLive, kindOf } from '@/lib/status'
import { refsOf } from '@/lib/tickets'
import type { ChangeState, Run, Ticket, VerificationRun } from '@/lib/types'
import {
  AttestationLink,
  Attestations,
  Failure,
  ModelNote,
  Muted,
  Pre,
  Sha,
  StageBadge,
  StatusBadge,
  Verdict,
} from '../status'
import { AgentQuestions } from './a2a'
import { EnvironmentBuild } from './environment'
import { useNav } from './nav'
import { TicketActions } from './ticket-actions'

function KV({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function LogButton({ log }: { log: string }) {
  const nav = useNav()
  return (
    <Button variant="outline" size="sm" onClick={() => nav.openLog(log)}>
      <ScrollTextIcon data-icon="inline-start" />
      Log
    </Button>
  )
}

function TicketLink({ id, ticket }: { id: string; ticket?: Ticket }) {
  const nav = useNav()
  return (
    <Button
      variant="link"
      className="h-auto gap-1 p-0 font-mono"
      onClick={() => nav.openTicket(id)}
    >
      {id}
      {ticket && <Muted className="font-sans">({ticket.status})</Muted>}
    </Button>
  )
}

/** Named outputs: each name, its documents (a list output: several). */
function Outputs({ outputs }: { outputs: Record<string, string | string[]> }) {
  const names = Object.keys(outputs)
  if (!names.length) return <Muted>none</Muted>
  return (
    <div className="space-y-1">
      {names.map((n) => (
        <div key={n}>
          <span className="font-medium">{n}</span>{' '}
          {refsOf(outputs[n]).map((ref) => (
            <AttestationLink key={ref} att={ref} className="block" />
          ))}
        </div>
      ))}
    </div>
  )
}

function Details({ ticket, state }: { ticket: Ticket; state: ChangeState }) {
  const byId = new Map(state.tickets.map((t) => [t.id, t]))
  const rows: [string, React.ReactNode][] = [
    ['type', <code key="type">{ticket.type}</code>],
    [
      'parent',
      ticket.parent ? (
        <span key="parent">
          <TicketLink id={ticket.parent} /> · {ticket.role}
        </span>
      ) : (
        'the change'
      ),
    ],
  ]
  if (ticket.type !== 'grouping') rows.push(['attempt', `${ticket.attempt} of ${ticket.limit}`])
  if (ticket.role === 'criterion')
    rows.push([
      'on failure',
      `${ticket.severity === 'rework' ? 'its owner reworks' : 'its owner waits on a human'}${
        ticket.waivable ? ' (waivable)' : ''
      }${ticket.waived ? ' · waived' : ''}`,
    ])
  if (ticket.signs.length)
    rows.push([
      'signs',
      `${ticket.signs.join(', ')}${ticket.applies ? ` · applies ${ticket.applies}` : ''}`,
    ])
  if (ticket.labels.length) rows.push(['labels', ticket.labels.join(', ')])
  if (ticket.inputs.length)
    rows.push([
      'inputs',
      <div key="inputs" className="space-y-1">
        {ticket.inputs.map((i) => (
          <div key={i}>
            <span className="font-medium">{i}</span>{' '}
            {refsOf(state.pointers[i]).map((ref) => (
              <AttestationLink key={ref} att={ref} className="block" />
            ))}
            {!state.pointers[i] && <Muted>not signed off yet</Muted>}
          </div>
        ))}
      </div>,
    ])
  if (ticket.sha) rows.push(['commit', <Sha key="sha" sha={ticket.sha} />])
  if (ticket.sandbox)
    rows.push([
      'sandbox',
      <code key="sb" className="font-mono text-xs">
        {ticket.sandbox}
      </code>,
    ])
  if (Object.keys(ticket.outputs).length)
    rows.push(['outputs', <Outputs key="outputs" outputs={ticket.outputs} />])
  if (ticket.subtasks.length)
    rows.push([
      'subtasks',
      <ol key="subtasks" className="space-y-1">
        {ticket.subtasks.map((g, i) => (
          <li key={i}>
            <Muted>{i + 1}.</Muted>{' '}
            {g.map((id, j) => (
              <span key={id}>
                {j > 0 && <Muted> ∥ </Muted>}
                <TicketLink id={id} ticket={byId.get(id)} />
              </span>
            ))}
          </li>
        ))}
      </ol>,
    ])
  if (ticket.criteria.length)
    rows.push([
      'criteria',
      <ol key="criteria" className="space-y-1">
        {ticket.criteria.map((id, i) => (
          <li key={id}>
            <Muted>{i + 1}.</Muted> <TicketLink id={id} ticket={byId.get(id)} />
          </li>
        ))}
      </ol>,
    ])
  return (
    <div className="space-y-4">
      <KV rows={rows} />
      {ticket.findings.length > 0 && (
        <section className="space-y-2">
          <h3 className="font-medium">
            {ticket.role === 'criterion' ? 'Findings' : 'Findings to address'}
          </h3>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {ticket.findings.map((f, i) => (
              <li key={i} className="whitespace-pre-wrap">
                {f}
              </li>
            ))}
          </ul>
        </section>
      )}
      {Object.keys(ticket.work).length > 0 && (
        <Collapsible>
          <CollapsibleTrigger render={<Button variant="ghost" size="sm" />}>
            Work
          </CollapsibleTrigger>
          <CollapsibleContent>
            <Pre>{JSON.stringify(ticket.work, null, 2)}</Pre>
          </CollapsibleContent>
        </Collapsible>
      )}
      {ticket.type === 'controller:provision' && (
        <section className="space-y-2">
          <h3 className="font-medium">Provisioned environment</h3>
          <EnvironmentBuild change={state.change} />
        </section>
      )}
      <AgentQuestions state={state} ticket={ticket.id} />
    </div>
  )
}

function RunCard({ run }: { run: Run }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span className="font-mono">{run.id}</span>
          <StatusBadge status={run.status} />
          {isLive(run) && <StageBadge stage={run.stage} />}
          <span className="font-normal">
            {run.role} · attempt {run.attempt} · {run.soul} <ModelNote run={run} />
            {run.consult && ' · a message'}
          </span>
        </CardTitle>
        <CardDescription className="font-mono text-xs">
          sandbox {run.sandbox}
          {run.sandbox_deleted && ' (deleted)'} ·{' '}
          {run.branch
            ? `branch ${run.branch}`
            : `detached at ${(run.judged_sha ?? run.base_sha ?? '').slice(0, 12)}`}{' '}
          · prompt {(run.prompt_pin ?? '').slice(7, 19)}
        </CardDescription>
        <CardAction>
          <LogButton log={run.id} />
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <Failure failure={run.failure} />
        {run.progress.slice(-4).map((p, i) => (
          <p key={i}>
            · {p.percent != null && `${Math.round(p.percent)}% `}
            {p.message}
          </p>
        ))}
        {run.report && (
          <div className="space-y-1">
            <p>
              <b>{run.report.status}</b>: {run.report.summary}
            </p>
            <Attestations atts={run.report.attestations} />
          </div>
        )}
        {run.violations.length > 0 && (
          <Collapsible>
            <CollapsibleTrigger
              render={<Button variant="ghost" size="sm" className="text-destructive" />}
            >
              {run.violations.length} rejected report(s)
            </CollapsibleTrigger>
            <CollapsibleContent>
              <Pre>{run.violations.map((v) => v.join('\n')).join('\n---\n')}</Pre>
            </CollapsibleContent>
          </Collapsible>
        )}
      </CardContent>
    </Card>
  )
}

function VerificationCard({ v }: { v: VerificationRun }) {
  const r = v.result
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span className="font-mono">{v.id}</span>
          {r && !r.infra_error ? <Verdict ok={r.ok} /> : <Badge variant="outline">{v.state}</Badge>}
          <Sha sha={v.sha} />
        </CardTitle>
        {r?.infra_error && (
          <CardDescription className="text-destructive">{r.infra_error}</CardDescription>
        )}
        {r?.log && (
          <CardAction>
            <LogButton log={r.log} />
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="space-y-1 font-mono text-xs">
        {(r?.results ?? []).map((c, i) => (
          <p key={i}>
            {c.exit_code === 0 ? '✓' : '✗'} {c.setup && <Badge variant="outline">setup</Badge>}{' '}
            {c.command} → {c.exit_code}
          </p>
        ))}
        {r?.attestation && <AttestationLink att={r.attestation} />}
      </CardContent>
    </Card>
  )
}

/** A ticket's decisions, details, verifications and runs, over the current tab. */
export function TicketSheet({
  state,
  ticketId,
  onClose,
}: {
  state: ChangeState
  ticketId: string | null
  onClose: () => void
}) {
  const ticket =
    state.tickets.find((t) => t.id === ticketId) ?? state.hooks?.find((t) => t.id === ticketId)
  const runs = ticket ? state.runs.filter((r) => r.ticket === ticket.id).reverse() : []
  const verifications = ticket
    ? (state.verifications ?? []).filter((v) => v.node === ticket.id).reverse()
    : []
  const verifying = ticket ? state.verifying?.[ticket.id] : undefined
  return (
    <Sheet open={Boolean(ticket)} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-2xl">
        {ticket && (
          <>
            <SheetHeader className="border-b">
              <SheetTitle className="flex flex-wrap items-center gap-2 pr-8">
                <span className="font-mono">{ticket.id}</span>
                <StatusBadge
                  status={ticket.status}
                  label={ticket.waiting ? `waiting: ${ticket.waiting}` : undefined}
                />
                <Badge variant="outline">{kindOf(ticket)}</Badge>
              </SheetTitle>
              <SheetDescription>{ticket.goal || ticket.type}</SheetDescription>
            </SheetHeader>
            <ScrollArea className="min-h-0 flex-1">
              <div className="space-y-4 p-4">
                <TicketActions ticket={ticket} state={state} />
                <Details ticket={ticket} state={state} />
                {(verifying || verifications.length > 0) && (
                  <section className="space-y-2">
                    <h3 className="font-medium">Verification</h3>
                    {verifying && (
                      <div className="flex items-center gap-2 text-sm">
                        verifying now <LogButton log={verifying} />
                      </div>
                    )}
                    {verifications.map((v) => (
                      <VerificationCard key={v.id} v={v} />
                    ))}
                  </section>
                )}
                {runs.length > 0 && (
                  <section className="space-y-2">
                    <h3 className="font-medium">Runs ({runs.length})</h3>
                    {runs.map((r) => (
                      <RunCard key={r.id} run={r} />
                    ))}
                  </section>
                )}
              </div>
            </ScrollArea>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
