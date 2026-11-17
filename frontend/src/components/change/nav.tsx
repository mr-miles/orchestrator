import { createContext, useContext } from 'react'

export type TabId = 'overview' | 'agents' | 'gates' | 'plan' | 'documents' | 'workflow'

export const TABS: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'agents', label: 'Agents' },
  { id: 'gates', label: 'Gates' },
  { id: 'plan', label: 'Plan' },
  { id: 'documents', label: 'Documents' },
  { id: 'workflow', label: 'Workflow' },
]

export interface Nav {
  cid: string
  tab: TabId
  setTab: (t: TabId) => void
  /** Open a node's details (a sheet over the current tab). */
  openNode: (id: string) => void
  /** Open a run's agent log. */
  openLog: (run: string) => void
  /** Open a document (at ``version``, else the latest). */
  openDoc: (path: string, version?: number) => void
}

export const NavContext = createContext<Nav | null>(null)

export function useNav(): Nav {
  const n = useContext(NavContext)
  if (!n) throw new Error('useNav outside a change view')
  return n
}
