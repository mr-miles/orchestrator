import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import type { Who } from './api'

interface Settings extends Who {
  setApprover: (v: string) => void
  setToken: (v: string) => void
}

const SettingsContext = createContext<Settings | null>(null)

/** Who acts: the approver id (remembered in this browser) and the operator token (kept for the
 * browser session only, never in localStorage). */
export function SettingsProvider({ children }: { children: ReactNode }) {
  const [approver, setApproverState] = useState(() => localStorage.getItem('approver') ?? '')
  const [token, setTokenState] = useState(() => sessionStorage.getItem('optoken') ?? '')
  const value = useMemo<Settings>(
    () => ({
      approver,
      token,
      setApprover: (v) => {
        localStorage.setItem('approver', v)
        setApproverState(v)
      },
      setToken: (v) => {
        sessionStorage.setItem('optoken', v)
        setTokenState(v)
      },
    }),
    [approver, token],
  )
  return <SettingsContext value={value}>{children}</SettingsContext>
}

export function useSettings(): Settings {
  const s = useContext(SettingsContext)
  if (!s) throw new Error('useSettings outside SettingsProvider')
  return s
}
