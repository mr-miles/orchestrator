import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useAct } from '@/hooks/use-act'
import type { ChangeState, GateResult } from '@/lib/types'
import { csv, options } from '@/lib/forms'
import { SelectField, TextField } from '../fields'
import { FormDialog } from '../form-dialog'
import { AttestationLink, Attestations, Muted, Sha, StatusDot, Verdict } from '../status'
import { useNav } from './nav'

const GATE_DOT: Record<GateResult['status'], string> = {
  satisfied: 'ACCEPTED',
  unsatisfied: 'ESCALATED',
  unevaluable: 'REVIEWING',
}

function ShipGates({ state }: { state: ChangeState }) {
  const c = state.change
  const res = c?.ship
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ship gates</CardTitle>
        <CardDescription>
          Tier {c?.tier}: {state.gates.length} gates.{' '}
          {res ? (
            <>
              Verdict at <Sha sha={res.integration_head} /> on <code>{res.integration_branch}</code>
            </>
          ) : (
            'Not evaluated yet (runs at the ship node).'
          )}
        </CardDescription>
        {res && (
          <CardAction>
            <Verdict ok={res.verdict === 'READY'} label={res.verdict} />
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="space-y-2">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Gate</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Detail</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {state.gates.map((g) => {
              const r = res?.gates[g]
              return (
                <TableRow key={g}>
                  <TableCell className="font-mono text-xs">{g}</TableCell>
                  <TableCell>
                    {r ? (
                      <span className="inline-flex items-center gap-1.5">
                        <StatusDot status={GATE_DOT[r.status]} />
                        {r.status}
                        {r.human_judgement && <Badge variant="secondary">human</Badge>}
                      </span>
                    ) : (
                      '—'
                    )}
                  </TableCell>
                  <TableCell className="whitespace-normal">{r?.detail}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
        {res && <Muted>{res.note}</Muted>}
      </CardContent>
    </Card>
  )
}

function AttestationsCard({ state }: { state: ChangeState }) {
  const { cid } = useNav()
  const act = useAct(cid)
  const kinds = state.attestation_kinds
  const tasks = state.nodes.flatMap((n) => (n.kind === 'build' && n.task ? [n.task.id] : []))
  const [kind, setKind] = useState(kinds[0] ?? '')
  const [verdict, setVerdict] = useState('pass')
  const [sources, setSources] = useState('')
  const [covers, setCovers] = useState('')
  const [waiver, setWaiver] = useState('')
  const atts = Object.values(state.change?.attestations ?? {})
  return (
    <Card>
      <CardHeader>
        <CardTitle>Attestations</CardTitle>
        <CardDescription>Human evidence for the gates that need judgement (G-AT1).</CardDescription>
        <CardAction>
          <FormDialog
            trigger="Record attestation"
            title="Record an attestation"
            submit="Record"
            onSubmit={() =>
              act.change(
                'attest',
                {
                  kind,
                  verdict,
                  sources: csv(sources),
                  covers: csv(covers).length ? csv(covers) : null,
                  waiver_reason: waiver || null,
                },
                'Attestation recorded',
              )
            }
          >
            <SelectField label="Kind" value={kind} onChange={setKind} options={options(kinds)} />
            <SelectField
              label="Verdict"
              value={verdict}
              onChange={setVerdict}
              options={options(['pass', 'fail', 'unknown', 'not_applicable'])}
            />
            <TextField
              label="Sources"
              value={sources}
              onChange={setSources}
              placeholder="file:… or git:…, comma separated"
              description="Relative paths are in this change."
            />
            <TextField
              label="Covers tasks (optional)"
              value={covers}
              onChange={setCovers}
              placeholder={tasks.join(', ')}
            />
            <TextField
              label="Waiver reason"
              value={waiver}
              onChange={setWaiver}
              description="Only for not_applicable."
            />
          </FormDialog>
        </CardAction>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Kind</TableHead>
              <TableHead>Verdict</TableHead>
              <TableHead>By</TableHead>
              <TableHead>Sources</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {atts.length ? (
              atts.map((a) => (
                <TableRow key={a.kind}>
                  <TableCell>{a.kind}</TableCell>
                  <TableCell>{a.verdict}</TableCell>
                  <TableCell>{a.attested_by}</TableCell>
                  <TableCell className="whitespace-normal">
                    <Attestations atts={a.sources} />
                    {a.warnings.map((w) => (
                      <p key={w} className="text-xs text-destructive">
                        {w}
                      </p>
                    ))}
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell colSpan={4} className="text-muted-foreground">
                  None
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function OverridesCard({ state }: { state: ChangeState }) {
  const { cid } = useNav()
  const act = useAct(cid)
  const [kind, setKind] = useState('add_gate')
  const [gate, setGate] = useState('')
  const [reason, setReason] = useState('')
  const [evidence, setEvidence] = useState('')
  const overrides = state.change?.overrides ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle>Gate overrides</CardTitle>
        <CardDescription>
          Add a gate to this change, or waive one with replacement evidence (G-OV1).
        </CardDescription>
        <CardAction>
          <FormDialog
            trigger="Override a gate"
            title="Override a gate"
            submit="Record override"
            variant="destructive"
            onSubmit={() =>
              act.change('override', { kind, gate, reason, evidence }, 'Override recorded')
            }
          >
            <SelectField
              label="Kind"
              value={kind}
              onChange={setKind}
              options={options(['add_gate', 'waive_gate'])}
            />
            <TextField label="Gate" value={gate} onChange={setGate} placeholder="gate id" />
            <TextField
              label="Reason"
              value={reason}
              onChange={setReason}
              description="At least 24 characters."
            />
            <TextField
              label="Evidence"
              value={evidence}
              onChange={setEvidence}
              placeholder="file:…@N"
              description="Replacement evidence (waive only)."
            />
          </FormDialog>
        </CardAction>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Override</TableHead>
              <TableHead>Gate</TableHead>
              <TableHead>By</TableHead>
              <TableHead>Reason / evidence</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {overrides.length ? (
              overrides.map((o, i) => (
                <TableRow key={i}>
                  <TableCell>{o.kind}</TableCell>
                  <TableCell className="font-mono text-xs">{o.gate}</TableCell>
                  <TableCell>{o.approver}</TableCell>
                  <TableCell className="whitespace-normal">
                    {o.reason} {o.evidence && <AttestationLink att={o.evidence} />}
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell colSpan={4} className="text-muted-foreground">
                  None
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function ProtectedPathsCard({ state }: { state: ChangeState }) {
  const { cid } = useNav()
  const act = useAct(cid)
  const tasks = state.nodes.flatMap((n) => (n.kind === 'build' && n.task ? [n.task.id] : []))
  const [task, setTask] = useState(tasks[0] ?? '')
  const [paths, setPaths] = useState('')
  const [reason, setReason] = useState('')
  const approvals = state.change?.protected_approvals ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle>Protected paths</CardTitle>
        <CardDescription>
          Permit a task to modify protected acceptance tests (only runs started after the approval).
        </CardDescription>
        <CardAction>
          <FormDialog
            trigger="Approve protected paths"
            title="Approve protected paths"
            submit="Approve"
            variant="destructive"
            onSubmit={() =>
              act.change('approve_protected_paths', { task, paths: csv(paths), reason }, 'Approved')
            }
          >
            <SelectField label="Task" value={task} onChange={setTask} options={options(tasks)} />
            <TextField
              label="Paths"
              value={paths}
              onChange={setPaths}
              placeholder="comma separated"
            />
            <TextField
              label="Reason"
              value={reason}
              onChange={setReason}
              description="At least 24 characters."
            />
          </FormDialog>
        </CardAction>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Task</TableHead>
              <TableHead>Paths</TableHead>
              <TableHead>By</TableHead>
              <TableHead>Reason</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {approvals.length ? (
              approvals.map((p, i) => (
                <TableRow key={i}>
                  <TableCell>{p.task}</TableCell>
                  <TableCell className="font-mono text-xs whitespace-normal">
                    {p.paths.join(', ')}
                  </TableCell>
                  <TableCell>
                    {p.approver} <Muted className="text-xs">{p.at}</Muted>
                  </TableCell>
                  <TableCell className="whitespace-normal">{p.reason}</TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell colSpan={4} className="text-muted-foreground">
                  None
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function Reaffirm({ surface }: { surface: string }) {
  const { cid } = useNav()
  const act = useAct(cid)
  const [reason, setReason] = useState('')
  return (
    <FormDialog
      trigger="Re-affirm"
      title={`Re-affirm surface ${surface}`}
      description="Why does this surface still describe the real interface?"
      submit="Re-affirm"
      onSubmit={() => act.change('approve_surface', { surface, reason }, 'Surface re-affirmed')}
    >
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        multiline
        description="At least 24 characters."
      />
    </FormDialog>
  )
}

function SurfacesCard({ state }: { state: ChangeState }) {
  const pins = state.change?.surface_pins ?? {}
  return (
    <Card>
      <CardHeader>
        <CardTitle>Verification surfaces</CardTitle>
        <CardDescription>
          Files pinned by the oracle's surfaces must not drift before ship (G-SF1).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Surface</TableHead>
              <TableHead>Commands</TableHead>
              <TableHead>Pins</TableHead>
              <TableHead>Pinned by</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {state.surfaces.length ? (
              state.surfaces.map((sf) => {
                const pin = pins[sf.id]
                return (
                  <TableRow key={sf.id}>
                    <TableCell className="whitespace-normal">
                      <b>{sf.id}</b>
                      <p className="text-xs text-muted-foreground">{sf.description}</p>
                    </TableCell>
                    <TableCell className="font-mono text-xs whitespace-normal">
                      {(sf.commands ?? []).join('\n')}
                    </TableCell>
                    <TableCell className="font-mono text-xs whitespace-normal">
                      {pin
                        ? Object.entries(pin.pins).map(([p, b]) => (
                            <div key={p}>
                              {p} {b ? b.slice(0, 8) : '(absent)'}
                            </div>
                          ))
                        : 'not pinned'}
                    </TableCell>
                    <TableCell className="space-y-1 whitespace-normal">
                      {pin && (
                        <p className="text-xs">
                          {pin.approver} · {pin.reason}
                        </p>
                      )}
                      <Reaffirm surface={sf.id} />
                    </TableCell>
                  </TableRow>
                )
              })
            ) : (
              <TableRow>
                <TableCell colSpan={4} className="text-muted-foreground">
                  No surfaces declared
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

export function GatesTab({ state }: { state: ChangeState }) {
  if (!state.change?.tier)
    return (
      <Empty className="border">
        <EmptyHeader>
          <EmptyTitle>Tier not assessed yet</EmptyTitle>
          <EmptyDescription>The ship gates follow from the tier accepted at H-1.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  return (
    <div className="flex flex-col gap-4">
      <ShipGates state={state} />
      <AttestationsCard state={state} />
      <OverridesCard state={state} />
      <ProtectedPathsCard state={state} />
      <SurfacesCard state={state} />
    </div>
  )
}
