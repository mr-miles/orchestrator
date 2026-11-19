import { ChevronRightIcon, CopyIcon, DownloadIcon, FileIcon, FolderIcon } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { Button, buttonVariants } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useFetch } from '@/hooks/use-fetch'
import { buildTree, formatSize, type TreeDir } from '@/lib/files'
import type { FileEntry, FileView } from '@/lib/types'
import { cn } from '@/lib/utils'
import { Muted, Pre } from './status'

function Dir({
  dir,
  depth,
  open,
  toggle,
  selected,
  onSelect,
}: {
  dir: TreeDir
  depth: number
  open: Set<string>
  toggle: (p: string) => void
  selected: string | null
  onSelect: (p: string) => void
}) {
  return (
    <>
      {dir.dirs.map((d) => (
        <li key={d.path}>
          <button
            type="button"
            className="flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left hover:bg-muted"
            style={{ paddingLeft: `${depth * 0.9 + 0.375}rem` }}
            aria-expanded={open.has(d.path)}
            onClick={() => toggle(d.path)}
          >
            <ChevronRightIcon
              className={cn(
                'size-3.5 shrink-0 transition-transform',
                open.has(d.path) && 'rotate-90',
              )}
            />
            <FolderIcon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">{d.name}</span>
          </button>
          {open.has(d.path) && (
            <ul>
              <Dir
                dir={d}
                depth={depth + 1}
                open={open}
                toggle={toggle}
                selected={selected}
                onSelect={onSelect}
              />
            </ul>
          )}
        </li>
      ))}
      {dir.files.map((f) => (
        <li key={f.path}>
          <button
            type="button"
            className={cn(
              'flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left hover:bg-muted',
              selected === f.path && 'bg-muted font-medium',
            )}
            style={{ paddingLeft: `${depth * 0.9 + 1.375}rem` }}
            aria-current={selected === f.path ? 'true' : undefined}
            onClick={() => onSelect(f.path)}
          >
            <FileIcon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">{f.name}</span>
            {f.note && <span className="ml-auto shrink-0 text-muted-foreground">{f.note}</span>}
          </button>
        </li>
      ))}
    </>
  )
}

/** Files under a folder tree, and the selected one's content with Copy and Download. ``version``
 * says when the listing's contents may have changed (a branch's commit, the store moving on). */
export function FileBrowser({
  label,
  files,
  load,
  downloadUrl,
  version,
  toolbar,
  empty = 'No files.',
}: {
  label: string
  files: (FileEntry & { note?: string })[]
  load: (path: string) => Promise<FileView>
  downloadUrl: (path: string) => string
  version: string
  toolbar?: ReactNode
  empty?: string
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState<Set<string>>(new Set())
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase()
    return f ? files.filter((x) => x.path.toLowerCase().includes(f)) : files
  }, [files, filter])
  const tree = useMemo(() => buildTree(shown), [shown])
  // while filtering, every folder with a match is open
  const openDirs = filter.trim() ? new Set(tree.allDirs) : open
  const current = files.some((f) => f.path === selected) ? selected : null
  const view = useFetch(current ? () => load(current) : null, `${version}:${current}`)
  const text = view.data?.path === current ? view.data?.text : null
  const toggle = (p: string) =>
    setOpen((s) => {
      const n = new Set(s)
      if (n.has(p)) n.delete(p)
      else n.add(p)
      return n
    })
  return (
    <div className="grid min-h-[28rem] gap-4 md:grid-cols-[minmax(14rem,20rem)_1fr]">
      <nav aria-label={label} className="flex min-w-0 flex-col gap-2 rounded-lg border p-2">
        {toolbar}
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter files…"
          aria-label="Filter files"
        />
        <ul className="max-h-[36rem] min-h-0 flex-1 overflow-auto text-xs">
          {shown.length ? (
            <Dir
              dir={tree.root}
              depth={0}
              open={openDirs}
              toggle={toggle}
              selected={current}
              onSelect={setSelected}
            />
          ) : (
            <li className="p-2">
              <Muted>{filter ? 'No file matches.' : empty}</Muted>
            </li>
          )}
        </ul>
      </nav>
      <section aria-label="File" className="flex min-w-0 flex-col gap-2">
        {current ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <code className="min-w-0 flex-1 text-sm break-all">{current}</code>
              {view.data?.size != null && <Muted>{formatSize(view.data.size)}</Muted>}
              <Button
                variant="outline"
                size="sm"
                disabled={text == null}
                onClick={() =>
                  navigator.clipboard
                    .writeText(text ?? '')
                    .then(() => toast.success('Copied to the clipboard'))
                    .catch(() => toast.error('The browser refused the clipboard'))
                }
              >
                <CopyIcon data-icon="inline-start" />
                Copy
              </Button>
              <a
                className={buttonVariants({ variant: 'outline', size: 'sm' })}
                href={downloadUrl(current)}
                download
              >
                <DownloadIcon data-icon="inline-start" />
                Download
              </a>
            </div>
            {view.error ? (
              <p className="text-sm text-destructive">{view.error}</p>
            ) : view.data?.path !== current ? (
              <Muted>Loading…</Muted>
            ) : text != null ? (
              <Pre className="max-h-[40rem] flex-1">{text}</Pre>
            ) : (
              <Muted>Not shown: {view.data.reason}. Download it instead.</Muted>
            )}
          </>
        ) : (
          <div className="grid flex-1 place-items-center rounded-lg border border-dashed p-6">
            <Muted>Choose a file to see it.</Muted>
          </div>
        )}
      </section>
    </div>
  )
}
