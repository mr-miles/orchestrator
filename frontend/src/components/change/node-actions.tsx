import { useState } from 'react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { FieldGroup } from '@/components/ui/field'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useAct } from '@/hooks/use-act'
import { docPath } from '@/lib/api'
import type { ChangeState, Node } from '@/lib/types'
import { ConfirmButton } from '../confirm-button'
import { SelectField, TextField } from '../fields'
import { Pre, Sha, SourceBadge, StatusDot } from '../status'
import { EnvironmentSpec } from './environment'
import { useNav } from './nav'

function Escalated({ node }: { node: Node }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [answer, setAnswer] = useState('')
  const [reason, setReason] = useState('')
  const [why, setWhy] = useState('')
  const e = node.escalation
  if (!e) return null
  if (e.retrying)
    return (
      <Alert>
        <AlertTitle>Retrying</AlertTitle>
        <AlertDescription>Removing the old sandbox, then dispatching a new run…</AlertDescription>
      </Alert>
    )
  return (
    <Card className="ring-destructive/40">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          Escalation: {e.kind} <SourceBadge source={e.source} />
        </CardTitle>
        <CardDescription className="text-foreground">{e.reason}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {e.missing_decision && (
          <p className="text-sm">
            <b>Decision needed:</b> {e.missing_decision}
          </p>
        )}
        {e.run && (
          <Button variant="link" className="h-auto p-0" onClick={() => nav.openLog(e.run ?? '')}>
            Agent log of {e.run}
          </Button>
        )}
        {e.log_tail && (
          <Collapsible>
            <CollapsibleTrigger render={<Button variant="ghost" size="sm" />}>
              Last log lines
            </CollapsibleTrigger>
            <CollapsibleContent>
              <Pre>{e.log_tail}</Pre>
            </CollapsibleContent>
          </Collapsible>
        )}
        <FieldGroup>
          <TextField
            label="Answer / decision"
            value={answer}
            onChange={setAnswer}
            multiline
            description="Attested as feedback for the next run."
          />
          {node.kind === 'build' && (
            <TextField
              label="Override reason"
              value={reason}
              onChange={setReason}
              description="At least 24 characters; for Override accept."
            />
          )}
        </FieldGroup>
      </CardContent>
      <CardFooter className="flex flex-wrap gap-2">
        <Button onClick={() => act.node(node.id, 'answer', { answer }, 'Resumed')}>
          Answer &amp; resume
        </Button>
        <Button
          variant="secondary"
          title="Removes the failed run's sandbox, then starts a new run"
          onClick={() => act.node(node.id, 'retry', {}, 'Retrying')}
        >
          Retry
        </Button>
        {node.kind === 'build' && (
          <Button
            variant="outline"
            onClick={() => act.node(node.id, 'override_accept', { reason })}
          >
            Override accept
          </Button>
        )}
        <ConfirmButton
          title="Abandon the change?"
          description="The change ends here; nothing more is dispatched for it. A retrospective then runs on what it produced so far."
          confirm="Abandon"
          body={<AbandonReason value={why} onChange={setWhy} />}
          onConfirm={() => act.node(node.id, 'abandon', { reason: why }, 'Change abandoned')}
        >
          Abandon
        </ConfirmButton>
      </CardFooter>
    </Card>
  )
}

const TIERS = [
  { value: '', label: 'Keep the derived tier' },
  ...['R0', 'R1', 'R2', 'R3'].map((t) => ({ value: t, label: t })),
]

function AcceptRequirements({ state }: { state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [tier, setTier] = useState('')
  const [reason, setReason] = useState('')
  const [feedback, setFeedback] = useState('')
  const r = state.change?.risk
  return (
    <Card>
      <CardHeader>
        <CardTitle>Accept requirements</CardTitle>
        <CardDescription>
          Derived tier <b>{r?.tier}</b> (score {r?.total}; threshold {r?.threshold_tier}; floors{' '}
          {(r?.floors ?? []).map((f) => `${f.signal}→${f.tier}`).join(', ') || 'none'})
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Signal</TableHead>
              <TableHead className="text-right">Score</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Object.entries(r?.scores ?? {}).map(([k, v]) => (
              <TableRow key={k}>
                <TableCell>{k}</TableCell>
                <TableCell className="text-right">{v}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <div className="flex flex-wrap gap-x-3 text-sm">
          {['intake/requirements.md', 'intake/risk.json', 'intake/questions.md'].map((p) => (
            <Button
              key={p}
              variant="link"
              className="h-auto p-0"
              onClick={() => nav.openDoc(docPath(nav.cid, p))}
            >
              {p.split('/')[1]}
            </Button>
          ))}
        </div>
        <FieldGroup>
          <SelectField label="Tier" value={tier} onChange={setTier} options={TIERS} />
          <TextField
            label="Reason"
            value={reason}
            onChange={setReason}
            description="Required to lower the tier."
          />
          <TextField
            label="Feedback"
            value={feedback}
            onChange={setFeedback}
            multiline
            description="For a revision."
          />
        </FieldGroup>
      </CardContent>
      <CardFooter className="flex gap-2">
        <Button
          onClick={() =>
            act.node('H-1', 'accept', { tier: tier || null, reason }, 'Requirements accepted')
          }
        >
          Accept
        </Button>
        <Button
          variant="outline"
          onClick={() => act.node('H-1', 'revise', { feedback }, 'Revision requested')}
        >
          Request revision
        </Button>
      </CardFooter>
    </Card>
  )
}

const H2_DOT = { approved: 'ACCEPTED', stale: 'ESCALATED', pending: 'REVIEWING' } as const

function ApproveSpec({ state }: { state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [feedback, setFeedback] = useState('')
  const c = state.change
  return (
    <Card>
      <CardHeader>
        <CardTitle>Approve spec &amp; oracle</CardTitle>
        <CardDescription>
          Each delta spec, the plan and the oracle are separate decisions; all must be approved
          before build
          {c?.tier === 'R3' && '; R3 also needs an independent-baseline attestation first'}. Oracle
          commit <Sha sha={c?.oracle_sha} />
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Table>
          <TableBody>
            {state.h2.map((x) => (
              <TableRow key={x.subject}>
                <TableCell className="font-medium">{x.subject}</TableCell>
                <TableCell className="whitespace-normal">
                  {x.paths.map((p) => (
                    <Button
                      key={p}
                      variant="link"
                      className="block h-auto p-0 font-mono text-xs"
                      onClick={() => nav.openDoc(p)}
                    >
                      {p.split('/').slice(2).join('/')}
                    </Button>
                  ))}
                  {x.warnings?.map((w) => (
                    <p key={w} className="mt-1 text-xs text-destructive">
                      {w}
                    </p>
                  ))}
                </TableCell>
                <TableCell>
                  <span className="inline-flex items-center gap-1.5">
                    <StatusDot status={H2_DOT[x.status]} />
                    {x.status}
                    {x.approver && ` · ${x.approver}`}
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  {x.status !== 'approved' && (
                    <Button
                      size="sm"
                      onClick={() => act.node('H-2', 'approve', { subjects: [x.subject] })}
                    >
                      Approve
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <TextField
          label="Feedback"
          value={feedback}
          onChange={setFeedback}
          multiline
          description="For a revision."
        />
      </CardContent>
      <CardFooter className="flex gap-2">
        <Button onClick={() => act.node('H-2', 'approve', { subjects: 'all' }, 'Approved')}>
          Approve all pending
        </Button>
        <Button
          variant="outline"
          onClick={() => act.node('H-2', 'revise', { feedback }, 'Revision requested')}
        >
          Request revision
        </Button>
      </CardFooter>
    </Card>
  )
}

function ApproveEnvironment({ state }: { state: ChangeState }) {
  const act = useAct(useNav().cid)
  const [feedback, setFeedback] = useState('')
  return (
    <Card>
      <CardHeader>
        <CardTitle>Approve sandbox environment</CardTitle>
        <CardDescription>
          The controller then builds the images and adds the network policies to each sandbox's
          policy; flagged entries are proposed, not yet in the library.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <EnvironmentSpec env={state.environment} />
        <TextField label="Feedback / note" value={feedback} onChange={setFeedback} multiline />
      </CardContent>
      <CardFooter className="flex gap-2">
        <Button onClick={() => act.node('H-E1', 'approve', { feedback }, 'Environment approved')}>
          Approve
        </Button>
        <Button
          variant="outline"
          onClick={() => act.node('H-E1', 'revise', { feedback }, 'Revision requested')}
        >
          Request revision
        </Button>
      </CardFooter>
    </Card>
  )
}

/** Why the operator ends the change: recorded, and read by the retrospective. */
function AbandonReason({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <TextField
      label="Reason"
      value={value}
      onChange={onChange}
      multiline
      description="Recorded with the change and given to the retrospective (optional)."
    />
  )
}

function AcceptChange({ state }: { state: ChangeState }) {
  const act = useAct(useNav().cid)
  const [note, setNote] = useState('')
  const [why, setWhy] = useState('')
  return (
    <Card>
      <CardHeader>
        <CardTitle>Accept the whole change</CardTitle>
        <CardDescription>
          At integration head <Sha sha={state.change?.integration_head} />
        </CardDescription>
      </CardHeader>
      <CardContent>
        <TextField label="Note" value={note} onChange={setNote} multiline />
      </CardContent>
      <CardFooter className="flex gap-2">
        <Button onClick={() => act.node('H-3', 'accept', { feedback: note }, 'Change accepted')}>
          Accept change
        </Button>
        <ConfirmButton
          title="Reject the change?"
          description="Rejecting abandons the change. A retrospective then runs on what it produced."
          confirm="Reject"
          body={<AbandonReason value={why} onChange={setWhy} />}
          onConfirm={() => act.node('H-3', 'reject', { reason: why }, 'Change rejected')}
        >
          Reject
        </ConfirmButton>
      </CardFooter>
    </Card>
  )
}

/** The decision a node waits for, if any. */
export function NodeActions({ node, state }: { node: Node; state: ChangeState }) {
  const act = useAct(useNav().cid)
  if (node.status === 'ESCALATED') return <Escalated node={node} />
  if (node.kind === 'ship' && node.status === 'ACCEPTED')
    return (
      <Button
        variant="outline"
        onClick={() => act.node('ship', 'recheck', {}, 'Re-running the readiness check')}
      >
        Re-run readiness check
      </Button>
    )
  if (node.status !== 'AWAITING_HUMAN') return null
  if (node.id === 'H-1') return <AcceptRequirements state={state} />
  if (node.id === 'H-2') return <ApproveSpec state={state} />
  if (node.id === 'H-E1') return <ApproveEnvironment state={state} />
  if (node.id === 'H-3') return <AcceptChange state={state} />
  return null
}
