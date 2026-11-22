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

/** What the workflow core offers on a node now (its generic actions). */
function offered(state: ChangeState, nodeId: string): string[] {
  return state.workflow?.actions[nodeId] ?? []
}

const SEND_BACK = 'send_back:'
// sending these back starts over everything built on them: confirm first
const SWEEPING = ['stage:requirements', 'stage:plan']

const RERUN = 'rerun:'

/** Run an earlier step again in place, as the workflow core offers it at an escalation
 * (provisioning: the approved spec built again against the current library); the escalated
 * step is then retried in its result. The answer is the reason (at least 24 characters). */
function Rerun({ state, nodeId, reason }: { state: ChangeState; nodeId: string; reason: string }) {
  const act = useAct(useNav().cid)
  const titles = new Map(state.nodes.map((n) => [n.id, n.title]))
  return (
    <>
      {offered(state, nodeId)
        .filter((a) => a.startsWith(RERUN))
        .map((a) => {
          const step = a.slice(RERUN.length)
          return (
            <Button
              key={a}
              variant="secondary"
              title="Builds the approved environment again against the current library (e.g. after a library entry was fixed), then retries this step in the new build. The answer is the reason."
              onClick={() => act.node(nodeId, a, { reason }, `${titles.get(step) ?? step} again`)}
            >
              {step.split('@')[0] === 'provision' ? 'Provision again' : `Run ${step} again`}
            </Button>
          )
        })}
    </>
  )
}

/** Send back each stage the node's work was built on, nearest first, as the workflow core
 * offers it. A stage sent back starts over with the feedback; Delivery's appends a fix round. */
function SendBack({
  state,
  nodeId,
  feedback,
}: {
  state: ChangeState
  nodeId: string
  feedback: string
}) {
  const act = useAct(useNav().cid)
  const titles = new Map((state.workflow?.stages ?? []).map((st) => [st.id, st.title]))
  const targets = offered(state, nodeId)
    .filter((a) => a.startsWith(SEND_BACK))
    .map((a) => a.slice(SEND_BACK.length))
  return (
    <>
      {targets.map((stage, i) => {
        const base = stage.split('@')[0] ?? stage
        const title = titles.get(stage) ?? stage
        const send = () => act.node(nodeId, 'send_back', { stage, feedback }, `${title} sent back`)
        if (base === 'stage:delivery')
          return (
            <Button
              key={stage}
              variant="secondary"
              title="A fix round plans new tasks on top of the integration head (critique, H-E1, the oracle if it changes, H-2), builds them and runs integration again. Accepted tasks are not reopened."
              onClick={send}
            >
              Plan a fix round
            </Button>
          )
        if (SWEEPING.includes(base))
          return (
            <ConfirmButton
              key={stage}
              title={`Send ${title} back?`}
              description="It starts over with your feedback, and everything built on it with it: every build task of every round, the integration branch, the oracle and the surface pins."
              confirm={`Send ${title} back`}
              disabled={!feedback.trim()}
              onConfirm={send}
            >
              Send back: {title}
            </ConfirmButton>
          )
        return (
          <Button
            key={stage}
            variant={i === 0 ? 'outline' : 'ghost'}
            disabled={!feedback.trim()}
            title={`${title} starts over with this feedback, and everything after it`}
            onClick={send}
          >
            Send back: {title}
          </Button>
        )
      })}
    </>
  )
}

function Escalated({ node, state }: { node: Node; state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [answer, setAnswer] = useState('')
  const [reason, setReason] = useState('')
  const [why, setWhy] = useState('')
  const e = node.escalation
  if (!e) return null
  const actions = offered(state, node.id)
  const override = actions.includes('override')
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
        {e.proposal && (
          <Alert>
            <AlertTitle>
              Resolver proposes: <code>{e.proposal.action}</code>
            </AlertTitle>
            <AlertDescription className="space-y-2">
              <p className="whitespace-pre-wrap">{e.proposal.rationale}</p>
              {Object.keys(e.proposal.payload).length > 0 && (
                <Pre>{JSON.stringify(e.proposal.payload, null, 2)}</Pre>
              )}
              {e.proposal.attestations.length > 0 && (
                <p className="font-mono text-xs">{e.proposal.attestations.join(' · ')}</p>
              )}
              <div className="flex flex-wrap gap-2">
                {e.proposal.action === 'abandon' ? (
                  <ConfirmButton
                    title="Abandon the change?"
                    description="The resolver proposes abandoning the change."
                    confirm="Abandon"
                    onConfirm={() =>
                      act.node(node.id, 'abandon', e.proposal?.payload ?? {}, 'Change abandoned')
                    }
                  >
                    Apply proposal
                  </ConfirmButton>
                ) : (
                  <Button
                    size="sm"
                    onClick={() =>
                      act.node(
                        node.id,
                        e.proposal?.action ?? '',
                        e.proposal?.payload ?? {},
                        "Applied the resolver's proposal",
                      )
                    }
                  >
                    Apply proposal
                  </Button>
                )}
                {typeof e.proposal.payload.answer === 'string' && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setAnswer(String(e.proposal?.payload.answer ?? ''))}
                  >
                    Edit as my answer
                  </Button>
                )}
              </div>
            </AlertDescription>
          </Alert>
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
            description="Attested as feedback for the next run – or, sending a stage back, what it is told to change (for a fix round, the comment the planner is given)."
          />
          {override && (
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
        {override && (
          <Button
            variant="outline"
            onClick={() => act.node(node.id, 'override_accept', { reason })}
          >
            Override accept
          </Button>
        )}
        <Rerun state={state} nodeId={node.id} reason={answer} />
        <SendBack state={state} nodeId={node.id} feedback={answer} />
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
        <SendBack state={state} nodeId="H-1" feedback={feedback} />
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

/** H-E1 "approve the plan" (spec and task graph, each delta spec, the environment) and H-2
 * "approve the oracle" (the oracle; at R3 the baseline attestation, its precondition): each
 * subject is a separate decision, possibly by different approvers. */
function ApproveSubjects({ state, nodeId }: { state: ChangeState; nodeId: string }) {
  const gate = baseId(nodeId)
  const oracle = gate === 'H-2'
  const nav = useNav()
  const act = useAct(nav.cid)
  const [feedback, setFeedback] = useState('')
  const c = state.change
  const rows = state.h2.filter((x) => (x.gate ?? 'H-2') === gate)
  const approvals = rows.filter((x) => x.kind !== 'attestation')
  const baseline = rows.filter((x) => x.kind === 'attestation')
  const attestsToo = baseline.some((x) => x.status !== 'approved' && !x.note)
  return (
    <Card>
      <CardHeader>
        <CardTitle>{oracle ? 'Approve the oracle' : 'Approve the plan'}</CardTitle>
        <CardDescription>
          {oracle ? (
            <>
              The protected acceptance tests
              {c?.tier === 'R3' && ', with the baseline evidence attested'}, before any task is
              built. Oracle commit <Sha sha={c?.oracle_sha} />
            </>
          ) : (
            <>
              The spec and task graph, each delta spec
              {state.environment && ' and the sandbox environment'} are separate decisions; the
              oracle and the tasks are built on them.
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {oracle ? <TraceSummary state={state} /> : <EnvironmentSpec env={state.environment} />}
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
                      onClick={() => act.node(nodeId, 'approve', { subjects: [x.subject] })}
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
            onAttest={() => act.node(nodeId, 'approve', { subjects: [x.subject] }, 'Attested')}
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
        <Button onClick={() => act.node(nodeId, 'approve', { subjects: 'all' }, 'Approved')}>
          Approve all pending
        </Button>
        {attestsToo && <Muted>Also attests: {GATE_TITLE.independent_baseline}</Muted>}
        <SendBack state={state} nodeId={nodeId} feedback={feedback} />
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

/** H-3's preconditions, the ship gates: what is not satisfied and why; the attestations only a
 * human can give, recorded with the recheck in one step. */
function ShipReadiness({ state, nodeId }: { state: ChangeState; nodeId: string }) {
  const act = useAct(useNav().cid)
  const [drafts, setDrafts] = useState<Record<string, AttestDraft>>({})
  const res = state.change?.ship
  const bad = Object.entries(res?.gates ?? {}).filter(([, g]) => g.status !== 'satisfied')
  const gates = (state.h3 ?? []).filter((g) => g.status !== 'attested')
  const todo = pendingAttestations(gates, drafts)
  const rerun = async () => {
    for (const t of todo) if (!(await act.change('attest', { ...t.payload }))) return
    await act.node(nodeId, 'recheck', {}, 'Re-checking the ship gates')
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
          Re-check the ship gates
        </Button>
        {todo.length > 0 && <Muted>Also attests: {alsoRecords(todo)}</Muted>}
      </CardFooter>
    </Card>
  )
}

/** A node id without its fix round (``H-2@2`` -> ``H-2``). */
function baseId(id: string): string {
  return id.split('@')[0] ?? id
}

/** The decision a node waits for, if any. */
export function NodeActions({ node, state }: { node: Node; state: ChangeState }) {
  if (node.status === 'ESCALATED') return <Escalated node={node} state={state} />
  if (node.status !== 'AWAITING_HUMAN') return null
  const gate = baseId(node.id)
  if (gate === 'H-1') return <AcceptRequirements state={state} />
  if (gate === 'H-E1' || gate === 'H-2') return <ApproveSubjects state={state} nodeId={node.id} />
  if (gate === 'H-3') {
    // accepted (or, at R0/R1, automatic): what holds it is its preconditions, the ship gates
    const tier = state.change?.tier ?? 'R1'
    const accepted = Boolean(state.change?.approvals?.['H-3']) || tier === 'R0' || tier === 'R1'
    return accepted ? (
      <ShipReadiness state={state} nodeId={node.id} />
    ) : (
      <AcceptChange state={state} />
    )
  }
  return null
}
