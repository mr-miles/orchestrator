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
import { useFetch } from '@/hooks/use-fetch'
import { getDoc } from '@/lib/api'
import { Pre } from '../status'

export interface DocRef {
  path: string
  version?: number
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
          ) : (
            <Pre className="max-h-none">{data?.content ?? 'Loading…'}</Pre>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
