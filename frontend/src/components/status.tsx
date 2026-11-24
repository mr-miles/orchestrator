import { Badge } from '@/components/ui/badge'
import { parseFileAttestation } from '@/lib/api'
import { humanize, shortSha, SOURCE_TEXT, STAGE_TEXT, statusColor } from '@/lib/status'
import type { FailureSource, RunFailure, RunStage } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useNav } from './change/nav'

export function StatusDot({ status, className }: { status: string; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn('inline-block size-2 shrink-0 rounded-full', className)}
      style={{ backgroundColor: statusColor(status) }}
    />
  )
}

/** A ticket, run or change status. */
export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return (
    <Badge variant="outline" className="gap-1.5 font-medium">
      <StatusDot status={status} />
      {label ?? humanize(status)}
    </Badge>
  )
}

export function StageBadge({ stage }: { stage?: RunStage | null }) {
  return stage ? <StatusBadge status={stage} label={STAGE_TEXT[stage]} /> : null
}

export function SourceBadge({ source }: { source?: FailureSource | null }) {
  if (!source) return null
  return (
    <Badge variant={source === 'AGENT' ? 'secondary' : 'destructive'}>{SOURCE_TEXT[source]}</Badge>
  )
}

export function Verdict({ ok, label }: { ok: boolean; label?: string }) {
  return (
    <Badge variant={ok ? 'secondary' : 'destructive'} className="font-semibold">
      <StatusDot status={ok ? 'ACCEPTED' : 'ESCALATED'} />
      {label ?? (ok ? 'PASS' : 'FAIL')}
    </Badge>
  )
}

export function Sha({ sha }: { sha?: string | null }) {
  return (
    <code className="font-mono text-xs" title={sha ?? undefined}>
      {shortSha(sha)}
    </code>
  )
}

export function Failure({ failure }: { failure?: RunFailure | null }) {
  if (!failure) return null
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <SourceBadge source={failure.source} />
        {failure.stage && <span>while {STAGE_TEXT[failure.stage]}</span>}
        {failure.exit_code != null && <span>exit {failure.exit_code}</span>}
      </div>
      <p className={cn('text-sm', failure.source !== 'AGENT' && 'text-destructive')}>
        {failure.reason}
      </p>
    </div>
  )
}

/** An attestation: a ``file:`` one opens the document at that version. */
export function AttestationLink({ att, className }: { att: string; className?: string }) {
  const nav = useNav()
  const file = parseFileAttestation(att)
  const cls = cn('font-mono text-xs break-all', className)
  if (!file) return <code className={cls}>{att}</code>
  return (
    <button
      type="button"
      className={cn(cls, 'text-left text-primary underline-offset-4 hover:underline')}
      onClick={() => nav.openDoc(file.path, file.version)}
    >
      {att}
    </button>
  )
}

export function Attestations({ atts }: { atts?: string[] }) {
  if (!atts?.length) return null
  return (
    <div className="flex flex-col gap-0.5">
      {atts.map((a) => (
        <AttestationLink key={a} att={a} />
      ))}
    </div>
  )
}

export function Muted({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn('text-sm text-muted-foreground', className)}>{children}</span>
}

/** Preformatted agent or controller text (logs, JSON, documents). */
export function Pre({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <pre
      className={cn(
        'max-h-[28rem] overflow-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs break-words whitespace-pre-wrap',
        className,
      )}
    >
      {children}
    </pre>
  )
}
