/** Attestations decided on the gate cards (H-2 baseline, H-3 and ship): one vocabulary, and
 * what each row would record once it is complete. */
import { csv } from './forms'
import type { GateAttestation } from './types'

/** Plain titles for the gates a human attests. */
export const GATE_TITLE: Record<string, string> = {
  independent_baseline: 'Baseline evidence',
  rollback_or_forward_fix_evidence: 'Recovery plan',
  security_or_e2e_evaluator: 'Security or end-to-end evaluation',
}

/** Plain names for attestation kinds. */
export const KIND_LABEL: Record<string, string> = {
  'independent-baseline': 'Baseline',
  'security-evaluation': 'Security evaluation',
  'e2e-evaluation': 'End-to-end evaluation',
  'rollback-evidence': 'Roll back',
  'forward-fix-evidence': 'Fix forward',
}

export const kindLabel = (k?: string | null) => (k ? (KIND_LABEL[k] ?? k) : '')

/** A row's status, in the words every gate card uses. */
export type AttestStatus = 'pending' | 'stale' | 'attested' | 'not_applicable' | 'failed'

export function gateStatus(g: GateAttestation): AttestStatus {
  if (g.status === 'failed') return 'failed'
  if (g.status === 'attested') return g.verdict === 'not_applicable' ? 'not_applicable' : 'attested'
  return 'pending'
}

/** What the human has filled in on a row so far. */
export interface AttestDraft {
  kind: string
  /** suggested sources unticked */
  off: string[]
  /** further sources, comma-separated */
  extra: string
  /** rows without specific evidence: attested not applicable, with a reason */
  na: boolean
  reason: string
}

export interface AttestPayload {
  kind: string
  verdict: 'pass' | 'not_applicable'
  sources: string[]
  waiver_reason?: string
}

export const draftFor = (g: GateAttestation, d?: AttestDraft): AttestDraft =>
  d ?? { kind: g.recommended ?? g.kinds[0] ?? '', off: [], extra: '', na: false, reason: '' }

/** What a row attests once it is complete, else null (left for later). A recommendation (the
 * release planner's recovery strategy) is complete as it stands: confirming is the decision. */
export function attestationOf(g: GateAttestation, d: AttestDraft): AttestPayload | null {
  const all = g.suggested.map((s) => s.att)
  if (g.recommended) return { kind: g.recommended, verdict: 'pass', sources: all }
  if (g.waivable && d.na) {
    const sources = g.waiver_sources ?? []
    return d.reason.trim() && sources.length
      ? { kind: d.kind, verdict: 'not_applicable', sources, waiver_reason: d.reason.trim() }
      : null
  }
  const sources = [...all.filter((a) => !d.off.includes(a)), ...csv(d.extra)]
  return sources.length ? { kind: d.kind, verdict: 'pass', sources } : null
}

/** The complete rows still to attest, with their titles (for "also records …"). */
export function pendingAttestations(
  gates: GateAttestation[],
  drafts: Record<string, AttestDraft>,
): { gate: string; payload: AttestPayload }[] {
  return gates
    .filter((g) => g.status !== 'attested')
    .flatMap((g) => {
      const payload = attestationOf(g, draftFor(g, drafts[g.gate]))
      return payload ? [{ gate: g.gate, payload }] : []
    })
}

/** "Also records: Recovery plan (fix forward), …" for a card's footer. */
export function alsoRecords(items: { gate: string; payload: AttestPayload }[]): string {
  return items
    .map(({ gate, payload }) => {
      const what = payload.verdict === 'not_applicable' ? 'not applicable' : kindLabel(payload.kind)
      return `${GATE_TITLE[gate] ?? gate} (${what.toLowerCase()})`
    })
    .join(', ')
}
