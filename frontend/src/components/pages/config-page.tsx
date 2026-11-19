import { useFetch } from '@/hooks/use-fetch'
import { configDownloadUrl, getConfigFile, getConfigFiles } from '@/lib/api'
import type { ConfigGroup } from '@/lib/route'
import { FileBrowser } from '../file-browser'
import { Muted } from '../status'

const ABOUT: Record<ConfigGroup, { title: string; text: string }> = {
  skills: {
    title: 'Skills',
    text: 'Instructions agents load when a task needs them (assets/skills).',
  },
  workflows: {
    title: 'Workflows',
    text: 'The workflow the controller enforces (WORKFLOW.md) and the schemas every document and report is checked against (assets/schemas).',
  },
  agents: {
    title: 'Agents',
    text: "Each role's soul, skills and write scope (roles.yaml), the souls, and the assignment prompts (assets/souls, assets/prompts).",
  },
}

/** A configuration group's definition files, as the controller reads them from its assets. */
export function ConfigPage({ group }: { group: ConfigGroup }) {
  const { data, error } = useFetch(() => getConfigFiles(group), group)
  const about = ABOUT[group]
  return (
    <div className="space-y-4">
      <div>
        <h1 className="font-heading text-xl font-semibold">{about.title}</h1>
        <p className="text-sm text-muted-foreground">{about.text}</p>
        {data && (
          <p className="text-xs text-muted-foreground">
            From <code>{data.root}</code> · asset set <code>{data.assets_sha.slice(7, 19)}</code>
          </p>
        )}
      </div>
      {error ? (
        <p className="text-sm text-destructive">{error}</p>
      ) : (
        <FileBrowser
          key={group}
          label={`${about.title} files`}
          files={data?.group === group ? data.files : []}
          version={data?.assets_sha ?? ''}
          load={(path) => getConfigFile(group, path)}
          downloadUrl={(path) => configDownloadUrl(group, path)}
          empty={data ? 'No files.' : 'Loading…'}
        />
      )}
      {data && !data.files.length && <Muted>Nothing found in the assets folder.</Muted>}
    </div>
  )
}
