/** The UI's pages and their addresses (the hash, so a link or reload comes back to them).
 *
 *   #/work                                     every change, by workspace or all together
 *   #/inbox                                    what needs you, and the inbox
 *   #/ws/<ws>[/<tab>]                          a workspace: its changes, documents, repository
 *   #/ws/<ws>/c/<change>/<tab>[/<ticket>]      a change (a ticket opens its sheet, once)
 *   #/config/<group>                           skills, workflows or agents: their definitions
 *
 * ``#<change>/<tab>[/<ticket>]`` – the address before workspaces (notification links) – opens the
 * change in its workspace. */
import { TABS, type TabId } from '@/components/change/nav'

export const WORKSPACE_TABS = ['changes', 'documents', 'repository'] as const
export type WorkspaceTab = (typeof WORKSPACE_TABS)[number]
export const CONFIG_GROUPS = ['skills', 'workflows', 'agents'] as const
export type ConfigGroup = (typeof CONFIG_GROUPS)[number]

/** A ticket to open once; ``n`` changes on every request. */
export interface Focus {
  ticket: string
  n: number
}

export type Route =
  | { page: 'work' }
  | { page: 'inbox' }
  | { page: 'workspace'; ws: string; tab: WorkspaceTab }
  | { page: 'change'; ws: string; cid: string; tab: TabId; focus: Focus | null }
  | { page: 'config'; group: ConfigGroup }

let focusCount = 0
export const nextFocus = (ticket?: string | null): Focus | null =>
  ticket ? { ticket, n: ++focusCount } : null

const tabOf = (t?: string): TabId => TABS.find((x) => x.id === t)?.id ?? 'overview'

/** The route of a hash. ``ws`` is '' for an old-style change link (its workspace is not in it). */
export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#/, '')
  const parts = raw.split('/').map((p) => decodeURIComponent(p))
  if (!raw.startsWith('/')) {
    const [cid, tab, ticket] = parts
    return cid
      ? { page: 'change', ws: '', cid, tab: tabOf(tab), focus: nextFocus(ticket) }
      : { page: 'work' }
  }
  const [, head, a, b, c, d, e] = parts
  if (head === 'inbox') return { page: 'inbox' }
  if (head === 'config' && CONFIG_GROUPS.includes(a as ConfigGroup))
    return { page: 'config', group: a as ConfigGroup }
  if (head === 'ws' && a) {
    if (b === 'c' && c) return { page: 'change', ws: a, cid: c, tab: tabOf(d), focus: nextFocus(e) }
    const tab = WORKSPACE_TABS.includes(b as WorkspaceTab) ? (b as WorkspaceTab) : 'changes'
    return { page: 'workspace', ws: a, tab }
  }
  return { page: 'work' }
}

const enc = encodeURIComponent

/** The hash of a route (a change's ticket is not kept: it opens once). */
export function formatHash(r: Route): string {
  switch (r.page) {
    case 'work':
      return '#/work'
    case 'inbox':
      return '#/inbox'
    case 'config':
      return `#/config/${r.group}`
    case 'workspace':
      return `#/ws/${enc(r.ws)}${r.tab === 'changes' ? '' : `/${r.tab}`}`
    case 'change':
      return `#/ws/${enc(r.ws)}/c/${enc(r.cid)}/${r.tab}`
  }
}

/** The same page, regardless of the ticket to open (an old-style change link, whose workspace is
 * not known yet, is the same page as that change in any workspace). */
export function samePage(a: Route, b: Route): boolean {
  if (a.page === 'change' && b.page === 'change' && (!a.ws || !b.ws))
    return a.cid === b.cid && a.tab === b.tab
  return formatHash(a) === formatHash(b)
}

/** Where a change opens (``ws``: its workspace when the change does not say). */
export const changeHref = (c: { id: string; workspace?: string }, ws: string) =>
  formatHash({ page: 'change', ws: c.workspace ?? ws, cid: c.id, tab: 'overview', focus: null })
