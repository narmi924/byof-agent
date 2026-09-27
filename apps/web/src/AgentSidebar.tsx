import { ChartNoAxesGantt, ListChecks, PanelLeftClose, Plus } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from './components/ui/button'
import { ScrollArea } from './components/ui/scroll-area'
import type { CaseRecord } from './caseContracts'
import type { ViewName } from './viewState'
import { WorkbenchSettings } from './WorkbenchSettings'
import { appNames } from './copy'

const dayKey = (value: string) => new Date(value).toDateString()

function statusNote(item: CaseRecord): string | null {
  if (item.error_code === 'MODEL_TURN_BUDGET_EXHAUSTED') return 'Paused'
  if (item.error_code === 'MODEL_CASE_BUDGET_EXHAUSTED' || item.error_code === 'MODEL_BUDGET_EXHAUSTED') return 'Limit reached'
  if (item.state === 'RESOLVED') return 'Done'
  return null
}

export function AgentSidebar({ conversations, selection, view, onSelect, onView, onHide, settings }: {
  conversations: CaseRecord[] | null
  selection: string
  view: ViewName
  onSelect: (id: string) => void
  onView: (view: ViewName) => void
  onHide?: () => void
  settings?: ReactNode
}) {
  const today = new Date().toDateString()
  const sorted = [...(conversations ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const groups = [
    { label: 'Today', items: sorted.filter(item => dayKey(item.created_at) === today) },
    { label: 'Earlier', items: sorted.filter(item => dayKey(item.created_at) !== today) },
  ].filter(group => group.items.length)
  const active = (id: string) => view === 'chat' && selection === id
  return <aside className="agent-sidebar" aria-label="Conversation sidebar">
    <div className="agent-sidebar-brand"><span className="wordmark">BYOF</span><span>{appNames.agent}</span>
      {onHide ? <Button variant="ghost" className="sidebar-toggle" aria-label="Hide sidebar" title="Hide sidebar" onClick={onHide}><PanelLeftClose size={16} aria-hidden="true" /></Button> : null}
    </div>
    <Button variant="outline" className="new-conversation" aria-current={active('new') ? 'page' : undefined} onClick={() => { onSelect('new'); onView('chat') }}>
      <Plus size={16} aria-hidden="true" />New conversation
    </Button>
    <ScrollArea className="agent-history">
      <nav aria-label="Conversations">
        {conversations === null ? <div className="history-skeleton" aria-hidden="true"><span /><span /><span /></div> : null}
        {groups.map(group => <section key={group.label}>
          <h2>{group.label}</h2>
          {group.items.map(item => {
            const note = statusNote(item)
            return <Button variant="ghost" key={item.case_id} className="history-item" title={item.title}
              aria-current={active(item.case_id) ? 'page' : undefined}
              onClick={() => { onSelect(item.case_id); onView('chat') }}>
              <span>{item.title}</span>{note ? <small>{note}</small> : null}
            </Button>
          })}
        </section>)}
      </nav>
    </ScrollArea>
    <nav className="agent-sidebar-foot" aria-label="Workbench navigation">
      <Button variant="ghost" aria-current={view === 'board' ? 'page' : undefined} onClick={() => onView('board')}><ChartNoAxesGantt size={16} aria-hidden="true" />Production timeline</Button>
      <Button variant="ghost" aria-current={view === 'records' ? 'page' : undefined} onClick={() => onView('records')}><ListChecks size={16} aria-hidden="true" />Execution log</Button>
      <WorkbenchSettings>{settings}</WorkbenchSettings>
    </nav>
  </aside>
}
