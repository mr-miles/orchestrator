import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { ago, useInboxRead } from '@/lib/attention'
import type { Attention, AttentionItem } from '@/lib/types'
import { cn } from '@/lib/utils'
import { Muted } from '../status'

function ItemRow({
  item,
  unread,
  onOpen,
}: {
  item: AttentionItem
  unread?: boolean
  onOpen: (i: AttentionItem) => void
}) {
  const waited = ago(item.since)
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(item)}
        className="flex w-full items-start gap-3 rounded-md p-2 text-left hover:bg-muted"
      >
        <span
          className={cn(
            'mt-1.5 size-2 shrink-0 rounded-full',
            item.blocking ? 'bg-destructive' : unread ? 'bg-primary' : 'bg-transparent',
          )}
        />
        <span className="min-w-0 flex-1">
          <span className={cn('block', (item.blocking || unread) && 'font-medium')}>
            {item.title}
          </span>
          <span className="block text-xs text-muted-foreground">
            <span className="font-mono">{item.change}</span>
            {waited && (item.blocking ? ` · waiting ${waited}` : ` · ${waited} ago`)} · {item.goal}
          </span>
          {item.reason && (
            <span className="block text-xs whitespace-pre-wrap text-muted-foreground">
              {item.reason}
            </span>
          )}
        </span>
      </button>
    </li>
  )
}

/** What needs a decision across every change (blocking), and the inbox to read. */
export function InboxPage({
  attention,
  onOpen,
}: {
  attention: Attention | null
  onOpen: (i: AttentionItem) => void
}) {
  const { isRead, markRead } = useInboxRead()
  const needs = attention?.needs_you ?? []
  const inbox = attention?.inbox ?? []
  const unread = inbox.filter((i) => !isRead(i.key))
  const open = (i: AttentionItem) => {
    if (!i.blocking) markRead([i.key])
    onOpen(i)
  }
  return (
    <div className="space-y-4">
      <div>
        <h1 className="font-heading text-xl font-semibold">Inbox</h1>
        <p className="text-sm text-muted-foreground">
          Decisions waiting for you first; then what is worth reading.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Needs you</CardTitle>
          <CardDescription>
            {needs.length
              ? `${needs.length} waiting for a decision`
              : 'Nothing is waiting for you.'}
          </CardDescription>
        </CardHeader>
        {needs.length > 0 && (
          <CardContent>
            <ul aria-label="Needs you">
              {needs.map((i) => (
                <ItemRow key={i.key} item={i} onOpen={open} />
              ))}
            </ul>
          </CardContent>
        )}
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Inbox</CardTitle>
          <CardDescription>
            {inbox.length ? `${unread.length} unread of ${inbox.length}` : 'Nothing to read.'}
          </CardDescription>
          {unread.length > 0 && (
            <CardAction>
              <Button
                variant="outline"
                size="sm"
                onClick={() => markRead(unread.map((i) => i.key))}
              >
                Mark all read
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <CardContent>
          {inbox.length ? (
            <ul aria-label="Inbox">
              {inbox.map((i) => (
                <ItemRow key={i.key} item={i} unread={!isRead(i.key)} onOpen={open} />
              ))}
            </ul>
          ) : (
            <Muted>Ended changes and retrospective reports appear here.</Muted>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
