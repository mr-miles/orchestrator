import { useId, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useFetch } from '@/hooks/use-fetch'
import {
  docDownloadUrl,
  getDoc,
  getGitFile,
  getGitRefs,
  getGitTree,
  gitDownloadUrl,
  listDocs,
} from '@/lib/api'
import { formatHash, WORKSPACE_TABS, type WorkspaceTab } from '@/lib/route'
import type { ChangeSummary, Workspace } from '@/lib/types'
import { FileBrowser } from '../file-browser'
import { Muted, Sha } from '../status'
import { ChangesTable } from './changes-table'

const TAB_LABEL: Record<WorkspaceTab, string> = {
  changes: 'Changes',
  documents: 'Document store',
  repository: 'Repository',
}

/** Every document in the store (the controller's own records on request), latest versions. */
function DocumentStore({ version }: { version: string }) {
  const id = useId()
  const [all, setAll] = useState(false)
  const { data, error } = useFetch(() => listDocs('', all), `${all}:${version}`)
  const files = (data ?? []).map((d) => ({
    path: d.path,
    note: `v${d.version} · ${d.role}`,
  }))
  const latest = new Map((data ?? []).map((d) => [d.path, d.version]))
  if (error) return <p className="text-sm text-destructive">{error}</p>
  return (
    <FileBrowser
      label="Documents"
      files={files}
      version={version}
      load={async (path) => {
        const d = await getDoc(path)
        return { path, size: new Blob([d.content]).size, text: d.content }
      }}
      downloadUrl={(path) => docDownloadUrl(path, latest.get(path))}
      empty={data ? 'The store is empty.' : 'Loading…'}
      toolbar={
        <div className="flex items-center gap-2 px-1 text-xs">
          <Checkbox id={id} checked={all} onCheckedChange={(v) => setAll(Boolean(v))} />
          <Label htmlFor={id} className="text-xs font-normal">
            Include controller records
          </Label>
        </div>
      }
    />
  )
}

/** The workspace repository's files at a branch: the base, or a change's integration branch. */
function Repository({ ws }: { ws: string }) {
  const refs = useFetch(() => getGitRefs(ws), ws)
  const [picked, setPicked] = useState<string | null>(null)
  const ref = picked ?? refs.data?.[0]?.name ?? ''
  const tree = useFetch(ref ? () => getGitTree(ws, ref) : null, `${ws}:${ref}`)
  if (refs.error) return <p className="text-sm text-destructive">{refs.error}</p>
  const items = (refs.data ?? []).map((r) => ({ value: r.name, label: r.name }))
  return (
    <div className="space-y-2">
      <FileBrowser
        label="Repository files"
        files={tree.data?.files ?? []}
        version={tree.data?.sha ?? ''}
        load={(path) => getGitFile(ws, tree.data?.sha ?? ref, path)}
        downloadUrl={(path) => gitDownloadUrl(ws, tree.data?.sha ?? ref, path)}
        empty={tree.data ? 'The branch has no files.' : 'Loading…'}
        toolbar={
          <div className="space-y-1">
            <Select items={items} value={ref} onValueChange={(v) => setPicked(String(v))}>
              <SelectTrigger size="sm" aria-label="Branch" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {items.map((r) => (
                  <SelectItem key={r.value} value={r.value}>
                    {r.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {tree.data && (
              <p className="px-1 text-xs text-muted-foreground">
                at <Sha sha={tree.data.sha} />
              </p>
            )}
          </div>
        }
      />
      {tree.data?.truncated && <Muted>The listing was cut at 20,000 files.</Muted>}
    </div>
  )
}

/** One workspace: its changes, its document store and its repository. */
export function WorkspacePage({
  ws,
  tab,
  workspaces,
  changes,
}: {
  ws: string
  tab: WorkspaceTab
  workspaces: Workspace[]
  changes: ChangeSummary[]
}) {
  const w = workspaces.find((x) => x.id === ws)
  const mine = changes.filter((c) => (c.workspace ?? ws) === ws)
  // the store moves on whenever a change does
  const version = mine.map((c) => `${c.id}:${c.status}:${c.passed}:${c.live_runs}`).join()
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">{w?.name || ws}</CardTitle>
          <CardDescription>
            Repository <code>{w?.repo ?? ws}</code>, base branch{' '}
            <code>{w?.base_branch ?? '…'}</code>
            {w?.source && (
              <>
                , cloned from <code>{w.source}</code>
              </>
            )}
            {w && ` · ${w.documents} documents`}
          </CardDescription>
        </CardHeader>
      </Card>
      <Tabs
        value={tab}
        onValueChange={(t) =>
          (location.hash = formatHash({ page: 'workspace', ws, tab: t as WorkspaceTab }))
        }
      >
        <TabsList>
          {WORKSPACE_TABS.map((t) => (
            <TabsTrigger key={t} value={t}>
              {TAB_LABEL[t]}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="changes">
          <Card>
            <CardContent>
              <ChangesTable
                changes={mine}
                workspaces={workspaces}
                defaultWorkspace={ws}
                grouped={false}
                empty="No changes in this workspace yet."
              />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="documents">
          {tab === 'documents' && <DocumentStore version={version} />}
        </TabsContent>
        <TabsContent value="repository">
          {tab === 'repository' && <Repository ws={ws} />}
        </TabsContent>
      </Tabs>
    </div>
  )
}
