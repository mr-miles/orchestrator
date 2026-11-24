import { createContext, useContext } from 'react'

export type TabId = 'overview' | 'agents' | 'documents' | 'workflow'

export const TABS: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Tickets' },
  { id: 'agents', label: 'Agents' },
  { id: 'documents', label: 'Documents' },
  { id: 'workflow', label: 'Workflow' },
]

export interface Nav {
  cid: string
  tab: TabId
  setTab: (t: TabId) => void
  /** Open a ticket's details and decisions (a sheet over the current tab). */
  openTicket: (id: string) => void
  /** Open a run's agent log. */
  openLog: (run: string) => void
  /** Open a document (at ``version``, else the latest). */
  openDoc: (path: string, version?: number) => void
  /** Open a document's diff from ``from`` (the version signed off) to the latest. */
  openDiff: (path: string, from: number) => void
}

export const NavContext = createContext<Nav | null>(null)

export function useNav(): Nav {
  const n = useContext(NavContext)
  if (!n) throw new Error('useNav outside a change view')
  return n
}
