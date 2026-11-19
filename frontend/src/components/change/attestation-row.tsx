import { useId, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { FieldGroup } from '@/components/ui/field'
import { Label } from '@/components/ui/label'
import {
  attestationOf,
  draftFor,
  gateStatus,
  GATE_TITLE,
  kindLabel,
  type AttestDraft,
  type AttestPayload,
  type AttestStatus,
} from '@/lib/attest'
import type { GateAttestation } from '@/lib/types'
import { SelectField, TextField } from '../fields'
import { AttestationLink, Muted, StatusDot } from '../status'

const DOT: Record<AttestStatus, string> = {
  pending: 'REVIEWING',
  stale: 'ESCALATED',
  attested: 'ACCEPTED',
  not_applicable: 'ACCEPTED',
  failed: 'ESCALATED',
}

function statusText(status: AttestStatus, by?: string | null, kind?: string | null) {
  const who = by ? ` by ${by}` : ''
  switch (status) {
    case 'pending':
      return 'needs you'
    case 'stale':
      return 'stale – attest again'
    case 'attested':
      return `attested${who}${kind ? ` (${kindLabel(kind).toLowerCase()})` : ''}`
    case 'not_applicable':
      return `not applicable${who}`
    case 'failed':
      return `failed${who}`
  }
}

/** The one layout of an attestation on a gate card: title and gate id, status, a summary of the
 * evidence and its sources, the row's own inputs, and an Attest button that records it now. */
export function AttestationRow({
  gate,
  title,
  status,
  by,
  kind,
  summary,
  sources,
  children,
  onAttest,
  disabled,
}: {
  gate: string
  title?: string
  status: AttestStatus
  by?: string | null
  kind?: string | null
  summary?: ReactNode
  sources: { label: string; att: string }[]
  children?: ReactNode
  onAttest: () => void
  disabled?: boolean
}) {
  const open = status === 'pending' || status === 'stale'
  return (
    <section className="space-y-2 rounded-md border p-3" aria-label={title ?? GATE_TITLE[gate]}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <h4 className="text-sm font-medium">{title ?? GATE_TITLE[gate] ?? gate}</h4>
          <p className="font-mono text-xs text-muted-foreground">{gate}</p>
        </div>
        <span className="inline-flex items-center gap-1.5 text-xs">
          <StatusDot status={DOT[status]} />
          {statusText(status, by, kind)}
        </span>
        {open && (
          <Button size="sm" disabled={disabled} onClick={onAttest}>
            Attest
          </Button>
        )}
      </div>
      {summary && <div className="text-sm">{summary}</div>}
      {sources.length > 0 && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {sources.map((s) => (
            <li key={s.att} className="inline-flex items-baseline gap-1.5">
              <span className="text-muted-foreground">{s.label}:</span>
              <AttestationLink att={s.att} />
            </li>
          ))}
        </ul>
      )}
      {open && children}
    </section>
  )
}

/** An H-3 / ship gate only a human attestation satisfies. A recommendation (the release planner's
 * recovery strategy) is attested as it stands: attesting confirms it was read. Otherwise the kind
 * and the evidence ticked – or, since nothing specific backs it, not applicable with a reason. */
export function GateAttestationRow({
  gate,
  draft,
  onChange,
  onAttest,
}: {
  gate: GateAttestation
  draft?: AttestDraft
  onChange: (d: AttestDraft) => void
  onAttest: (payload: AttestPayload) => void
}) {
  const id = useId()
  const d = draftFor(gate, draft)
  const payload = attestationOf(gate, d)
  const status = gateStatus(gate)
  const recommended = gate.recommended && (
    <p>
      Recommended: <b>{kindLabel(gate.recommended)}</b>
      {gate.rationale && <span className="text-muted-foreground"> – {gate.rationale}</span>}
    </p>
  )
  const pickSources = !gate.recommended && !(gate.waivable && d.na)
  return (
    <AttestationRow
      gate={gate.gate}
      status={status}
      by={gate.attested_by}
      kind={gate.kind}
      summary={recommended || (!gate.suggested.length && <Muted>Nothing on record to cite.</Muted>)}
      sources={
        pickSources
          ? []
          : gate.waivable && d.na
            ? (gate.waiver_sources ?? []).map((att) => ({ label: 'recorded against', att }))
            : gate.suggested
      }
      disabled={!payload}
      onAttest={() => payload && onAttest(payload)}
    >
      {!gate.recommended && (
        <FieldGroup>
          <div className="grid gap-3 sm:grid-cols-2">
            <SelectField
              label="Kind"
              value={d.kind}
              onChange={(kind) => onChange({ ...d, kind })}
              options={gate.kinds.map((k) => ({ value: k, label: kindLabel(k) }))}
            />
            {gate.waivable && (
              <SelectField
                label="Verdict"
                value={d.na ? 'not_applicable' : 'pass'}
                onChange={(v) => onChange({ ...d, na: v === 'not_applicable' })}
                options={[
                  { value: 'pass', label: 'Pass' },
                  { value: 'not_applicable', label: 'Not applicable' },
                ]}
              />
            )}
          </div>
          {gate.waivable && d.na ? (
            <TextField
              label="Reason"
              value={d.reason}
              onChange={(reason) => onChange({ ...d, reason })}
              multiline
              description="Why it does not apply (at least 24 characters); recorded against the source above."
            />
          ) : (
            <>
              {gate.suggested.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-sm font-medium">Evidence</p>
                  {gate.suggested.map((s, i) => (
                    <div key={s.att} className="flex items-center gap-2 text-sm">
                      <Checkbox
                        id={`${id}-${i}`}
                        checked={!d.off.includes(s.att)}
                        onCheckedChange={(v) =>
                          onChange({
                            ...d,
                            off: v ? d.off.filter((a) => a !== s.att) : [...d.off, s.att],
                          })
                        }
                      />
                      <Label htmlFor={`${id}-${i}`} className="font-normal">
                        {s.label}
                      </Label>
                      <AttestationLink att={s.att} />
                    </div>
                  ))}
                </div>
              )}
              <TextField
                label="Other sources"
                value={d.extra}
                onChange={(extra) => onChange({ ...d, extra })}
                placeholder="file:changes/<id>/…@1, git:<repo>@<sha>"
                description="Comma-separated attestations, e.g. your own evaluation notes."
              />
            </>
          )}
        </FieldGroup>
      )}
    </AttestationRow>
  )
}
