import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useFetch } from '@/hooks/use-fetch'
import { docPath, getDoc } from '@/lib/api'
import type { ChangeState, TaskContract } from '@/lib/types'
import { AttestationLink, Pre } from '../status'
import { useNav } from './nav'

export function PlanTab({ state }: { state: ChangeState }) {
  const nav = useNav()
  const plan = state.nodes.find((n) => n.kind === 'plan')
  const { data, error } = useFetch(
    () =>
      Promise.all([
        getDoc(docPath(nav.cid, 'plan/taskgraph.json')),
        getDoc(docPath(nav.cid, 'plan/spec.md')),
      ]),
    `${nav.cid}:${plan?.cycle}:${plan?.status}`,
  )
  if (!data)
    return (
      <Empty className="border">
        <EmptyHeader>
          <EmptyTitle>No plan yet</EmptyTitle>
          <EmptyDescription>
            {error ? 'The planner has not written one.' : 'Loading…'}
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  const [tg, spec] = data
  const tasks = (JSON.parse(tg.content) as { tasks?: TaskContract[] }).tasks ?? []
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Tasks</CardTitle>
          <CardDescription>
            <AttestationLink att={tg.attestation} />
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Task</TableHead>
                <TableHead>Soul</TableHead>
                <TableHead>Depends on</TableHead>
                <TableHead>Files</TableHead>
                <TableHead>Requirements</TableHead>
                <TableHead>Verification</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tasks.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="whitespace-normal">
                    <Button
                      variant="link"
                      className="h-auto p-0"
                      onClick={() => nav.openNode(`build:${t.id}`)}
                    >
                      {t.id}
                    </Button>{' '}
                    {t.title}
                  </TableCell>
                  <TableCell>{t.soul}</TableCell>
                  <TableCell>{(t.depends_on ?? []).join(', ')}</TableCell>
                  <TableCell className="font-mono text-xs whitespace-pre-line">
                    {(t.files_modified ?? []).join('\n')}
                  </TableCell>
                  <TableCell>{(t.requirements ?? []).join(', ')}</TableCell>
                  <TableCell className="font-mono text-xs whitespace-pre-line">
                    {(t.verification ?? []).join('\n')}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Spec</CardTitle>
          <CardDescription>
            <AttestationLink att={spec.attestation} />
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Pre className="max-h-none">{spec.content}</Pre>
        </CardContent>
      </Card>
    </div>
  )
}
