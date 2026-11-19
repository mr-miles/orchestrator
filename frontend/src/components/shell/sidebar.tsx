import {
  BotIcon,
  BriefcaseIcon,
  FolderGitIcon,
  InboxIcon,
  SparklesIcon,
  WorkflowIcon,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { useInboxRead } from '@/lib/attention'
import { formatHash, type Route } from '@/lib/route'
import type { Attention, ChangeSummary, Workspace } from '@/lib/types'
import { cn } from '@/lib/utils'

function Item({
  href,
  icon: Icon,
  label,
  active,
  depth = 0,
  onClick,
  children,
}: {
  href: string
  icon: LucideIcon
  label: string
  active: boolean
  depth?: number
  onClick?: () => void
  children?: ReactNode
}) {
  return (
    <li>
      <a
        href={href}
        aria-current={active ? 'page' : undefined}
        onClick={onClick}
        className={cn(
          'flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted',
          active && 'bg-muted font-medium text-foreground',
          !active && 'text-muted-foreground',
          depth > 0 && 'pl-8',
        )}
      >
        <Icon className="size-4 shrink-0" />
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {children}
      </a>
    </li>
  )
}

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <h2 className="px-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </h2>
      <ul className="space-y-0.5">{children}</ul>
    </div>
  )
}

/** The left-hand menu: workloads (work, by workspace, and the inbox), then configuration. */
export function AppSidebar({
  route,
  project,
  workspaces,
  changes,
  attention,
  onNavigate,
}: {
  route: Route
  project: string
  workspaces: Workspace[]
  changes: ChangeSummary[]
  attention: Attention | null
  onNavigate?: () => void
}) {
  const { isRead } = useInboxRead()
  const needs = attention?.needs_you.length ?? 0
  const unread = (attention?.inbox ?? []).filter((i) => !isRead(i.key)).length
  const active = changes.filter((c) => c.status === 'ACTIVE').length
  const ws = route.page === 'workspace' || route.page === 'change' ? route.ws : null
  return (
    <nav aria-label="Main" className="flex h-full flex-col gap-6 p-3">
      <a href="#/work" className="px-2" onClick={onNavigate}>
        <span className="font-heading font-semibold">Legion</span>
        {project && <span className="block truncate text-xs text-muted-foreground">{project}</span>}
      </a>
      <Group label="Workloads">
        <Item
          href={formatHash({ page: 'work' })}
          icon={BriefcaseIcon}
          label="Work"
          onClick={onNavigate}
          active={route.page === 'work'}
        >
          {active > 0 && (
            <Badge variant="secondary" title={`${active} active`}>
              {active}
            </Badge>
          )}
        </Item>
        {workspaces.map((w) => (
          <Item
            key={w.id}
            href={formatHash({ page: 'workspace', ws: w.id, tab: 'changes' })}
            icon={FolderGitIcon}
            label={w.name || w.id}
            onClick={onNavigate}
            active={ws === w.id}
            depth={1}
          />
        ))}
        <Item
          href={formatHash({ page: 'inbox' })}
          icon={InboxIcon}
          label="Inbox"
          onClick={onNavigate}
          active={route.page === 'inbox'}
        >
          {needs > 0 && (
            <Badge variant="destructive" title={`${needs} need you`}>
              {needs}
            </Badge>
          )}
          {unread > 0 && (
            <Badge variant="secondary" title={`${unread} unread`}>
              {unread}
            </Badge>
          )}
        </Item>
      </Group>
      <Group label="Configuration">
        <Item
          href={formatHash({ page: 'config', group: 'skills' })}
          icon={SparklesIcon}
          label="Skills"
          onClick={onNavigate}
          active={route.page === 'config' && route.group === 'skills'}
        />
        <Item
          href={formatHash({ page: 'config', group: 'workflows' })}
          icon={WorkflowIcon}
          label="Workflows"
          onClick={onNavigate}
          active={route.page === 'config' && route.group === 'workflows'}
        />
        <Item
          href={formatHash({ page: 'config', group: 'agents' })}
          icon={BotIcon}
          label="Agents"
          onClick={onNavigate}
          active={route.page === 'config' && route.group === 'agents'}
        />
      </Group>
    </nav>
  )
}
