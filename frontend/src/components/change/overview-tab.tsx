import { useState } from 'react'
import { toast } from 'sonner'
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
import { Input } from '@/components/ui/input'
import { useAct } from '@/hooks/use-act'
import { humanize, isLive, PROGRESS_ORDER, statusColor } from '@/lib/status'
import { refsOf, withWork } from '@/lib/tickets'
import type { ChangeState } from '@/lib/types'
import { ConfirmButton } from '../confirm-button'
import { TextField } from '../fields'
import { FormDialog } from '../form-dialog'
import { AttestationLink, Muted, Sha, StatusBadge, StatusDot } from '../status'
import { AgentQuestions } from './a2a'
import { useNav } from './nav'
import { TicketTree } from './ticket-tree'

/** The pause a decision owner sets (``pause``); the workflow's own – a sign-off applying a
 * replan (``signing <id>``), a chosen replan (``replan``) – lift by themselves. */
const OPERATOR = 'operator'

function pausedText(reasons: string[]): string {
  const said = reasons.map((r) =>
    r === OPERATOR
      ? 'paused by a decision owner (Resume to go on)'
      : r.startsWith('signing ')
        ? `waiting for ${r.slice(8)}'s sign-off to apply it`
        : r === 'replan'
          ? 'waiting for a replan to be applied'
          : r,
  )
  return `${said.join('; ')} – nothing new starts meanwhile.`
}

function ProgressBar({ state }: { state: ChangeState }) {
  const tickets = withWork(state.tickets)
  const counts = new Map<string, number>()
  tickets.forEach((t) => counts.set(t.status, (counts.get(t.status) ?? 0) + 1))
  const total = tickets.length || 1
  const shown = PROGRESS_ORDER.filter((s) => counts.get(s))
  const live = state.runs.filter(isLive).length
  return (
    <div className="space-y-1.5">
      <div className="flex h-2 overflow-hidden rounded-full bg-muted">
        {shown.map((s) => (
          <div
            key={s}
            title={`${s}: ${counts.get(s)}`}
            style={{
              width: `${(100 * (counts.get(s) ?? 0)) / total}%`,
              backgroundColor: statusColor(s),
            }}
          />
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {counts.get('passed') ?? 0}/{tickets.length} tickets passed
        {shown
          .filter((s) => s !== 'passed')
          .map((s) => ` · ${humanize(s)} ${counts.get(s)}`)
          .join('')}{' '}
        · {state.runs.length} run(s), {live} live
      </p>
    </div>
  )
}

/** What a decision owner can do with the change as a whole. */
function ChangeActions({ state }: { state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [reason, setReason] = useState('')
  const [plan, setPlan] = useState('')
  const active = state.change?.status === 'ACTIVE'
  const paused = state.paused.includes(OPERATOR)
  if (!active) {
    if (state.change?.pruned_at) return null
    return (
      <ConfirmButton
        title="Prune this change?"
        description="Removes its integration branch and evidence refs from the hub; take the branch first. Its records stay."
        confirm="Prune"
        onConfirm={() => act.change('prune', {}, 'Pruned')}
      >
        Prune
      </ConfirmButton>
    )
  }
  return (
    <div className="flex flex-wrap gap-2">
      {paused ? (
        <Button variant="outline" size="sm" onClick={() => act.change('resume', {}, 'Resumed')}>
          Resume
        </Button>
      ) : (
        <Button variant="outline" size="sm" onClick={() => act.change('pause', {}, 'Pausing')}>
          Pause
        </Button>
      )}
      <FormDialog
        trigger="Replan"
        title="Replan the change"
        description="A replan document (add, cancel, update, fail), applied while the workflow is paused and nothing is running. Pause first."
        submit="Apply"
        onSubmit={async () => {
          let parsed: unknown
          try {
            parsed = JSON.parse(plan)
          } catch (e) {
            toast.error(`The plan is not JSON: ${e instanceof Error ? e.message : e}`)
            return false
          }
          return act.change('replan', { plan: parsed }, 'Replanned')
        }}
      >
        <TextField label="Plan (JSON)" value={plan} onChange={setPlan} multiline />
      </FormDialog>
      <ConfirmButton
        title="Abandon this change?"
        description="Running work stops and everything not finished is cancelled; the retrospective then runs on what there is."
        confirm="Abandon"
        body={
          <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason" />
        }
        onConfirm={() => act.change('abandon', { reason }, 'Abandoned')}
      >
        Abandon
      </ConfirmButton>
    </div>
  )
}

/** The approved version of each named output: what consumers are given. */
function Pointers({ state }: { state: ChangeState }) {
  const names = Object.keys(state.pointers)
  if (!names.length) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Signed off</CardTitle>
        <CardDescription>
          The approved version of each output: what later tickets are given.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5 text-sm">
          {names.map((n) => (
            <div key={n} className="contents">
              <dt className="font-medium">{n}</dt>
              <dd className="min-w-0">
                {refsOf(state.pointers[n]).map((ref) =>
                  ref.startsWith('git:') ? (
                    <Sha key={ref} sha={ref.split('@').at(-1)} />
                  ) : (
                    <AttestationLink key={ref} att={ref} className="block" />
                  ),
                )}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  )
}

/** Once the change has ended: its retrospective (a side process) and asking for another. */
function RetrospectiveCard({ state }: { state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [focus, setFocus] = useState('')
  const retro = state.hooks?.find((t) => t.type === 'agent:retrospective')
  const report = retro?.outputs?.report
  return (
    <Card>
      <CardHeader>
        <CardTitle>Retrospective</CardTitle>
        <CardDescription>
          An agent studies the change's logs, reflections and reviews and proposes improvements to
          the souls, skills and process.
        </CardDescription>
        {retro && (
          <CardAction>
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5"
              onClick={() => nav.openTicket(retro.id)}
            >
              <StatusDot status={retro.status} />
              {retro.status} · attempt {retro.attempt}
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center">
        {refsOf(report).map((ref) => (
          <AttestationLink key={ref} att={ref} />
        ))}
        <Input
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
          placeholder="Focus (optional): what to look at"
          className="sm:max-w-md"
        />
        <Button
          variant="secondary"
          onClick={async () => {
            if (await act.change('retrospective', { focus })) setFocus('')
          }}
        >
          {retro ? 'Run another retrospective' : 'Run retrospective'}
        </Button>
      </CardContent>
    </Card>
  )
}

export function OverviewTab({ state, selected }: { state: ChangeState; selected: string | null }) {
  const ch = state.change
  const ended = ch && ch.status !== 'ACTIVE'
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            {ch && <StatusBadge status={ch.status} />}
            {ch?.tier && <Badge variant="outline">tier {ch.tier}</Badge>}
            {state.paused.includes(OPERATOR) && <Badge variant="destructive">paused</Badge>}
          </CardTitle>
          <CardDescription>{ch?.goal}</CardDescription>
          <CardAction>
            <ChangeActions state={state} />
          </CardAction>
          {state.paused.length > 0 && <Muted>{pausedText(state.paused)}</Muted>}
          {ch?.ended && (
            <Muted>
              Ended by {ch.ended.by}
              {ch.ended.reason ? `: ${ch.ended.reason}` : ''}
            </Muted>
          )}
          {state.pull_request && (
            <p className="text-sm">
              Pull request{' '}
              <a
                href={state.pull_request.url}
                target="_blank"
                rel="noreferrer"
                className="font-medium underline underline-offset-4"
              >
                #{state.pull_request.number}
              </a>{' '}
              from <code>{state.pull_request.branch}</code>
              {state.pull_request.labels.map((l) => (
                <Badge key={l} variant="secondary" className="ml-1">
                  {l}
                </Badge>
              ))}
            </p>
          )}
        </CardHeader>
        <CardContent>
          <ProgressBar state={state} />
        </CardContent>
      </Card>
      <AgentQuestions state={state} />
      {ended && <RetrospectiveCard state={state} />}
      <Card>
        <CardHeader>
          <CardTitle>Tickets</CardTitle>
          <CardDescription>
            Each ticket's work, then its subtask groups in order (∥: in parallel), then its criteria
            (✓). Select one for its details and decisions.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <TicketTree state={state} selected={selected} />
        </CardContent>
      </Card>
      <Pointers state={state} />
    </div>
  )
}
