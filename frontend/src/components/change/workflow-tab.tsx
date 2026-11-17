import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useFetch } from '@/hooks/use-fetch'
import { getWorkflow } from '@/lib/api'
import { Pre } from '../status'

export function WorkflowTab({ assetsSha }: { assetsSha: string }) {
  const { data } = useFetch(getWorkflow, 'workflow')
  return (
    <Card>
      <CardHeader>
        <CardTitle>WORKFLOW.md</CardTitle>
        <CardDescription>
          Asset set <code className="font-mono">{assetsSha}</code>
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Pre className="max-h-[70vh]">{data ?? 'Loading…'}</Pre>
      </CardContent>
    </Card>
  )
}
