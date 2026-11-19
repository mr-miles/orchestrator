import { XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useFetch } from '@/hooks/use-fetch'
import { getDigest } from '@/lib/api'
import { ago, useLastSeen } from '@/lib/attention'
import type { AttentionItem } from '@/lib/types'

/** "While you were away": what happened on every change since this browser last looked (when
 * that was over half an hour ago), counted from the controller's timelines – no model call.
 * What waits for a decision is listed, never folded into a count. */
export function AwayDigest({ onOpen }: { onOpen: (item: AttentionItem) => void }) {
  const { since, dismiss } = useLastSeen()
  const { data } = useFetch(since ? () => getDigest(since) : null, since ?? '')
  if (!since || !data || (data.changes.length === 0 && data.needs_you.length === 0)) return null
  return (
    <Card className="mb-4" size="sm">
      <CardHeader>
        <CardTitle>While you were away</CardTitle>
        <CardDescription>Since {ago(since)} ago</CardDescription>
        <CardAction>
          <Button variant="ghost" size="icon-sm" aria-label="Dismiss" onClick={dismiss}>
            <XIcon />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {data.changes.map((c) => (
          <p key={c.id}>
            <span className="font-mono">{c.id}</span>{' '}
            <span className="text-muted-foreground">{c.goal}</span>
            {': '}
            {Object.entries(c.counts)
              .map(([label, n]) => `${n} ${label}`)
              .join(', ') || 'no notable events'}
          </p>
        ))}
        {data.needs_you.length > 0 && (
          <div>
            <p className="font-medium text-destructive">Waiting for a decision</p>
            {data.needs_you.map((i) => (
              <Button
                key={i.key}
                variant="link"
                className="block h-auto p-0"
                onClick={() => onOpen(i)}
              >
                {i.change}: {i.title}
                {i.since && ` (waiting ${ago(i.since)})`}
              </Button>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
