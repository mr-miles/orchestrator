import { useEffect, useState } from 'react'

/** Fetch ``load()`` whenever ``key`` changes (nothing while ``load`` is null); the previous data
 * stays until the new arrives. */
export function useFetch<T>(load: (() => Promise<T>) | null, key: string) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!load) return
    let live = true
    load().then(
      (d) => {
        if (live) {
          setData(d)
          setError(null)
        }
      },
      (e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      },
    )
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ``key`` says when to reload
  }, [key])
  return { data, error }
}
