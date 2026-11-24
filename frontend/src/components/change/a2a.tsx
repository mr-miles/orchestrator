import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { useAct } from '@/hooks/use-act'
import type { A2ACard, AgentQuestion, ChangeState } from '@/lib/types'
import { useNav } from './nav'

/** A build's files as they are and as they would be if the request is approved: the added
 * paths marked. */
function ScopeChange({ q }: { q: AgentQuestion }) {
  const after = [...q.before, ...q.paths.filter((p) => !q.before.includes(p))]
  const list = (paths: string[], added: boolean) => (
    <ul className="font-mono text-xs">
      {paths.map((p) => (
        <li
          key={p}
          className={added && q.paths.includes(p) ? 'font-medium text-primary' : undefined}
        >
          {added && q.paths.includes(p) ? '+ ' : '  '}
          {p}
        </li>
      ))}
    </ul>
  )
  return (
    <div className="grid grid-cols-2 gap-3 text-sm" aria-label="files_modified before and after">
      <div>
        <p className="text-muted-foreground">files_modified now</p>
        {list(q.before, false)}
      </div>
      <div>
        <p className="text-muted-foreground">if approved</p>
        {list(after, true)}
      </div>
    </div>
  )
}

/** A build's request for more files: its reason, what to know, its files before and after,
 * and the decision – approve, or deny with a reason. */
function OpenScopeRequest({ q }: { q: AgentQuestion }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [reason, setReason] = useState('')
  return (
    <div className="space-y-2 rounded-md border p-3">
      <p className="text-sm">
        <Button variant="link" className="h-auto p-0" onClick={() => nav.openTicket(q.ticket)}>
          {q.role}@{q.ticket}
        </Button>{' '}
        asks to change <b>{q.paths.join(', ')}</b>:
      </p>
      <p className="text-sm whitespace-pre-wrap">{q.question}</p>
      {q.warnings.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-destructive">
          {q.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      <ScopeChange q={q} />
      <Textarea
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Reason (required to deny; sent to the agent)"
        aria-label={`Reason for ${q.role}@${q.ticket}'s request`}
      />
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={async () => {
            if (await act.decideScope(q.id, 'approve', reason)) setReason('')
          }}
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="destructive"
          disabled={!reason.trim()}
          onClick={async () => {
            if (await act.decideScope(q.id, 'deny', reason)) setReason('')
          }}
        >
          Deny
        </Button>
      </div>
    </div>
  )
}

function OpenQuestion({ q }: { q: AgentQuestion }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [answer, setAnswer] = useState('')
  return (
    <div className="space-y-2 rounded-md border p-3">
      <p className="text-sm">
        <Button variant="link" className="h-auto p-0" onClick={() => nav.openTicket(q.ticket)}>
          {q.role}@{q.ticket}
        </Button>{' '}
        asks:
      </p>
      <p className="text-sm whitespace-pre-wrap">{q.question}</p>
      <Textarea
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        placeholder="Your answer (sent to the agent as is)"
        aria-label={`Answer to ${q.role}@${q.ticket}`}
      />
      <Button
        size="sm"
        disabled={!answer.trim()}
        onClick={async () => {
          if (await act.answer(q.id, answer)) setAnswer('')
        }}
      >
        Answer
      </Button>
    </div>
  )
}

/** Agents' questions to the decision owners (A2A ask_human): the open ones, to answer, then
 * the answered and withdrawn ones. ``ticket`` limits them to one ticket's agents. */
export function AgentQuestions({ state, ticket }: { state: ChangeState; ticket?: string }) {
  const qs = (state.a2a?.questions ?? []).filter((q) => !ticket || q.ticket === ticket)
  if (!qs.length) return null
  const open = qs.filter((q) => q.answer == null && q.withdrawn == null)
  const done = qs.filter((q) => !open.includes(q))
  return (
    <Card>
      <CardHeader>
        <CardTitle>Agents' questions</CardTitle>
        <CardDescription>
          Asked over A2A (the orchestrator's ask_human, or a build's request_scope for more files);
          the agent waits for the answer.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {open.map((q) =>
          q.kind === 'scope' ? (
            <OpenScopeRequest key={q.id} q={q} />
          ) : (
            <OpenQuestion key={q.id} q={q} />
          ),
        )}
        {done.map((q) => (
          <div key={q.id} className="text-sm text-muted-foreground">
            <b>
              {q.role}@{q.ticket}
            </b>
            : {q.kind === 'scope' ? `asked for ${q.paths.join(', ')} (${q.question})` : q.question}{' '}
            — {q.answer != null ? `${q.answered_by}: ${q.answer}` : `withdrawn (${q.withdrawn})`}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

/** The change's A2A cards: what its agents can message. */
export function AgentCards({ cards }: { cards: A2ACard[] }) {
  const nav = useNav()
  if (!cards.length) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>A2A cards</CardTitle>
        <CardDescription>
          The orchestrator, the resolver, and every agent that has started on a ticket (a message to
          one that is stopped resumes it for an answer). Agents find them in the change's catalog.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Card</TableHead>
              <TableHead>Agent</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {cards.map((c) => (
              <TableRow key={c.id}>
                <TableCell className="font-mono">
                  {c.ticket && c.role !== 'resolver' ? (
                    <Button
                      variant="link"
                      className="h-auto p-0 font-mono"
                      onClick={() => nav.openTicket(c.ticket!)}
                    >
                      {c.id}
                    </Button>
                  ) : (
                    c.id
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant={c.state === 'running' ? 'outline' : 'secondary'}>{c.state}</Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
