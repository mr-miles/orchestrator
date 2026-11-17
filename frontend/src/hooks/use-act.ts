import { toast } from 'sonner'
import { changeAction, nodeAction } from '@/lib/api'
import { useSettings } from '@/lib/settings'
import type { ActionResult } from '@/lib/types'

/** Human decisions: a node action or a change-level action, as the configured approver. The
 * outcome is a toast; the new state arrives over the change's event stream. */
export function useAct(cid: string) {
  const who = useSettings()
  const run = async (fn: () => Promise<ActionResult>, done?: string): Promise<boolean> => {
    if (!who.approver.trim()) {
      toast.error('Set your approver id first (Settings in the top right).')
      return false
    }
    try {
      const r = await fn()
      if (r.warnings?.length) r.warnings.forEach((w) => toast.warning(w))
      else if (r.note) toast.info(r.note)
      else if (done) toast.success(done)
      return true
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
      return false
    }
  }
  return {
    node: (node: string, action: string, payload: Record<string, unknown> = {}, done?: string) =>
      run(() => nodeAction(cid, node, action, payload, who), done),
    change: (action: string, payload: Record<string, unknown> = {}, done?: string) =>
      run(() => changeAction(cid, action, payload, who), done),
  }
}
