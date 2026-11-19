import type { ActionResult, Attention, ChangeSummary, Digest, Doc, DocMeta, Meta } from './types'

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

export function listDocs(prefix: string, all: boolean) {
  return api<DocMeta[]>(`/api/docs?prefix=${encodeURIComponent(prefix)}${all ? '&all=1' : ''}`)
}

export interface Who {
  approver: string
  token: string
}

export function startChange(goal: string, who: Who) {
  return api<{ change: string }>('/api/changes', { goal }, who.token)
}

export function nodeAction(
  cid: string,
  node: string,
  action: string,
  payload: Record<string, unknown>,
  who: Who,
) {
  return api<ActionResult>(
    `/api/changes/${encodeURIComponent(cid)}/nodes/${encodeURIComponent(node)}/action`,
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

/** ``changes/<cid>/<rel>``: a document path in a change. */
export const docPath = (cid: string, rel: string) => `changes/${cid}/${rel}`

/** The path and version of a ``file:<path>@<version>`` attestation, else null. */
export function parseFileAttestation(att: string): { path: string; version: number } | null {
  const m = /^file:(.+)@(\d+)$/.exec(att)
  return m ? { path: m[1] ?? '', version: Number(m[2]) } : null
}
