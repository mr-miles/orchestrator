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
          Asked over A2A (the orchestrator's ask_human); the agent waits for the answer.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {open.map((q) => (
          <OpenQuestion key={q.id} q={q} />
        ))}
        {done.map((q) => (
          <div key={q.id} className="text-sm text-muted-foreground">
            <b>
              {q.role}@{q.ticket}
            </b>
            : {q.question} —{' '}
            {q.answer != null ? `${q.answered_by}: ${q.answer}` : `withdrawn (${q.withdrawn})`}
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
