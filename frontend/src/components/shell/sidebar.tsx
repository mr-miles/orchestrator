import {
  BotIcon,
  BriefcaseIcon,
  FolderGitIcon,
  InboxIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  SparklesIcon,
  WorkflowIcon,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useInboxRead } from '@/lib/attention'
import { formatHash, type Route } from '@/lib/route'
import type { Attention, ChangeSummary, Workspace } from '@/lib/types'
import { cn } from '@/lib/utils'

/** A menu entry. Collapsed: its icon only, named by its tooltip; its badges become a dot. */
function Item({
  href,
  icon: Icon,
  label,
  active,
  depth = 0,
  collapsed = false,
  dot,
  onClick,
  children,
}: {
  href: string
  icon: LucideIcon
  label: string
  active: boolean
  depth?: number
  collapsed?: boolean
  /** collapsed: the dot standing in for the badges (none: no dot) */
  dot?: 'destructive' | 'secondary'
  onClick?: () => void
  children?: ReactNode
}) {
  return (
    <li>
      <a
        href={href}
        aria-current={active ? 'page' : undefined}
        title={collapsed ? label : undefined}
        onClick={onClick}
        className={cn(
          'relative flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted',
          active && 'bg-muted font-medium text-foreground',
          !active && 'text-muted-foreground',
          depth > 0 && !collapsed && 'pl-8',
          collapsed && 'justify-center',
        )}
      >
        <Icon className="size-4 shrink-0" />
        <span className={cn('min-w-0 flex-1 truncate', collapsed && 'sr-only')}>{label}</span>
        {!collapsed && children}
        {collapsed && dot && (
          <span
            aria-hidden
            className={cn(
              'absolute top-1 right-1 size-2 rounded-full',
              dot === 'destructive' ? 'bg-destructive' : 'bg-primary',
            )}
          />
        )}
      </a>
    </li>
  )
}

function Group({
  label,
  collapsed,
  children,
}: {
  label: string
  collapsed: boolean
  children: ReactNode
}) {
  return (
    <div className="space-y-1">
      <h2
        className={cn(
          'px-2 text-xs font-medium tracking-wide text-muted-foreground uppercase',
          collapsed && 'sr-only',
        )}
      >
        {label}
      </h2>
      {collapsed && <hr className="mx-2 border-border" />}
      <ul className="space-y-0.5">{children}</ul>
    </div>
  )
}

/** The left-hand menu: workloads (work, by workspace, and the inbox), then configuration.
 * With ``onToggle`` it can collapse to its icons (``collapsed``). */
export function AppSidebar({
  route,
  project,
  workspaces,
  changes,
  attention,
  onNavigate,
  collapsed = false,
  onToggle,
}: {
  route: Route
  project: string
  workspaces: Workspace[]
  changes: ChangeSummary[]
  attention: Attention | null
  onNavigate?: () => void
  collapsed?: boolean
  onToggle?: () => void
}) {
  const { isRead } = useInboxRead()
  const needs = attention?.needs_you.length ?? 0
  const unread = (attention?.inbox ?? []).filter((i) => !isRead(i.key)).length
  const active = changes.filter((c) => c.status === 'ACTIVE').length
  const ws = route.page === 'workspace' || route.page === 'change' ? route.ws : null
  return (
    <nav aria-label="Main" className={cn('flex h-full flex-col gap-6', collapsed ? 'p-2' : 'p-3')}>
      <div className={cn('flex items-start gap-1', collapsed && 'flex-col items-center')}>
        <a
          href="#/work"
          className={cn('min-w-0 flex-1', collapsed ? 'px-0 text-center' : 'px-2')}
          title={collapsed ? `Legion${project ? ` – ${project}` : ''}` : undefined}
          onClick={onNavigate}
        >
          <span className="font-heading font-semibold">{collapsed ? 'L' : 'Legion'}</span>
          {project && !collapsed && (
            <span className="block truncate text-xs text-muted-foreground">{project}</span>
          )}
        </a>
        {onToggle && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onToggle}
            aria-label={collapsed ? 'Expand the menu' : 'Collapse the menu'}
            aria-expanded={!collapsed}
            title={collapsed ? 'Expand the menu' : 'Collapse the menu'}
          >
            {collapsed ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />}
          </Button>
        )}
      </div>
      <Group label="Workloads" collapsed={collapsed}>
        <Item
          href={formatHash({ page: 'work' })}
          icon={BriefcaseIcon}
          label="Work"
          onClick={onNavigate}
          active={route.page === 'work'}
          collapsed={collapsed}
          dot={active > 0 ? 'secondary' : undefined}
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
            collapsed={collapsed}
          />
        ))}
        <Item
          href={formatHash({ page: 'inbox' })}
          icon={InboxIcon}
          label="Inbox"
          onClick={onNavigate}
          active={route.page === 'inbox'}
          collapsed={collapsed}
          dot={needs > 0 ? 'destructive' : unread > 0 ? 'secondary' : undefined}
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
      <Group label="Configuration" collapsed={collapsed}>
        <Item
          href={formatHash({ page: 'config', group: 'skills' })}
          icon={SparklesIcon}
          label="Skills"
          onClick={onNavigate}
          active={route.page === 'config' && route.group === 'skills'}
          collapsed={collapsed}
        />
        <Item
          href={formatHash({ page: 'config', group: 'workflows' })}
          icon={WorkflowIcon}
          label="Workflows"
          onClick={onNavigate}
          active={route.page === 'config' && route.group === 'workflows'}
          collapsed={collapsed}
        />
        <Item
          href={formatHash({ page: 'config', group: 'agents' })}
          icon={BotIcon}
          label="Agents"
          onClick={onNavigate}
          active={route.page === 'config' && route.group === 'agents'}
          collapsed={collapsed}
        />
      </Group>
    </nav>
  )
}
