import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { Change, EnvironmentSpec as Spec } from '@/lib/types'
import { Attestations, Muted, Pre } from '../status'
import { useNav } from './nav'

/** The sandbox environment spec: profiles (tools, network, resources), the role map, and the
 * proposed library entries a profile uses (flagged: new, reviewed here first). */
export function EnvironmentSpec({ env }: { env: Spec | null }) {
  const nav = useNav()
  if (!env) return <Muted>No environment spec yet.</Muted>
  const { spec, proposals } = env
  const isNew = (kind: 'tools' | 'policies', name: string) =>
    proposals[kind].includes(name.split('@')[0] ?? name)
  const tag = (kind: 'tools' | 'policies', name: string) => (
    <Badge
      key={name}
      variant={isNew(kind, name) ? 'destructive' : 'secondary'}
      title={isNew(kind, name) ? 'proposed: not in the library' : undefined}
    >
      {name}
    </Badge>
  )
  const roles = (p: string) =>
    Object.entries(spec.roles)
      .filter(([, x]) => x === p)
      .map(([r]) => r)
      .join(', ') || 'unused'
  const proposed = (['tools', 'policies'] as const).flatMap((k) =>
    Object.entries(spec.proposed?.[k] ?? {}).map(([name, body]) => ({ k, name, body })),
  )
  return (
    <div className="space-y-3 text-sm">
      <p>{spec.summary}</p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Profile</TableHead>
            <TableHead>Roles</TableHead>
            <TableHead>Tools</TableHead>
            <TableHead>Network</TableHead>
            <TableHead>Resources</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {Object.entries(spec.profiles).map(([p, pr]) => (
            <TableRow key={p}>
              <TableCell className="font-medium">{p}</TableCell>
              <TableCell className="whitespace-normal">{roles(p)}</TableCell>
              <TableCell className="whitespace-normal">
                <div className="flex flex-wrap gap-1">{pr.tools.map((t) => tag('tools', t))}</div>
              </TableCell>
              <TableCell className="whitespace-normal">
                {(env.network[p] ?? []).length ? (
                  (env.network[p] ?? []).map((x) => (
                    <div key={x.policy} className="space-x-1">
                      {tag('policies', x.policy)}
                      <Muted className="text-xs">
                        for {x.tools.join(', ')}: {x.hosts.join(', ')}
                      </Muted>
                    </div>
                  ))
                ) : (
                  <Muted>{pr.offline ? 'offline' : 'none'}</Muted>
                )}
              </TableCell>
              <TableCell className="text-xs">
                {pr.resources
                  ? Object.entries(pr.resources)
                      .map(([k, v]) => `${k} ${v}`)
                      .join(', ')
                  : 'defaults'}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {env.problems.length > 0 && (
        <div className="space-y-1 text-destructive">
          {env.problems.map((p) => (
            <p key={p}>{p}</p>
          ))}
        </div>
      )}
      {proposed.map(({ k, name, body }) => (
        <Collapsible key={`${k}:${name}`}>
          <CollapsibleTrigger render={<Button variant="ghost" size="sm" />}>
            Proposed {k === 'tools' ? 'tool' : 'network policy'} <code>{name}</code>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <Pre>{JSON.stringify(body, null, 2)}</Pre>
          </CollapsibleContent>
        </Collapsible>
      ))}
      <Collapsible>
        <CollapsibleTrigger render={<Button variant="ghost" size="sm" />}>
          Rationale
        </CollapsibleTrigger>
        <CollapsibleContent>
          <p className="px-3 py-2">{spec.rationale}</p>
        </CollapsibleContent>
      </Collapsible>
      <Button variant="link" className="h-auto p-0" onClick={() => nav.openDoc(env.path)}>
        {env.path.split('/').slice(2).join('/')} v{env.version}
      </Button>
    </div>
  )
}

/** The environment provisioned for each role (images, network policies, resources). */
export function EnvironmentBuild({ change }: { change: Change | null }) {
  const b = change?.environment
  if (!b) return <Muted>Not provisioned yet.</Muted>
  return (
    <div className="space-y-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Role</TableHead>
            <TableHead>Profile</TableHead>
            <TableHead>Image</TableHead>
            <TableHead>Network policies</TableHead>
            <TableHead>Resources</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {Object.entries(b.roles).map(([r, x]) => (
            <TableRow key={r}>
              <TableCell className="font-medium">{r}</TableCell>
              <TableCell>{x.profile}</TableCell>
              <TableCell className="font-mono text-xs whitespace-normal">
                {x.image ?? 'default'}
              </TableCell>
              <TableCell className="font-mono text-xs whitespace-normal">
                {Object.values(x.network ?? {})
                  .map(
                    (n) =>
                      `${n.name}: ${(n.endpoints ?? []).map((e) => `${e.host}:${e.port}`).join(', ')}`,
                  )
                  .join('\n') || '—'}
              </TableCell>
              <TableCell className="font-mono text-xs whitespace-normal">
                {[
                  x.cpu && `--cpu ${x.cpu}`,
                  x.memory && `--memory ${x.memory}`,
                  x.driver_config && `driver ${x.driver_config}`,
                ]
                  .filter(Boolean)
                  .join('\n') || '—'}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Attestations atts={[b.spec, b.log, b.attestation].filter((a): a is string => Boolean(a))} />
    </div>
  )
}
