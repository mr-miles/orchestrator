import { useFetch } from '@/hooks/use-fetch'
import { getTrace } from './api'
import type { ChangeState, Traceability } from './types'

/** A cheap fingerprint of the change's documents moving on: views that fetch documents or
 * computed records (the Documents tab, the traceability matrix) refresh on it. */
export function docsVersion(state: ChangeState): string {
  const nodes = state.nodes.map((n) => `${n.status}${n.cycle}`).join()
  return `${state.runs.length}:${nodes}:${state.h2.map((s) => s.status).join()}`
}

/** The change's traceability matrix, fetched again whenever its documents may have moved on. */
export function useTrace(cid: string, state: ChangeState) {
  return useFetch(() => getTrace(cid), `${cid}:${docsVersion(state)}`)
}

export function summaryLine(t: Traceability) {
  const s = t.summary
  const n = (k: number, one: string) => `${k} ${one}${k === 1 ? '' : 's'}`
  return [
    n(s.requirements, 'requirement'),
    n(s.tasks, 'task'),
    n(s.criteria, 'criterion').replace('criterions', 'criteria'),
    n(s.warnings, 'warning'),
  ].join(' · ')
}
