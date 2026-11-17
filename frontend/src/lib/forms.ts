export interface Option {
  value: string
  label: string
}

/** Comma-separated input as a list. */
export const csv = (s: string) =>
  s
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)

export const options = (values: string[]): Option[] => values.map((v) => ({ value: v, label: v }))
