import { useState } from 'react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { useAct } from '@/hooks/use-act'
import { WAITING_TEXT } from '@/lib/status'
import { refsOf } from '@/lib/tickets'
import type { ChangeState, Ticket, Waiting } from '@/lib/types'
import { FormDialog } from '../form-dialog'
import { TextField } from '../fields'
import { AttestationLink, Attestations, Muted, Pre, Sha } from '../status'
import { EnvironmentSpec } from './environment'
import { useNav } from './nav'

/** The remedies each reason for waiting allows (tickets/model.py REMEDIES): waivers are per
 * rejected, waivable criterion. */
const REMEDIES: Record<Waiting, ('retry' | 'waive' | 'replan')[]> = {
  attempts: ['waive', 'replan'],
  repeated: ['waive', 'replan'],
  escalated: ['waive', 'replan'],
  system: ['retry', 'replan'],
  budget: ['retry', 'replan'],
  blocked: ['retry', 'replan'],
  empty: ['replan'],
}

/** A sign-off's subjects as shown to its decision owners: each named output's documents at the
 * exact versions, ``head`` as its commit. */
function Subjects({ subjects }: { subjects: Record<string, string | string[]> }) {
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5 text-sm">
      {Object.entries(subjects).map(([name, v]) => (
        <div key={name} className="contents">
          <dt className="font-medium">{name}</dt>
          <dd className="min-w-0">
            {refsOf(v).map((ref) =>
              ref.startsWith('git:') ? (
                <Sha key={ref} sha={ref.split('@').at(-1)} />
              ) : (
                <AttestationLink key={ref} att={ref} />
              ),
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function SignoffDecision({ ticket, state }: { ticket: Ticket; state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [reason, setReason] = useState('')
  const due = state.signoffs[ticket.id]
  if (!due) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Sign-off due</CardTitle>
        <CardDescription>
          You sign exactly these versions
          {ticket.applies && <> – and approving applies {ticket.applies}</>}. A rejection sends its
          work back with your reason.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Subjects subjects={due.subjects} />
        {'environment' in due.subjects && state.environment && (
          <EnvironmentSpec env={state.environment} />
        )}
        <Textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (required to reject)"
          aria-label={`Reason for ${ticket.id}`}
        />
        <div className="flex gap-2">
          <Button
            onClick={async () => {
              if (await act.ticket(ticket.id, 'approve', { reason }, `${ticket.id} approved`))
                setReason('')
            }}
          >
            Approve
          </Button>
          <Button
            variant="destructive"
            disabled={!reason.trim()}
            onClick={async () => {
              if (await act.ticket(ticket.id, 'reject', { reason }, `${ticket.id} rejected`))
                setReason('')
            }}
          >
            Reject
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function Proposal({ ticket, state }: { ticket: Ticket; state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const p = state.change?.proposals?.[ticket.id]
  if (!p) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          The resolver proposes <Badge variant="secondary">{p.action}</Badge>
        </CardTitle>
        <CardDescription className="whitespace-pre-wrap">{p.rationale}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {Object.keys(p.payload).length > 0 && <Pre>{JSON.stringify(p.payload, null, 2)}</Pre>}
        <Attestations atts={p.attestations} />
        <Button onClick={() => act.ticket(ticket.id, p.action, p.payload, 'Proposal applied')}>
          Apply proposal
        </Button>
      </CardContent>
    </Card>
  )
}

function Remedies({ ticket, state }: { ticket: Ticket; state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [answer, setAnswer] = useState('')
  const [reason, setReason] = useState('')
  const proposed = state.change?.proposals?.[ticket.id]
  const [plan, setPlan] = useState('')
  if (ticket.status !== 'waiting' || !ticket.waiting) return null
  const allowed = REMEDIES[ticket.waiting]
  const byId = new Map(state.tickets.map((t) => [t.id, t]))
  const waivable = ticket.criteria
    .map((c) => byId.get(c))
    .filter((c): c is Ticket => Boolean(c && c.status === 'rejected' && c.waivable))
  return (
    <Card>
      <CardHeader>
        <CardTitle>Waiting on a human</CardTitle>
        <CardDescription>
          {WAITING_TEXT[ticket.waiting]}
          {ticket.findings.length > 0 && ' – its findings are below.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        {allowed.includes('retry') && (
          <FormDialog
            trigger="Retry"
            title={`Retry ${ticket.id}`}
            description="The same ticket again, at the same attempt. An answer (to a blocked agent's question, say) is what its next round is told."
            submit="Retry"
            onSubmit={() => act.ticket(ticket.id, 'retry', answer.trim() ? { answer } : {})}
          >
            <TextField label="Answer (optional)" value={answer} onChange={setAnswer} multiline />
          </FormDialog>
        )}
        {allowed.includes('waive') &&
          waivable.map((c) => (
            <FormDialog
              key={c.id}
              trigger={`Waive ${c.id}`}
              title={`Waive ${c.id}`}
              description="Its failure counts as passed, with your reason on the record."
              submit="Waive"
              variant="destructive"
              onSubmit={() => act.ticket(ticket.id, `waive:${c.id}`, { reason })}
            >
              <TextField label="Reason" value={reason} onChange={setReason} multiline />
            </FormDialog>
          ))}
        {allowed.includes('waive') && waivable.length === 0 && ticket.waiting !== 'empty' && (
          <Muted>No failed criterion of it can be waived.</Muted>
        )}
        <FormDialog
          trigger="Replan"
          title={`Replan ${ticket.id}`}
          description="A replan document: it fails this ticket (replaced) or adds to it (repaired), and applies as soon as nothing else is running. Without one, the workflow waits paused for a change-level replan."
          submit="Replan"
          onSubmit={async () => {
            const text =
              plan.trim() || (proposed?.payload?.plan ? JSON.stringify(proposed.payload.plan) : '')
            let parsed: unknown = undefined
            if (text) {
              try {
                parsed = JSON.parse(text)
              } catch (e) {
                toast.error(`The plan is not JSON: ${e instanceof Error ? e.message : e}`)
                return false
              }
            }
            return act.ticket(ticket.id, 'replan', parsed === undefined ? {} : { plan: parsed })
          }}
        >
          <TextField
            label="Plan (JSON, optional)"
            value={plan}
            onChange={setPlan}
            multiline
            placeholder={'{"fail": ["…"], "add": [{"into": "…", "group": 1, "ticket": {…}}]}'}
          />
        </FormDialog>
      </CardContent>
    </Card>
  )
}

/** What a decision owner may do on a ticket now: a sign-off's decision, a waiting ticket's
 * remedies and the resolver's proposal for it. Nothing when nothing is due. */
export function TicketActions({ ticket, state }: { ticket: Ticket; state: ChangeState }) {
  return (
    <div className="space-y-3">
      <SignoffDecision ticket={ticket} state={state} />
      <Proposal ticket={ticket} state={state} />
      <Remedies ticket={ticket} state={state} />
    </div>
  )
}
