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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { isLive } from '@/lib/status'
import type { ChangeState, Finding, Node, Run } from '@/lib/types'
import {
  Attestations,
  AttestationLink,
  Failure,
  Muted,
  Pre,
  Sha,
  StageBadge,
  StatusBadge,
  Verdict,
} from '../status'
import { AgentQuestions } from './a2a'
import { EnvironmentBuild, EnvironmentSpec } from './environment'
import { useNav } from './nav'
import { NodeActions } from './node-actions'

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

const VERIFICATION_LABEL: Record<string, string> = {
  integration: 'Integration',
  preflight: 'Preflight',
  baseline: 'Baseline (controller)',
}

const SEVERITY: Record<Finding['severity'], 'destructive' | 'secondary' | 'outline'> = {
  BLOCKER: 'destructive',
  WARNING: 'secondary',
  SUGGESTION: 'outline',
}

function Findings({ findings }: { findings: Finding[] }) {
  if (!findings.length) return <Muted>No findings.</Muted>
  return (
    <div className="space-y-2">
      {findings.map((f, i) => (
        <div key={i} className="space-y-1 rounded-lg border p-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={SEVERITY[f.severity]}>{f.severity}</Badge>
            <Badge variant="outline">{f.confidence}</Badge>
            <b>{f.title}</b>
          </div>
          {f.observation && <p>{f.observation}</p>}
          {f.impact && <p className="text-muted-foreground">Impact: {f.impact}</p>}
          {f.fix && <p>Fix: {f.fix}</p>}
          <Attestations atts={f.attestations} />
        </div>
      ))}
    </div>
  )
}

/** Opens a log (an agent run's, a verification's) in the log sheet. */
function LogButton({ log }: { log: string }) {
  const nav = useNav()
  return (
    <Button variant="outline" size="sm" onClick={() => nav.openLog(log)}>
      <ScrollTextIcon data-icon="inline-start" />
      Log
    </Button>
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
            {run.role} · cycle {run.cycle} · {run.soul}
          </span>
        </CardTitle>
        <CardDescription className="font-mono text-xs">
          sandbox {run.sandbox}
          {run.sandbox_deleted && ' (deleted)'} ·{' '}
          {run.branch
            ? `branch ${run.branch}`
            : `detached at ${(run.judged_sha ?? run.base_sha ?? '').slice(0, 12)}`}{' '}
          · assets {(run.assets_sha ?? '').slice(7, 19)} · prompt{' '}
          {(run.prompt_pin ?? '').slice(7, 19)}
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

function Details({ node, state }: { node: Node; state: ChangeState }) {
  const nav = useNav()
  const rows: [string, React.ReactNode][] = [
    [
      'id',
      <>
        <code>{node.id}</code> · {node.kind}
        {node.wave ? ` · wave ${node.wave}` : ''}
        {node.kind === 'build' && (node.round ?? 1) > 1 ? ` · fix round ${node.round}` : ''}
      </>,
    ],
    [
      'depends on',
      node.depends_on.length
        ? node.depends_on.map((d, i) => (
            <span key={d}>
              {i > 0 && ', '}
              <Button variant="link" className="h-auto p-0" onClick={() => nav.openNode(d)}>
                {d}
              </Button>
            </span>
          ))
        : '—',
    ],
    ['cycle', `${node.cycle}${node.extra_cycles ? ` (+${node.extra_cycles} granted)` : ''}`],
  ]
  if (node.base_sha) rows.push(['base', <Sha key="base" sha={node.base_sha} />])
  if (node.candidate_sha) rows.push(['candidate', <Sha key="candidate" sha={node.candidate_sha} />])
  if (node.accepted_sha) rows.push(['accepted', <Sha key="accepted" sha={node.accepted_sha} />])
  if (node.sandbox)
    rows.push([
      'sandbox',
      <code key="sandbox" className="font-mono text-xs">
        {node.sandbox}
      </code>,
    ])
  return (
    <div className="space-y-4">
      <KV rows={rows} />
      <AgentQuestions state={state} node={node.id} />
      {node.kind === 'plan' && state.environment && (
        <section className="space-y-2">
          <h3 className="font-medium">Environment spec</h3>
          <EnvironmentSpec env={state.environment} />
        </section>
      )}
      {node.kind === 'provision' && (
        <section className="space-y-2">
          <h3 className="font-medium">Provisioned environment</h3>
          <EnvironmentBuild change={state.change} />
        </section>
      )}
      {node.task && (
        <Collapsible>
          <CollapsibleTrigger render={<Button variant="ghost" size="sm" />}>
            Task contract
          </CollapsibleTrigger>
          <CollapsibleContent>
            <Pre>{JSON.stringify(node.task, null, 2)}</Pre>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  )
}

function NodeBody({ node, state }: { node: Node; state: ChangeState }) {
  const runs = state.runs.filter((r) => r.node_id === node.id).reverse()
  const reviews = Object.entries(node.reviews ?? {})
  const verification = Object.entries(node.verification ?? {})
  const verifying = state.verifying?.[node.id]
  return (
    <div className="space-y-4">
      <NodeActions node={node} state={state} />
      <Tabs defaultValue="details">
        <TabsList variant="line">
          <TabsTrigger value="details">Details</TabsTrigger>
          {reviews.length > 0 && (
            <TabsTrigger value="reviews">Reviews ({reviews.length})</TabsTrigger>
          )}
          {(verification.length > 0 || verifying) && (
            <TabsTrigger value="verification">Verification</TabsTrigger>
          )}
          {node.feedback.length > 0 && (
            <TabsTrigger value="feedback">Feedback ({node.feedback.length})</TabsTrigger>
          )}
          {runs.length > 0 && <TabsTrigger value="runs">Runs ({runs.length})</TabsTrigger>}
        </TabsList>
        <TabsContent value="details" className="pt-2">
          <Details node={node} state={state} />
        </TabsContent>
        <TabsContent value="reviews" className="space-y-3 pt-2">
          {reviews.map(([k, r]) => (
            <Card key={k} size="sm">
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-2">
                  {k} <Verdict ok={r.verdict === 'PASS'} label={r.verdict} />
                  {r.sha && <Sha sha={r.sha} />}
                </CardTitle>
                {r.doc && (
                  <CardDescription>
                    <AttestationLink att={r.doc} />
                  </CardDescription>
                )}
              </CardHeader>
              <CardContent>
                <Findings findings={r.findings} />
              </CardContent>
            </Card>
          ))}
        </TabsContent>
        <TabsContent value="verification" className="space-y-3 pt-2">
          {verifying && (
            <Card size="sm">
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-2">
                  Verifying <StatusBadge status="VERIFYING" />
                  {node.candidate_sha && <Sha sha={node.candidate_sha} />}
                </CardTitle>
                <CardDescription className="font-mono text-xs">sandbox {verifying}</CardDescription>
                <CardAction>
                  <LogButton log={verifying} />
                </CardAction>
              </CardHeader>
            </Card>
          )}
          {verification.map(([k, v]) => (
            <Card key={k} size="sm">
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-2">
                  {VERIFICATION_LABEL[k] ?? `Cycle ${k}`} <Verdict ok={v.ok} /> <Sha sha={v.sha} />
                  {v.cached && (
                    <Badge variant="outline" title={`verification ${v.run ?? ''}`}>
                      verified already
                    </Badge>
                  )}
                </CardTitle>
                {v.infra_error && (
                  <CardDescription className="text-destructive">{v.infra_error}</CardDescription>
                )}
                {v.log && (
                  <CardAction>
                    <LogButton log={v.log} />
                  </CardAction>
                )}
              </CardHeader>
              <CardContent className="space-y-1 font-mono text-xs">
                {v.results.map((r, i) => (
                  <p key={i}>
                    {r.exit_code === 0 ? '✓' : '✗'}{' '}
                    {r.setup && <Badge variant="outline">setup</Badge>} {r.command} → {r.exit_code}
                  </p>
                ))}
                {v.attestation && <AttestationLink att={v.attestation} />}
              </CardContent>
            </Card>
          ))}
        </TabsContent>
        <TabsContent value="feedback" className="space-y-2 pt-2">
          {node.feedback.map((f, i) => (
            <div key={i} className="space-y-1 rounded-lg border p-3 text-sm">
              <div className="flex gap-2">
                <Badge variant="outline">cycle {f.cycle}</Badge>
                <Badge variant="secondary">{f.source}</Badge>
              </div>
              <p className="whitespace-pre-wrap">{f.text}</p>
              <Attestations atts={f.attestations} />
            </div>
          ))}
        </TabsContent>
        <TabsContent value="runs" className="space-y-3 pt-2">
          {runs.map((r) => (
            <RunCard key={r.id} run={r} />
          ))}
        </TabsContent>
      </Tabs>
    </div>
  )
}

/** A node's details, decisions and runs, over the current tab: a sheet, or for a human gate a
 * wide dialog, so the decision's evidence (subjects, matrix, diff, gates) fits without scrolling
 * sideways. */
export function NodeSheet({
  state,
  nodeId,
  onClose,
}: {
  state: ChangeState
  nodeId: string | null
  onClose: () => void
}) {
  const node = state.nodes.find((n) => n.id === nodeId)
  const title = node && (
    <>
      {node.title} <StatusBadge status={node.status} />
    </>
  )
  if (node?.kind === 'human')
    return (
      <Dialog open onOpenChange={(o) => !o && onClose()}>
        <DialogContent className="flex max-h-[92vh] flex-col gap-0 p-0 sm:max-w-[min(80rem,calc(100%-4rem))]">
          <DialogHeader className="border-b p-4">
            <DialogTitle className="flex flex-wrap items-center gap-2 pr-8">{title}</DialogTitle>
            <DialogDescription>
              {node.kind} · {node.id}
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <NodeBody node={node} state={state} />
          </div>
        </DialogContent>
      </Dialog>
    )
  return (
    <Sheet open={Boolean(node)} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-2xl">
        {node && (
          <>
            <SheetHeader className="border-b">
              <SheetTitle className="flex flex-wrap items-center gap-2 pr-8">{title}</SheetTitle>
              <SheetDescription>
                {node.kind} · {node.id}
              </SheetDescription>
            </SheetHeader>
            <ScrollArea className="min-h-0 flex-1">
              <div className="p-4">
                <NodeBody node={node} state={state} />
              </div>
            </ScrollArea>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
