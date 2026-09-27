import type { FinishedGoodsLot, Snapshot } from './contracts'
import type { FactoryEditor } from './factoryEditor'
import type { FactorySection } from './factorySections'
import { factorySections } from './factorySections'
import { shortDateTime } from './businessText'
import { FactoryOrders } from './factory/FactoryOrders'
import { FactorySupply } from './factory/FactorySupply'
import { FactoryCapacity } from './factory/FactoryCapacity'
import { FactoryExecution } from './factory/FactoryExecution'
import { count } from './copy'

export interface FactoryChange { at: string; label: string }

interface Props {
  snapshot: Snapshot
  finishedGoods?: FinishedGoodsLot[] | null | undefined
  onEdit: (editor: FactoryEditor) => void
  disabled?: boolean
  changes?: FactoryChange[]
}

function sectionSummary(section: FactorySection, snapshot: Snapshot): string {
  switch (section) {
    case 'orders': return `${count(snapshot.orders.length, 'order')} · ${count(snapshot.profile.products.length, 'product')}`
    case 'supply': return `${count(snapshot.inventory.length, 'material')} · ${snapshot.receipts.filter(item => ['EXPECTED', 'CONFIRMED'].includes(item.status)).length} inbound`
    case 'capacity': return `${count(snapshot.resources.length, 'machine')} · ${snapshot.workers.length} staff`
    case 'execution': return `${snapshot.actuals.filter(item => item.state === 'BLOCKED').length} blocked · ${snapshot.actuals.filter(item => item.state === 'COMPLETED' && !['PASSED', 'FAILED'].includes(item.quality_state)).length} awaiting QC`
  }
}

/** Active problems a demo operator should see without scrolling. */
function sectionAlert(section: FactorySection, snapshot: Snapshot): string | null {
  switch (section) {
    case 'orders': return null
    case 'supply': return null
    case 'capacity': {
      const down = snapshot.resources.filter(item => item.status !== 'AVAILABLE').length
      const absent = snapshot.workers.filter(item => item.status !== 'AVAILABLE').length
      return [down ? `${down} down` : '', absent ? `${absent} absent` : ''].filter(Boolean).join(' · ') || null
    }
    case 'execution': {
      const blocked = snapshot.actuals.filter(item => item.state === 'BLOCKED').length
      return blocked ? `${blocked} blocked` : null
    }
  }
}

export function FactoryOverview({ snapshot, finishedGoods, onEdit, disabled = false, changes = [] }: Props) {
  const shared = { snapshot, onEdit, disabled }
  const zone = snapshot.profile.timezone
  return <div className="factory-overview factory-layout">
    <aside className="factory-rail" aria-label="Shop floor navigation">
      <nav className="factory-section-nav" aria-label="Jump to a business group">
        {factorySections.map(section => {
          const alert = sectionAlert(section.id, snapshot)
          return <button key={section.id} type="button" onClick={() => document.getElementById(`factory-${section.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
            <b>{section.title}</b><span>{sectionSummary(section.id, snapshot)}</span>{alert ? <em>{alert}</em> : null}
          </button>
        })}
      </nav>
      <section className="factory-changes" aria-label="Disruptions this run">
        <h2>Disruptions this run</h2>
        {changes.length ? <ol>{changes.map((item, index) => <li key={`${item.at}:${index}`}><time>{shortDateTime(item.at, zone)}</time><span>{item.label}</span></li>)}</ol> : <p className="muted">No disruptions yet</p>}
      </section>
    </aside>
    <div className="factory-main">
      <h1 className="visually-hidden">Shop floor</h1>
      {factorySections.map(section => <section key={section.id} id={`factory-${section.id}`} className="factory-business-group" aria-labelledby={`factory-${section.id}-title`}>
        <header className="factory-business-group-header">
          <h2 id={`factory-${section.id}-title`}>{section.title}</h2>
          <span>{sectionSummary(section.id, snapshot)}</span>
        </header>
        {section.id === 'orders' ? <FactoryOrders {...shared} /> : null}
        {section.id === 'supply' ? <FactorySupply {...shared} finishedGoods={finishedGoods} /> : null}
        {section.id === 'capacity' ? <FactoryCapacity {...shared} /> : null}
        {section.id === 'execution' ? <FactoryExecution {...shared} /> : null}
      </section>)}
    </div>
  </div>
}
