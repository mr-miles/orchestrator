import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type Theme = 'dark' | 'light' | 'system'

const STORAGE_KEY = 'legion-theme' // also read by public/theme.js before the first paint

interface ThemeState {
  theme: Theme
  setTheme: (theme: Theme) => void
}

const ThemeContext = createContext<ThemeState>({ theme: 'system', setTheme: () => undefined })

/** Light, dark or the system's preference, as the ``dark`` class on <html> (shadcn/ui's Vite
 * theme provider). */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(
    () => (localStorage.getItem(STORAGE_KEY) as Theme | null) ?? 'system',
  )
  useEffect(() => {
    const root = document.documentElement
    const media = matchMedia('(prefers-color-scheme: dark)')
    const apply = () => {
      const dark = theme === 'dark' || (theme === 'system' && media.matches)
      root.classList.remove('light', 'dark')
      root.classList.add(dark ? 'dark' : 'light')
    }
    apply()
    if (theme !== 'system') return
    media.addEventListener('change', apply)
    return () => media.removeEventListener('change', apply)
  }, [theme])
  const setTheme = (t: Theme) => {
    localStorage.setItem(STORAGE_KEY, t)
    setThemeState(t)
  }
  return <ThemeContext value={{ theme, setTheme }}>{children}</ThemeContext>
}

export const useTheme = () => useContext(ThemeContext)
