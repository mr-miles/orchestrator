import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { useFetch } from '@/hooks/use-fetch'
import { getDoc, getDocDiff } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Pre } from '../status'

export interface DocRef {
  path: string
  version?: number
  /** Show the diff from this version (the one approved) to ``version`` or the latest. */
  diffFrom?: number
}

const DIFF_LINE = (l: string) =>
  l.startsWith('+++') || l.startsWith('---')
    ? 'text-muted-foreground'
    : l.startsWith('+')
      ? 'bg-green-500/10 text-green-800 dark:text-green-400'
      : l.startsWith('-')
        ? 'bg-red-500/10 text-red-800 dark:text-red-400'
        : l.startsWith('@@')
          ? 'text-sky-700 dark:text-sky-400'
          : ''

/** A unified diff, one coloured line per line. */
export function Diff({ text }: { text: string }) {
  if (!text) return <p className="text-sm text-muted-foreground">No changes.</p>
  return (
    <pre className="rounded-md border bg-muted/40 p-3 font-mono text-xs whitespace-pre-wrap">
      {text.split('\n').map((l, i) => (
        <div key={i} className={cn(DIFF_LINE(l))}>
          {l || ' '}
        </div>
      ))}
    </pre>
  )
}

/** A document version: its metadata, the other versions and the content. */
export function DocSheet({
  doc,
  onOpen,
  onClose,
}: {
  doc: DocRef | null
  onOpen: (d: DocRef) => void
  onClose: () => void
}) {
  const { data, error } = useFetch(
    doc ? () => getDoc(doc.path, doc.version) : null,
    `${doc?.path}@${doc?.version}`,
  )
  const diff = useFetch(
    doc?.diffFrom ? () => getDocDiff(doc.path, doc.diffFrom ?? 0, doc.version) : null,
    `${doc?.path}@${doc?.diffFrom}..${doc?.version}`,
  )
  const versions = (data?.history ?? []).map((h) => ({
    value: String(h.version),
    label: `v${h.version}`,
  }))
  return (
    <Sheet open={Boolean(doc)} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-3xl">
        <SheetHeader className="border-b">
          <SheetTitle className="pr-8 font-mono text-sm break-all">{doc?.path}</SheetTitle>
          {data && (
            <SheetDescription render={<div />} className="flex flex-wrap items-center gap-2">
              <Select
                items={versions}
                value={String(data.version)}
                onValueChange={(v) => doc && onOpen({ path: doc.path, version: Number(v) })}
              >
                <SelectTrigger size="sm" aria-label="Version">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {versions.map((v) => (
                    <SelectItem key={v.value} value={v.value}>
                      {v.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {doc?.diffFrom ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onOpen({ path: doc.path, version: doc.version })}
                >
                  Show the document
                </Button>
              ) : (
                (data.history?.length ?? 0) > 1 &&
                data.version > 1 && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      doc &&
                      onOpen({ path: doc.path, version: data.version, diffFrom: data.version - 1 })
                    }
                  >
                    Diff from v{data.version - 1}
                  </Button>
                )
              )}
              <span>
                by {data.author} ({data.role}) · {data.created_at} · schema{' '}
                <code>{data.schema}</code> · <code>{data.sha256.slice(7, 19)}</code>
              </span>
            </SheetDescription>
          )}
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {error ? (
            <p className="text-sm text-destructive">{error}</p>
          ) : doc?.diffFrom ? (
            diff.error ? (
              <p className="text-sm text-destructive">{diff.error}</p>
            ) : diff.data ? (
              <div className="space-y-2">
                <p className="text-sm text-muted-foreground">
                  Changes from v{diff.data.from} to v{diff.data.to}
                </p>
                <Diff text={diff.data.diff} />
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Loading…</p>
            )
          ) : (
            <Pre className="max-h-none">{data?.content ?? 'Loading…'}</Pre>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
