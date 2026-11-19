import { BellIcon, InboxIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ago, useBrowserNotifications, useInboxRead } from '@/lib/attention'
import type { Attention, AttentionItem } from '@/lib/types'
import { cn } from '@/lib/utils'

function Item({
  item,
  unread,
  onOpen,
}: {
  item: AttentionItem
  unread?: boolean
  onOpen: (item: AttentionItem) => void
}) {
  const waited = ago(item.since)
  return (
    <DropdownMenuItem onClick={() => onOpen(item)} className="items-start gap-2">
      <span
        className={cn(
          'mt-1.5 size-2 shrink-0 rounded-full',
          item.blocking ? 'bg-destructive' : unread ? 'bg-primary' : 'bg-transparent',
        )}
      />
      <div className="min-w-0 flex-1">
        <p className={cn('truncate', (item.blocking || unread) && 'font-medium')}>{item.title}</p>
        <p className="truncate text-xs text-muted-foreground">
          <span className="font-mono">{item.change}</span>
          {waited && (item.blocking ? ` · waiting ${waited}` : ` · ${waited} ago`)} · {item.goal}
        </p>
        {item.reason && <p className="line-clamp-2 text-xs text-muted-foreground">{item.reason}</p>}
      </div>
    </DropdownMenuItem>
  )
}

/** The header's attention menu (WORKFLOW.md §2.2): what needs a decision across every change,
 * loud, and the inbox – ended changes and retrospective reports – with unread ones marked. */
export function AttentionMenu({
  attention,
  onOpen,
}: {
  attention: Attention | null
  onOpen: (item: AttentionItem) => void
}) {
  const { isRead, markRead } = useInboxRead()
  const needs = attention?.needs_you ?? []
  const inbox = attention?.inbox ?? []
  const unread = inbox.filter((i) => !isRead(i.key))
  const notify = useBrowserNotifications(attention ? needs : null, onOpen)
  const open = (item: AttentionItem) => {
    if (!item.blocking) markRead([item.key])
    onOpen(item)
  }
  const label = `Needs you: ${needs.length}; inbox: ${unread.length} unread`
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant={needs.length ? 'outline' : 'ghost'} className="gap-1.5" />}
        aria-label={label}
        title={label}
      >
        <BellIcon />
        {needs.length > 0 && (
          <Badge variant="destructive">
            {needs.length}
            <span className="hidden sm:inline">&nbsp;need you</span>
          </Badge>
        )}
        {unread.length > 0 && (
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            <InboxIcon className="size-3.5" />
            {unread.length}
          </span>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[28rem] max-w-[calc(100vw-2rem)]">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Needs you ({needs.length})</DropdownMenuLabel>
          {needs.length === 0 && (
            <DropdownMenuItem disabled>No decision is waiting for you</DropdownMenuItem>
          )}
          {needs.map((i) => (
            <Item key={i.key} item={i} onOpen={open} />
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLabel>
            Inbox{unread.length > 0 && ` (${unread.length} unread)`}
          </DropdownMenuLabel>
          {inbox.length === 0 && <DropdownMenuItem disabled>Nothing to read</DropdownMenuItem>}
          {inbox.map((i) => (
            <Item key={i.key} item={i} unread={!isRead(i.key)} onOpen={open} />
          ))}
          {unread.length > 0 && (
            <DropdownMenuItem onClick={() => markRead(unread.map((i) => i.key))}>
              Mark all read
            </DropdownMenuItem>
          )}
        </DropdownMenuGroup>
        {notify.supported && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuCheckboxItem
              checked={notify.enabled}
              disabled={notify.denied}
              onCheckedChange={(on) => void notify.setEnabled(on)}
            >
              {notify.denied
                ? 'Desktop notifications are blocked by the browser'
                : 'Desktop notifications while this page is open'}
            </DropdownMenuCheckboxItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
