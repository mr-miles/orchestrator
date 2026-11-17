import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ThemeProvider } from '@/components/theme-provider'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SettingsProvider } from '@/lib/settings'
import App from './App'
import './index.css'

const root = document.getElementById('root')
if (!root) throw new Error('no #root element')
createRoot(root).render(
  <StrictMode>
    <ThemeProvider>
      <SettingsProvider>
        <TooltipProvider>
          <App />
          <Toaster richColors closeButton />
        </TooltipProvider>
      </SettingsProvider>
    </ThemeProvider>
  </StrictMode>,
)
