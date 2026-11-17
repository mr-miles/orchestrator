import { useId, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useFetch } from '@/hooks/use-fetch'
import { docPath, listDocs } from '@/lib/api'
import { useNav } from './nav'

const SCOPES = [
  { value: 'change', label: 'This change' },
  { value: 'all', label: 'All changes' },
]

export function DocumentsTab({ version }: { version: string }) {
  const nav = useNav()
  const [scope, setScope] = useState('change')
  const [all, setAll] = useState(false)
  const id = useId()
  const prefix = scope === 'change' ? docPath(nav.cid, '') : ''
  const { data } = useFetch(() => listDocs(prefix, all), `${prefix}:${all}:${version}`)
  return (
    <Card>
      <CardHeader className="flex flex-wrap items-center gap-4">
        <CardTitle className="mr-auto">Documents</CardTitle>
        <Select items={SCOPES} value={scope} onValueChange={(v) => setScope(String(v))}>
          <SelectTrigger size="sm" aria-label="Scope">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCOPES.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="flex items-center gap-2">
          <Checkbox id={id} checked={all} onCheckedChange={(v) => setAll(Boolean(v))} />
          <Label htmlFor={id} className="font-normal">
            Include controller records
          </Label>
        </div>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Path</TableHead>
              <TableHead>Latest</TableHead>
              <TableHead>Schema</TableHead>
              <TableHead>Author</TableHead>
              <TableHead>Written</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(data ?? []).map((d) => (
              <TableRow key={d.path}>
                <TableCell className="whitespace-normal">
                  <Button
                    variant="link"
                    className="h-auto p-0 text-left font-mono text-xs break-all whitespace-normal"
                    onClick={() => nav.openDoc(d.path)}
                  >
                    {d.path}
                  </Button>
                </TableCell>
                <TableCell>v{d.version}</TableCell>
                <TableCell className="font-mono text-xs">{d.schema}</TableCell>
                <TableCell>
                  {d.author} <span className="text-muted-foreground">{d.role}</span>
                </TableCell>
                <TableCell className="text-muted-foreground">{d.created_at}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
