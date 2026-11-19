import { useCallback, useEffect, useRef, useState } from 'react'
import type { AttentionItem } from './types'

/** Browser-side attention state (WORKFLOW.md §2.2): which inbox items were read, when this
 * browser last looked, and opt-in desktop notifications. The controller decides what the items
 * are; nothing here changes the workflow. */

const READ_KEY = 'inbox-read'
const SEEN_KEY = 'last-seen'
const NOTIFY_KEY = 'browser-notifications'
const MAX_READ = 500
/** Away for longer than this, the landing view shows what happened meanwhile. */
export const AWAY_MS = 30 * 60 * 1000

/** "45s", "12m", "9h 02m", "3d 4h": how long ago ``iso`` was (empty when unknown). */
export function ago(iso: string, now = Date.now()): string {
  const t = Date.parse(iso)
  if (!iso || Number.isNaN(t)) return ''
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

function readSet(): string[] {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(READ_KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

/** Which inbox items this browser has read (the newest ``MAX_READ`` keys are kept). */
export function useInboxRead() {
  const [read, setRead] = useState<string[]>(readSet)
  const markRead = useCallback((keys: string[]) => {
    setRead((cur) => {
      const next = [...cur.filter((k) => !keys.includes(k)), ...keys].slice(-MAX_READ)
      localStorage.setItem(READ_KEY, JSON.stringify(next))
      return next
    })
  }, [])
  const isRead = useCallback((key: string) => read.includes(key), [read])
  return { isRead, markRead }
}

/** The tab title counts what needs you: ``(2) Legion Orchestrator``. */
export function useAttentionTitle(count: number) {
  useEffect(() => {
    const base = 'Legion Orchestrator'
    document.title = count > 0 ? `(${count}) ${base}` : base
  }, [count])
}

/** When this browser was last here before this visit (read once), and a way to mark the
 * digest of what happened since as seen. While the page is visible "now" is recorded every
 * minute and when it is hidden, so the next visit knows how long it was away. */
export function useLastSeen() {
  // read once: this visit's "since" (null unless the browser was away long enough)
  const [since, setSince] = useState<string | null>(() => {
    const p = localStorage.getItem(SEEN_KEY)
    return p && Date.now() - Date.parse(p) > AWAY_MS ? p : null
  })
  useEffect(() => {
    const record = () => localStorage.setItem(SEEN_KEY, new Date().toISOString())
    const onVisibility = () => document.visibilityState === 'hidden' && record()
    const timer = setInterval(() => document.visibilityState === 'visible' && record(), 60_000)
    addEventListener('visibilitychange', onVisibility)
    addEventListener('pagehide', record)
    return () => {
      clearInterval(timer)
      removeEventListener('visibilitychange', onVisibility)
      removeEventListener('pagehide', record)
    }
  }, [])
  const dismiss = useCallback(() => {
    localStorage.setItem(SEEN_KEY, new Date().toISOString())
    setSince(null)
  }, [])
  return { since, dismiss }
}

const canNotify = () => typeof Notification !== 'undefined'

/** Opt-in desktop notifications for blocking items that appear while the page is open (the
 * controller's own sinks cover the rest). Items present when the page loads are not announced. */
export function useBrowserNotifications(
  items: AttentionItem[] | null,
  onOpen: (item: AttentionItem) => void,
) {
  const [enabled, setEnabledState] = useState(
    () => canNotify() && localStorage.getItem(NOTIFY_KEY) === '1',
  )
  const known = useRef<Set<string> | null>(null)
  const open = useRef(onOpen)
  useEffect(() => {
    open.current = onOpen
  })
  useEffect(() => {
    if (!items) return
    const keys = new Set(items.map((i) => i.key))
    const before = known.current
    known.current = keys
    if (!before || !enabled || !canNotify() || Notification.permission !== 'granted') return
    for (const item of items) {
      if (before.has(item.key)) continue
      const n = new Notification(item.title, {
        body: `${item.change}: ${item.goal}`,
        tag: item.key,
      })
      n.onclick = () => {
        window.focus()
        open.current(item)
        n.close()
      }
    }
  }, [items, enabled])
  const setEnabled = useCallback(async (on: boolean) => {
    if (on && canNotify() && Notification.permission === 'default')
      await Notification.requestPermission()
    const ok = on && canNotify() && Notification.permission === 'granted'
    localStorage.setItem(NOTIFY_KEY, ok ? '1' : '0')
    setEnabledState(ok)
  }, [])
  return {
    supported: canNotify(),
    denied: canNotify() && Notification.permission === 'denied',
    enabled,
    setEnabled,
  }
}
