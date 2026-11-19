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
import { alsoRecords, GATE_TITLE, pendingAttestations, type AttestDraft } from '@/lib/attest'
import type { ChangeState, H2Subject, Node } from '@/lib/types'
import { ConfirmButton } from '../confirm-button'
import { SelectField, TextField } from '../fields'
import { Muted, Pre, Sha, SourceBadge, StatusDot } from '../status'
import { EnvironmentSpec } from './environment'
import { AttestationRow, GateAttestationRow } from './attestation-row'
import { useNav } from './nav'
import { AcceptancePreview, TraceSummary } from './traceability'

function Escalated({ node }: { node: Node }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [answer, setAnswer] = useState('')
  const [reason, setReason] = useState('')
  const [why, setWhy] = useState('')
  const e = node.escalation
  if (!e) return null
  // a late integration failure is fixed by a fix round: new tasks on top of the head (§5.10a)
  const planFix = node.kind === 'integration' && e.kind === 'integration_failed'
  // before H-2, an oracle or baseline the plan cannot satisfy goes back to the planner
  const revisePlan = node.kind === 'oracle' || node.kind === 'baseline'
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
            description={
              planFix
                ? 'Attested as feedback for the next run; for Plan a fix, the comment the planner is given.'
                : revisePlan
                  ? 'Attested as feedback for the next run; for Revise the plan, what the planner is told to change.'
                  : 'Attested as feedback for the next run.'
            }
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
        {revisePlan && (
          <Button
            variant="secondary"
            disabled={!answer.trim()}
            title="Sends the plan back to the planner with this feedback, as Request revision does at H-2: the plan is critiqued again, then the oracle and baseline start over"
            onClick={() =>
              act.node(
                node.id,
                'revise_plan',
                { feedback: answer },
                'Plan sent back to the planner',
              )
            }
          >
            Revise the plan
          </Button>
        )}
        {planFix && (
          <Button
            variant="secondary"
            title="The planner plans new tasks on top of the integration head: critique, H-2, build and integration again. Accepted tasks are not reopened."
            onClick={() => act.node(node.id, 'plan_fix', { comment: answer }, 'Fix round planned')}
          >
            Plan a fix
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

/** The baseline's evidence: the controller's run of the oracle commands before any build. */
function BaselineSummary({ row }: { row: H2Subject }) {
  if (row.note) return <Muted>{row.note}</Muted>
  const results = row.results ?? []
  const passing = results.filter((r) => r.exit_code === 0).length
  return (
    <div className="space-y-1">
      <p>
        The oracle commands, run by the controller before any build: {passing} of {results.length}{' '}
        already pass (each should fail until the change is built).
      </p>
      <ul className="font-mono text-xs">
        {results.map((r) => (
          <li key={r.command} className={r.exit_code === 0 ? 'text-destructive' : ''}>
            {r.exit_code === 0 ? '✓' : '✗'} {r.command} → exit {r.exit_code}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ApproveSpec({ state }: { state: ChangeState }) {
  const round = state.change?.round ?? 1
  const nav = useNav()
  const act = useAct(nav.cid)
  const [feedback, setFeedback] = useState('')
  const c = state.change
  const approvals = state.h2.filter((x) => x.kind !== 'attestation')
  const baseline = state.h2.filter((x) => x.kind === 'attestation')
  const attestsToo = baseline.some((x) => x.status !== 'approved' && !x.note)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Approve spec &amp; oracle</CardTitle>
        <CardDescription>
          Each delta spec, the plan and the oracle are separate decisions
          {c?.tier === 'R3' && ', and the baseline evidence is attested'}; all must be done before
          build. Oracle commit <Sha sha={c?.oracle_sha} />
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <TraceSummary state={state} />
        <Table>
          <TableBody>
            {approvals.map((x) => (
              <TableRow key={x.subject}>
                <TableCell className="font-medium">{x.subject}</TableCell>
                <TableCell className="whitespace-normal">
                  {x.paths.map((p) => {
                    const was = x.approved?.[p]
                    const changed = x.status === 'stale' && was != null && was !== x.latest?.[p]
                    return (
                      <div key={p} className="flex flex-wrap items-baseline gap-x-2">
                        <Button
                          variant="link"
                          className="h-auto p-0 font-mono text-xs"
                          onClick={() => nav.openDoc(p)}
                        >
                          {p.split('/').slice(2).join('/')}
                        </Button>
                        {changed && (
                          <Button
                            variant="link"
                            className="h-auto p-0 text-xs text-destructive"
                            onClick={() => nav.openDiff(p, was)}
                          >
                            changed since v{was} was approved
                          </Button>
                        )}
                      </div>
                    )
                  })}
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
        {baseline.map((x) => (
          <AttestationRow
            key={x.subject}
            gate="independent_baseline"
            status={x.status === 'approved' ? 'attested' : x.status}
            by={x.approver}
            summary={<BaselineSummary row={x} />}
            sources={(x.sources ?? []).map((att) => ({
              label: att.includes('baseline/report.md') ? 'baseline report' : "controller's run",
              att,
            }))}
            disabled={Boolean(x.note)}
            onAttest={() => act.node('H-2', 'approve', { subjects: [x.subject] }, 'Attested')}
          />
        ))}
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
        {attestsToo && <Muted>Also attests: {GATE_TITLE.independent_baseline}</Muted>}
        <Button
          variant="outline"
          title={
            round > 1
              ? `Sends fix round ${round} back to the planner; earlier rounds stand`
              : undefined
          }
          onClick={() => act.node('H-2', 'revise', { feedback }, 'Revision requested')}
        >
          {round > 1 ? `Revise round ${round}` : 'Request revision'}
        </Button>
        {round > 1 && (
          <ConfirmButton
            title="Restart the whole plan?"
            description="Every build task of every round, the integration branch, the oracle and the surface pins start over."
            confirm="Restart plan"
            onConfirm={() => act.node('H-2', 'restart', { feedback }, 'Plan restarted')}
          >
            Restart plan
          </ConfirmButton>
        )}
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
  const [drafts, setDrafts] = useState<Record<string, AttestDraft>>({})
  const gates = state.h3 ?? []
  const todo = pendingAttestations(gates, drafts)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Accept the whole change</CardTitle>
        <CardDescription>
          At integration head <Sha sha={state.change?.integration_head} />
          {gates.length > 0 && '. Some gates need your attestation.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <TraceSummary state={state} />
        <AcceptancePreview state={state} />
        {gates.map((g) => (
          <GateAttestationRow
            key={g.gate}
            gate={g}
            draft={drafts[g.gate]}
            onChange={(d) => setDrafts({ ...drafts, [g.gate]: d })}
            onAttest={(a) => act.change('attest', { ...a }, 'Attested')}
          />
        ))}
        <TextField label="Note" value={note} onChange={setNote} multiline />
      </CardContent>
      <CardFooter className="flex flex-wrap items-center gap-2">
        <Button
          onClick={() =>
            act.node(
              'H-3',
              'accept',
              { feedback: note, attestations: todo.map((t) => t.payload) },
              'Change accepted',
            )
          }
        >
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
        {todo.length > 0 && <Muted>Also attests: {alsoRecords(todo)}</Muted>}
      </CardFooter>
    </Card>
  )
}

/** The ship check's verdict. NOT_READY: the gates not satisfied and why; the attestations only
 * a human can give, recorded with the re-run in one step. */
function ShipReadiness({ state }: { state: ChangeState }) {
  const act = useAct(useNav().cid)
  const [drafts, setDrafts] = useState<Record<string, AttestDraft>>({})
  const res = state.change?.ship
  const bad = Object.entries(res?.gates ?? {}).filter(([, g]) => g.status !== 'satisfied')
  const gates = (state.h3 ?? []).filter((g) => g.status !== 'attested')
  const todo = pendingAttestations(gates, drafts)
  const rerun = async () => {
    for (const t of todo) if (!(await act.change('attest', { ...t.payload }))) return
    await act.node('ship', 'recheck', {}, 'Re-running the readiness check')
  }
  return (
    <Card className={res?.verdict === 'NOT_READY' ? 'ring-destructive/40' : undefined}>
      <CardHeader>
        <CardTitle>{res?.verdict === 'READY' ? 'Ready to ship' : 'Not ready to ship'}</CardTitle>
        <CardDescription>
          {res ? (
            <>
              Verdict at <Sha sha={res.integration_head} />:{' '}
              {bad.length
                ? `${bad.length} of ${Object.keys(res.gates).length} gates not satisfied.`
                : 'every gate is satisfied.'}
            </>
          ) : (
            'Not evaluated yet.'
          )}
        </CardDescription>
      </CardHeader>
      {(bad.length > 0 || gates.length > 0) && (
        <CardContent className="space-y-3">
          <ul className="space-y-1 text-sm">
            {bad.map(([g, r]) => (
              <li key={g}>
                <StatusDot status={r.status === 'unsatisfied' ? 'ESCALATED' : 'REVIEWING'} />{' '}
                <code>{g}</code> {r.status}: {r.detail}
              </li>
            ))}
          </ul>
          {gates.map((g) => (
            <GateAttestationRow
              key={g.gate}
              gate={g}
              draft={drafts[g.gate]}
              onChange={(d) => setDrafts({ ...drafts, [g.gate]: d })}
              onAttest={(a) => act.change('attest', { ...a }, 'Attested')}
            />
          ))}
          {bad.some(([g]) => !gates.some((x) => x.gate === g)) && (
            <Muted>
              Other gates need their evidence first (Gates tab: attestations, surfaces, protected
              paths, overrides), then a re-run.
            </Muted>
          )}
        </CardContent>
      )}
      <CardFooter className="flex flex-wrap items-center gap-2">
        <Button variant={bad.length ? 'default' : 'outline'} onClick={rerun}>
          Re-run readiness check
        </Button>
        {todo.length > 0 && <Muted>Also attests: {alsoRecords(todo)}</Muted>}
      </CardFooter>
    </Card>
  )
}

/** The decision a node waits for, if any. */
export function NodeActions({ node, state }: { node: Node; state: ChangeState }) {
  if (node.status === 'ESCALATED') return <Escalated node={node} />
  if (node.kind === 'ship' && (node.status === 'ACCEPTED' || node.status === 'FAILED'))
    return <ShipReadiness state={state} />
  if (node.status !== 'AWAITING_HUMAN') return null
  if (node.id === 'H-1') return <AcceptRequirements state={state} />
  if (node.id === 'H-2') return <ApproveSpec state={state} />
  if (node.id === 'H-E1') return <ApproveEnvironment state={state} />
  if (node.id === 'H-3') return <AcceptChange state={state} />
  return null
}
