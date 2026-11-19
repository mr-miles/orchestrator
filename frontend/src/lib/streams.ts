import { useEffect, useState } from 'react'
import type { Attention, ChangeState, ChangeSummary, LogEntry, LogUpdate } from './types'

export type Connection = 'connecting' | 'open' | 'closed'

/** The open EventSource connection's state, from its readyState. */
function connectionOf(es: EventSource): Connection {
  return es.readyState === EventSource.OPEN
    ? 'open'
    : es.readyState === EventSource.CLOSED
      ? 'closed'
      : 'connecting'
}

interface Streamed {
  cid: string
  state: ChangeState | null
  connection: Connection
}

/** A change's live state, the change list and every change's attention items, pushed by
 * ``/api/changes/<cid>/events`` whenever one differs (the browser reconnects by itself after a
 * dropped connection). What arrived is kept per change, so switching changes never shows the
 * previous one's state. */
export function useChangeStream(cid: string | null) {
  const [got, setGot] = useState<Streamed | null>(null)
  const [changes, setChanges] = useState<ChangeSummary[] | null>(null)
  const [attention, setAttention] = useState<Attention | null>(null)
  useEffect(() => {
    if (!cid) return
    const es = new EventSource(`/api/changes/${encodeURIComponent(cid)}/events`)
    const update = (patch: Partial<Streamed>) =>
      setGot((g) => ({
        ...(g?.cid === cid ? g : { cid, state: null, connection: 'connecting' as const }),
        ...patch,
      }))
    es.addEventListener('state', (e) =>
      update({ state: JSON.parse((e as MessageEvent<string>).data) }),
    )
    es.addEventListener('changes', (e) => setChanges(JSON.parse((e as MessageEvent<string>).data)))
    es.addEventListener('attention', (e) =>
      setAttention(JSON.parse((e as MessageEvent<string>).data)),
    )
    es.onopen = () => update({ connection: 'open' })
    es.onerror = () => update({ connection: connectionOf(es) })
    return () => es.close()
  }, [cid])
  const mine = got && got.cid === cid ? got : null
  return {
    state: mine?.state ?? null,
    changes,
    attention,
    connection: mine?.connection ?? 'connecting',
  }
}

/** The change list and every change's attention items, pushed by ``/api/events`` – for the
 * pages that show no single change (``enabled`` false: nothing is opened). */
export function useOverviewStream(enabled: boolean) {
  const [changes, setChanges] = useState<ChangeSummary[] | null>(null)
  const [attention, setAttention] = useState<Attention | null>(null)
  const [connection, setConnection] = useState<Connection>('connecting')
  useEffect(() => {
    if (!enabled) return
    const es = new EventSource('/api/events')
    es.addEventListener('changes', (e) => setChanges(JSON.parse((e as MessageEvent<string>).data)))
    es.addEventListener('attention', (e) =>
      setAttention(JSON.parse((e as MessageEvent<string>).data)),
    )
    es.onopen = () => setConnection('open')
    es.onerror = () => setConnection(connectionOf(es))
    return () => es.close()
  }, [enabled])
  return { changes, attention, connection }
}

export interface LogView {
  update: Omit<LogUpdate, 'from' | 'entries'> | null
  entries: LogEntry[]
  ended: boolean
}

export const EMPTY_LOG: LogView = { update: null, entries: [], ended: false }

/** Apply a ``log`` event: entries before ``from`` are kept, the rest replaced. */
export function applyLogUpdate(view: LogView, u: LogUpdate): LogView {
  const { from, entries, ...head } = u
  return { ...view, update: head, entries: [...view.entries.slice(0, from), ...entries] }
}

/** A run's agent log, pushed by ``/api/runs/<run>/log/events`` until the run has ended. */
export function useLogStream(run: string | null): LogView {
  const [got, setGot] = useState<{ run: string; view: LogView } | null>(null)
  useEffect(() => {
    if (!run) return
    const es = new EventSource(`/api/runs/${encodeURIComponent(run)}/log/events`)
    const update = (f: (v: LogView) => LogView) =>
      setGot((g) => ({ run, view: f(g?.run === run ? g.view : EMPTY_LOG) }))
    es.addEventListener('log', (e) =>
      update((v) => applyLogUpdate(v, JSON.parse((e as MessageEvent<string>).data))),
    )
    es.addEventListener('end', () => {
      update((v) => ({ ...v, ended: true }))
      es.close()
    })
    return () => es.close()
  }, [run])
  return got && got.run === run ? got.view : EMPTY_LOG
}
