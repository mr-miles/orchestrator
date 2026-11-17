import { useId, type ReactNode } from 'react'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import type { Option } from '@/lib/forms'

/** Labelled form controls over the shadcn/ui field primitives. */
export function TextField({
  label,
  value,
  onChange,
  placeholder,
  description,
  multiline,
  type,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  description?: ReactNode
  multiline?: boolean
  type?: string
}) {
  const id = useId()
  return (
    <Field>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {multiline ? (
        <Textarea
          id={id}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <Input
          id={id}
          type={type}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {description && <FieldDescription>{description}</FieldDescription>}
    </Field>
  )
}

export function SelectField({
  label,
  value,
  onChange,
  options,
  description,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: Option[]
  description?: ReactNode
}) {
  const id = useId()
  return (
    <Field>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Select items={options} value={value} onValueChange={(v) => onChange(String(v ?? ''))}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {description && <FieldDescription>{description}</FieldDescription>}
    </Field>
  )
}
