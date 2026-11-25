import type {
  ActionResult,
  AgentQuestion,
  Attention,
  ChangeSummary,
  ConfigFiles,
  Digest,
  Doc,
  DocDiff,
  DocMeta,
  FileView,
  GitRef,
  GitTree,
  Meta,
  Workspace,
} from './types'

export class ApiError extends Error {}

/** A JSON (or text) API call; writes carry the operator token, which only lives in memory and
 * the browser session (see settings). */
export async function api<T>(path: string, body?: unknown, token?: string): Promise<T> {
  const init: RequestInit =
    body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Operator-Token': token ?? '' },
          body: JSON.stringify(body),
        }
  const r = await fetch(path, init)
  const json = (r.headers.get('content-type') ?? '').includes('json')
  const data: unknown = json ? await r.json() : await r.text()
  if (!r.ok) {
    const message =
      typeof data === 'object' && data !== null && 'error' in data
        ? String((data as { error: unknown }).error)
        : String(data)
    throw new ApiError(message || `${r.status} ${r.statusText}`)
  }
  return data as T
}

export const getMeta = () => api<Meta>('/api/meta')
export const getChanges = () => api<ChangeSummary[]>('/api/changes')
export const getAttention = () => api<Attention>('/api/attention')
export const getDigest = (since: string) =>
  api<Digest>(`/api/digest?since=${encodeURIComponent(since)}`)
export const getWorkflow = () => api<string>('/api/workflow')
export const getRawLog = (run: string) =>
  api<string>(`/api/runs/${encodeURIComponent(run)}/log?lines=5000`)

export function getDoc(path: string, version?: number) {
  const v = version ? `&version=${version}` : ''
  return api<Doc>(`/api/doc?path=${encodeURIComponent(path)}${v}`)
}

/** The unified diff of a document from version ``from`` to ``to`` (else the latest). */
export function getDocDiff(path: string, from: number, to?: number) {
  const t = to ? `&to=${to}` : ''
  return api<DocDiff>(`/api/doc/diff?path=${encodeURIComponent(path)}&from=${from}${t}`)
}

export function listDocs(prefix: string, all: boolean) {
  return api<DocMeta[]>(`/api/docs?prefix=${encodeURIComponent(prefix)}${all ? '&all=1' : ''}`)
}

export interface Who {
  approver: string
  token: string
}

export function startChange(goal: string, workspace: string, who: Who, workflow?: string) {
  return api<{ change: string }>(
    '/api/changes',
    workflow ? { goal, workspace, workflow } : { goal, workspace },
    who.token,
  )
}

/** A decision on a ticket: a sign-off's ``approve`` / ``reject``, a waiting ticket's remedy
 * (``retry``, ``waive:<criterion>``, ``restart``, ``replan``). */
export function ticketAction(
  cid: string,
  ticket: string,
  action: string,
  payload: Record<string, unknown>,
  who: Who,
) {
  return api<ActionResult>(
    `/api/changes/${encodeURIComponent(cid)}/tickets/${encodeURIComponent(ticket)}/action`,
    { action, approver: who.approver, payload },
    who.token,
  )
}

export function changeAction(
  cid: string,
  action: string,
  payload: Record<string, unknown>,
  who: Who,
) {
  return api<ActionResult>(
    `/api/changes/${encodeURIComponent(cid)}/action`,
    { action, approver: who.approver, payload },
    who.token,
  )
}

/** Answer an agent's question (A2A ask_human) – or decide a build's request for more files
 * (request_scope: ``decision`` approve or deny, ``answer`` the reason): it completes its task. */
export function answerQuestion(
  cid: string,
  qid: string,
  answer: string,
  who: Who,
  decision?: 'approve' | 'deny',
) {
  return api<AgentQuestion>(
    `/api/changes/${encodeURIComponent(cid)}/questions/${encodeURIComponent(qid)}/answer`,
    { answer, approver: who.approver, ...(decision ? { decision } : {}) },
    who.token,
  )
}

/** ``changes/<cid>/<rel>``: a document path in a change. */
export const docPath = (cid: string, rel: string) => `changes/${cid}/${rel}`

/** The path and version of a ``file:<path>@<version>`` attestation, else null. */
export function parseFileAttestation(att: string): { path: string; version: number } | null {
  const m = /^file:(.+)@(\d+)$/.exec(att)
  return m ? { path: m[1] ?? '', version: Number(m[2]) } : null
}

const q = (params: Record<string, string | number | undefined>) =>
  Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&')

export const getWorkspaces = () => api<Workspace[]>('/api/workspaces')

const wsBase = (ws: string) => `/api/workspaces/${encodeURIComponent(ws)}/git`
export const getGitRefs = (ws: string) => api<GitRef[]>(`${wsBase(ws)}/refs`)
export const getGitTree = (ws: string, ref: string) =>
  api<GitTree>(`${wsBase(ws)}/tree?${q({ ref })}`)
export const getGitFile = (ws: string, ref: string, path: string) =>
  api<FileView>(`${wsBase(ws)}/file?${q({ ref, path })}`)
export const gitDownloadUrl = (ws: string, ref: string, path: string) =>
  `${wsBase(ws)}/file?${q({ ref, path, download: 1 })}`

export const docDownloadUrl = (path: string, version?: number) =>
  `/api/doc/raw?${q({ path, version })}`

export const getConfigFiles = (group: string) =>
  api<ConfigFiles>(`/api/config/${encodeURIComponent(group)}`)
export const getConfigFile = (group: string, path: string) =>
  api<FileView>(`/api/config/${encodeURIComponent(group)}/file?${q({ path })}`)
export const configDownloadUrl = (group: string, path: string) =>
  `/api/config/${encodeURIComponent(group)}/file?${q({ path, download: 1 })}`
