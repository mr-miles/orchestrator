import { useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { FieldGroup } from '@/components/ui/field'

/** A button opening a dialog with a form; ``onSubmit`` resolving true closes it. */
export function FormDialog({
  trigger,
  title,
  description,
  submit,
  variant = 'default',
  onSubmit,
  children,
}: {
  trigger: ReactNode
  title: string
  description?: ReactNode
  submit: string
  variant?: 'default' | 'destructive' | 'secondary'
  onSubmit: () => Promise<boolean>
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="sm" />}>{trigger}</DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <form
          className="contents"
          onSubmit={async (e) => {
            e.preventDefault()
            setBusy(true)
            try {
              if (await onSubmit()) setOpen(false)
            } finally {
              setBusy(false)
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>
          <FieldGroup>{children}</FieldGroup>
          <DialogFooter showCloseButton>
            <Button type="submit" variant={variant} disabled={busy}>
              {submit}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
