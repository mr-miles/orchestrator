import {
  ChevronRightIcon,
  MenuIcon,
  MonitorIcon,
  MoonIcon,
  SettingsIcon,
  SunIcon,
} from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { FieldGroup } from '@/components/ui/field'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { startChange } from '@/lib/api'
import { useSettings } from '@/lib/settings'
import { CHANGE_DOT, humanize } from '@/lib/status'
import type { Connection } from '@/lib/streams'
import type { Attention, AttentionItem, ChangeSummary, Workspace } from '@/lib/types'
import { cn } from '@/lib/utils'
import { AttentionMenu } from './attention-menu'
import { SelectField, TextField } from './fields'
import { StatusBadge } from './status'
import { useTheme, type Theme } from './theme-provider'

/** Starts a change in a workspace: intake runs on the goal. */
export function NewChangeDialog({
  open,
  onOpenChange,
  onStarted,
  workspaces,
  defaultWorkspace,
  workflows = [],
  defaultWorkflow = '',
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  onStarted: (cid: string, ws: string) => void
  workspaces: Workspace[]
  /** the workspace being looked at, else the first */
  defaultWorkspace: string
  /** the workflows offered; the select shows only when several */
  workflows?: string[]
  defaultWorkflow?: string
}) {
  const who = useSettings()
  const [goal, setGoal] = useState('')
  const [picked, setPicked] = useState<string | null>(null)
  const ws = picked ?? defaultWorkspace
  const [pickedWf, setPickedWf] = useState<string | null>(null)
  const wf = pickedWf ?? defaultWorkflow
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <form
          className="contents"
          onSubmit={async (e) => {
            e.preventDefault()
            setBusy(true)
            try {
              const r = await startChange(goal, ws, who, wf)
              setGoal('')
              setPicked(null)
              setPickedWf(null)
              onOpenChange(false)
              onStarted(r.change, ws)
            } catch (err) {
              toast.error(err instanceof Error ? err.message : String(err))
            } finally {
              setBusy(false)
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>Start a change</DialogTitle>
            <DialogDescription>
              Intake turns the goal into requirements and a risk tier for H-1.
            </DialogDescription>
          </DialogHeader>
          <FieldGroup>
            {workspaces.length > 1 && (
              <SelectField
                label="Workspace"
                value={ws}
                onChange={setPicked}
                options={workspaces.map((w) => ({ value: w.id, label: w.name }))}
                description="The repository the change is made in."
              />
            )}
            {workflows.length > 1 && (
              <SelectField
                label="Workflow"
                value={wf}
                onChange={setPickedWf}
                options={workflows.map((w) => ({ value: w, label: w }))}
                description="The workflow the change follows."
              />
            )}
            <TextField
              label="Goal"
              value={goal}
              onChange={setGoal}
              multiline
              placeholder="Describe the goal for the agents…"
            />
          </FieldGroup>
          <DialogFooter showCloseButton>
            <Button type="submit" disabled={busy || !goal.trim() || !ws}>
              Start intake
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Who acts: the approver id and the operator token (this browser session only). */
function SettingsDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const s = useSettings()
  const [approver, setApprover] = useState(s.approver)
  const [token, setToken] = useState(s.token)
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (o) {
          setApprover(s.approver)
          setToken(s.token)
        }
        onOpenChange(o)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <form
          className="contents"
          onSubmit={(e) => {
            e.preventDefault()
            s.setApprover(approver.trim())
            s.setToken(token)
            onOpenChange(false)
          }}
        >
          <DialogHeader>
            <DialogTitle>Operator settings</DialogTitle>
            <DialogDescription>Decisions are recorded under your approver id.</DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <TextField
              label="Approver id"
              value={approver}
              onChange={setApprover}
              description="One of the controller's decision owners; remembered in this browser."
            />
            <TextField
              label="Operator token"
              type="password"
              value={token}
              onChange={setToken}
              description="Required for decisions when the controller has one; kept for this browser session only."
            />
          </FieldGroup>
          <DialogFooter showCloseButton>
            <Button type="submit">Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

const THEMES: { value: Theme; label: string; icon: typeof SunIcon }[] = [
  { value: 'light', label: 'Light', icon: SunIcon },
  { value: 'dark', label: 'Dark', icon: MoonIcon },
  { value: 'system', label: 'System', icon: MonitorIcon },
]

function ConnectionDot({ connection }: { connection: Connection }) {
  const text = { open: 'Live', connecting: 'Reconnecting…', closed: 'Disconnected' }[connection]
  return (
    <Tooltip>
      <TooltipTrigger
        render={<span />}
        className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
        aria-label={text}
      >
        <span
          className={cn(
            'size-2 rounded-full',
            connection === 'open'
              ? 'bg-green-500'
              : connection === 'connecting'
                ? 'animate-pulse bg-amber-500'
                : 'bg-red-500',
          )}
        />
        <span className="hidden sm:inline">{text}</span>
      </TooltipTrigger>
      <TooltipContent>Updates are pushed by the controller as they happen.</TooltipContent>
    </Tooltip>
  )
}

export interface Crumb {
  label: string
  href?: string
  mono?: boolean
}

/** Where you are: ``Workloads › <workspace> › <change>``, or ``Configuration › Skills``. */
function Breadcrumbs({ crumbs }: { crumbs: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 text-sm">
        {crumbs.map((c, i) => {
          const last = i === crumbs.length - 1
          return (
            <li key={`${i}:${c.label}`} className="flex min-w-0 items-center gap-1.5">
              {i > 0 && <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" />}
              {last || !c.href ? (
                <span
                  aria-current={last ? 'page' : undefined}
                  className={cn(
                    'truncate',
                    last ? 'font-medium' : 'text-muted-foreground',
                    c.mono && 'font-mono',
                  )}
                >
                  {c.label}
                </span>
              ) : (
                <a
                  href={c.href}
                  className={cn(
                    'truncate text-muted-foreground hover:text-foreground',
                    c.mono && 'font-mono',
                  )}
                >
                  {c.label}
                </a>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

export function Header({
  crumbs,
  status,
  connection,
  attention,
  onOpenItem,
  onMenu,
}: {
  crumbs: Crumb[]
  /** the open change's status, beside the breadcrumbs */
  status?: ChangeSummary['status'] | null
  connection: Connection | null
  attention: Attention | null
  onOpenItem: (item: AttentionItem) => void
  /** opens the main menu (narrow screens) */
  onMenu: () => void
}) {
  const s = useSettings()
  const { theme, setTheme } = useTheme()
  const [settings, setSettings] = useState(false)
  return (
    <header className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur">
      <div className="flex h-14 items-center gap-3 px-4">
        <Button
          variant="ghost"
          size="icon-sm"
          className="md:hidden"
          aria-label="Menu"
          onClick={onMenu}
        >
          <MenuIcon />
        </Button>
        <Breadcrumbs crumbs={crumbs} />
        {status && <StatusBadge status={CHANGE_DOT[status]} label={humanize(status)} />}
        <div className="ml-auto flex items-center gap-3">
          <AttentionMenu attention={attention} onOpen={onOpenItem} />
          {connection && <ConnectionDot connection={connection} />}
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button variant="ghost" className="gap-2" />}>
              <SettingsIcon />
              <span className="hidden sm:inline">{s.approver || 'Settings'}</span>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuGroup>
                <DropdownMenuLabel>
                  {s.approver ? `Acting as ${s.approver}` : 'No approver set'}
                </DropdownMenuLabel>
                <DropdownMenuItem onClick={() => setSettings(true)}>
                  Operator settings…
                </DropdownMenuItem>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>Theme</DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  <DropdownMenuRadioGroup value={theme} onValueChange={(v) => setTheme(v as Theme)}>
                    {THEMES.map(({ value, label, icon: Icon }) => (
                      <DropdownMenuRadioItem key={value} value={value}>
                        <Icon />
                        {label}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      <SettingsDialog open={settings} onOpenChange={setSettings} />
    </header>
  )
}
