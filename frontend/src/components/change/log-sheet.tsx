import { useEffect, useId, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import { useFetch } from '@/hooks/use-fetch'
import { getRawLog } from '@/lib/api'
import { useLogStream } from '@/lib/streams'
import type { LogEntry, LogKind } from '@/lib/types'
import { cn } from '@/lib/utils'
import { Failure, Pre, SourceBadge, StageBadge, StatusBadge } from '../status'

const KIND: Record<LogKind, string> = {
  sandbox: 'sandbox',
  init: 'agent start',
  text: 'agent',
  prompt: 'input',
  thinking: 'thinking',
  tool_call: 'tool',
  tool_result: 'result',
  result: 'final result',
  waiting: 'turn ended, waiting',
  task: 'background task',
  event: 'event',
}

const BORDER: Partial<Record<LogKind, string>> = {
  init: 'border-cyan-500',
  text: 'border-green-500',
  tool_call: 'border-blue-500',
  result: 'border-green-500 bg-muted/40',
  waiting: 'border-amber-500 bg-muted/40',
  task: 'border-purple-500',
}

const MONO: LogKind[] = ['sandbox', 'tool_call', 'tool_result']

function Entry({ e }: { e: LogEntry }) {
  const head = (
    <span className="mr-2 text-[10px] tracking-wide text-muted-foreground uppercase">
      {KIND[e.kind] ?? e.kind}
      {e.title && (
        <b className="ml-2 text-xs tracking-normal text-foreground normal-case">{e.title}</b>
      )}
    </span>
  )
  const body = e.text && (
    <div
      className={cn(
        'break-words whitespace-pre-wrap',
        MONO.includes(e.kind) && 'font-mono text-xs',
        e.kind === 'tool_result' && 'text-muted-foreground',
        e.kind === 'thinking' && 'text-muted-foreground italic',
        e.error && 'text-destructive',
      )}
    >
      {e.text}
    </div>
  )
  return (
    <div
      className={cn(
        'border-l-[3px] border-border py-1 pl-2.5 text-sm',
        BORDER[e.kind],
        e.error && 'border-destructive',
      )}
    >
      {e.kind === 'tool_result' && e.text.length > 300 ? (
        <details>
          <summary className="cursor-pointer">
            {head}
            <span className="text-xs text-muted-foreground">{e.text.slice(0, 120)}…</span>
          </summary>
          {body}
        </details>
      ) : (
        <>
          {head}
          {body}
        </>
      )}
    </div>
  )
}

/** A run's agent log, live while it runs; it follows the end unless scrolled up. */
export function LogSheet({ run, onClose }: { run: string | null; onClose: () => void }) {
  const log = useLogStream(run)
  const [raw, setRaw] = useState(false)
  const rawId = useId()
  const box = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  const rawLog = useFetch(
    run && raw ? () => getRawLog(run) : null,
    `${run}:${raw}:${log.entries.length}`,
  )
  useEffect(() => {
    const el = box.current
    if (el && follow.current) el.scrollTop = el.scrollHeight
  }, [log.entries, rawLog.data])
  const u = log.update
  return (
    <Sheet open={Boolean(run)} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-4xl">
        <SheetHeader className="border-b">
          <SheetTitle className="flex flex-wrap items-center gap-2 pr-8">
            Agent log <span className="font-mono">{run}</span>
            {u && <StatusBadge status={u.status} />}
            {u?.live && <StageBadge stage={u.stage} />}
            {u?.failure && <SourceBadge source={u.failure.source} />}
            {u?.live && (
              <Badge variant="secondary" className="gap-1">
                <Spinner className="size-3" /> live
              </Badge>
            )}
          </SheetTitle>
          <SheetDescription render={<div />} className="flex items-center gap-2">
            <Switch id={rawId} checked={raw} onCheckedChange={setRaw} />
            <Label htmlFor={rawId} className="font-normal">
              Raw output
            </Label>
          </SheetDescription>
          {u?.failure && <Failure failure={u.failure} />}
        </SheetHeader>
        <div
          ref={box}
          className="min-h-0 flex-1 space-y-1 overflow-auto p-4"
          onScroll={(e) => {
            const el = e.currentTarget
            follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
          }}
        >
          {raw ? (
            <Pre className="max-h-none">{rawLog.data || '(empty)'}</Pre>
          ) : log.entries.length ? (
            log.entries.map((e, i) => <Entry key={i} e={e} />)
          ) : (
            <p className="text-sm text-muted-foreground">{u ? '(empty)' : 'Loading…'}</p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
