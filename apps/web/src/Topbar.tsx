import { Bell, PanelLeftOpen } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from './components/ui/button'
import type { Freshness, Snapshot } from './contracts'
import type { RiskSuggestion } from './riskContracts'
import { dayKeyOf, parseMs, safeZone } from './dayWindow'
import { shortDateTime } from './businessText'
import { Popover } from './ui'
import { appNames } from './copy'

/** The factory clock is the product's anchor: one ink pill on both surfaces. */
export function FactoryClock({ snapshot }: { snapshot: Snapshot | null }) {
  const zone = safeZone(snapshot?.profile.timezone)
  return <span className="factory-clock" title="Factory time">
    <span className="factory-clock-dot" aria-hidden="true" />
    <span className="visually-hidden">Factory time</span>
    <b>{snapshot ? shortDateTime(snapshot.snapshot_clock, zone) : '--:--'}</b>
  </span>
}

export function SyncState({ freshness }: { freshness: Freshness }) {
  if (freshness === 'CURRENT') return null
  return <span className="topbar-chip tone-critical" role="status">{freshness === 'STALE' ? 'Sync delayed' : 'Connecting'}</span>
}

export function AgentTopbar({ snapshot, freshness, suggestions, onSuggestion, onShowSidebar }: {
  snapshot: Snapshot | null
  freshness: Freshness
  suggestions: RiskSuggestion[]
  onSuggestion: (suggestion: RiskSuggestion) => void
  onShowSidebar?: (() => void) | undefined
}) {
  const zone = safeZone(snapshot?.profile.timezone)
  const clock = parseMs(snapshot?.snapshot_clock)
  const today = Number.isFinite(clock) ? dayKeyOf(clock, zone) : null
  const due = snapshot?.orders.filter(order => !['COMPLETED', 'CANCELLED'].includes(order.status)
    && today !== null && dayKeyOf(Date.parse(order.due_at), zone) === today).length ?? 0
  return <header className="topbar">
    {onShowSidebar ? <Button variant="ghost" className="sidebar-toggle" aria-label="Show sidebar" title="Show sidebar" onClick={onShowSidebar}><PanelLeftOpen size={16} aria-hidden="true" /></Button> : null}
    <FactoryClock snapshot={snapshot} />
    {snapshot ? <span className="topbar-chip">{snapshot.active_plan_version ? 'Plan active' : 'No active plan'}</span> : null}
    {snapshot ? <span className="topbar-chip">Due today: {due}</span> : null}
    <SyncState freshness={freshness} />
    <span className="topbar-spacer" />
    {suggestions.length ? <Popover label={<><Bell size={14} aria-hidden="true" />Field changes {suggestions.length}</>}>
      <ul className="change-list" aria-label="Field changes">
        {suggestions.map(item => <li key={item.suggestion_id}>
          <button type="button" onClick={() => onSuggestion(item)}><b>{item.title}</b><span>{item.detail}</span></button>
        </li>)}
      </ul>
    </Popover> : null}
  </header>
}

export function FactoryTopbar({ snapshot, freshness, children }: { snapshot: Snapshot | null; freshness: Freshness; children?: ReactNode }) {
  return <header className="topbar factory-topbar">
    <span className="topbar-brand"><span className="wordmark">BYOF</span><span>{appNames.simulator}</span></span>
    <FactoryClock snapshot={snapshot} />
    <SyncState freshness={freshness} />
    <span className="topbar-spacer" />
    {children}
  </header>
}
