import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { FactoryOverview } from './FactoryOverview'
import type { FactoryEditor } from './factoryEditor'
import type { FinishedGoodsLot, Snapshot } from './contracts'
import { actual, boardSnapshot, day } from './test/boardFixture'
import { businessTerms } from './test/businessFixture'

function overviewSnapshot(): Snapshot {
  const base = boardSnapshot()
  return boardSnapshot({
    schema_version: 'byof.snapshot/3',
    business_terms: businessTerms,
    orders: [...base.orders, { ...base.orders[0]!, order_id: 'SO-SECOND', quantity: 50, status: 'CONFIRMED' }],
    receipts: [
      { receipt_id: 'RCPT-1', material_id: 'MAT-RING', quantity: 100, unit: 'EA', eta: `${day}T08:00:00Z`, status: 'CONFIRMED', received_at: null, version: 1 },
      { receipt_id: 'RCPT-2', material_id: 'MAT-RING', quantity: 50, unit: 'EA', eta: `${day}T09:00:00Z`, status: 'EXPECTED', received_at: null, version: 1 },
    ],
    actuals: [
      actual({ operation_id: 'SO-001-R001-B001-OP10', state: 'COMPLETED', completed_quantity: 50, quality_state: 'PENDING', actual_end: `${day}T01:00:00Z` }),
      actual({ operation_id: 'SO-001-R001-B001-OP20', state: 'BLOCKED', completed_quantity: 10, remaining_minutes: null, remaining_setup_minutes: 0 }),
    ],
  })
}

function Harness({ snapshot, finishedGoods, disabled, onEdit }: { snapshot: Snapshot; finishedGoods?: FinishedGoodsLot[] | null; disabled: boolean; onEdit: (editor: FactoryEditor) => void }) {
  return <FactoryOverview snapshot={snapshot} finishedGoods={finishedGoods} onEdit={onEdit} disabled={disabled} />
}

function open(snapshot = overviewSnapshot(), finishedGoods: FinishedGoodsLot[] | null = [], disabled = false) {
  const onEdit = vi.fn()
  const result = render(<Harness snapshot={snapshot} finishedGoods={finishedGoods} disabled={disabled} onEdit={onEdit} />)
  return { ...result, onEdit }
}

describe('the shop floor is organized by business module', () => {
  it('the four record groups and their actions are on one page; jumping within the page does not switch subpages', () => {
    open()
    expect(screen.getByRole('heading', { level: 1, name: 'Shop floor' })).toBeVisible()
    for (const title of ['Orders and delivery', 'Inventory and supply', 'Machines and staff', 'Execution and quality']) expect(screen.getByRole('heading', { level: 2, name: title })).toBeVisible()
    const navigation = screen.getByRole('navigation', { name: 'Jump to a business group' })
    expect(within(navigation).getByRole('button', { name: /Inventory and supply/ })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^Enter/ })).not.toBeInTheDocument()
    expect(screen.getAllByText('2 orders · 1 product')[0]).toBeVisible()
    expect(within(navigation).getByRole('button', { name: /Execution and quality/ })).toHaveTextContent('1 blocked')
    expect(screen.getByRole('table', { name: 'Current orders · 2' })).toBeVisible()
    expect(screen.getByRole('table', { name: 'Current raw materials' })).toBeVisible()
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
  })

  it('disruptions start only from the object records of the four groups with no duplicate entry; the left rail records the disruptions of this run', () => {
    open()
    expect(screen.queryByRole('region', { name: 'Create disruptions' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Disruptions this run' })).toHaveTextContent('No disruptions yet')
  })

  it('each action is bound to its source record and demo tools do not mix into business cards', async () => {
    const { onEdit } = open()
    expect(screen.queryByRole('button', { name: /Demo tools|New demo run|Replay/ })).not.toBeInTheDocument()
    const groups: [string, [string, FactoryEditor][]][] = [
      ['Orders and delivery', [
        ['New order', { kind: 'new-order' }],
        ['Change order SO-SECOND', { kind: 'order', orderId: 'SO-SECOND' }],
        ['Split-delivery rule BRG-6202', { kind: 'delivery-rule', productId: 'BRG-6202' }],
      ]],
      ['Inventory and supply', [
        ['Count MAT-RING', { kind: 'inventory', materialId: 'MAT-RING' }],
        ['Record receipt', { kind: 'new-receipt' }],
        ['Update receipt RCPT-2', { kind: 'receipt', receiptId: 'RCPT-2' }],
        ['New quote RCPT-2', { kind: 'quote', receiptId: 'RCPT-2' }],
        ['Edit quote Q1', { kind: 'quote', receiptId: 'RCPT-1', quoteId: 'Q1' }],
      ]],
      ['Machines and staff', [
        ['Edit machine ASM-01', { kind: 'resource', resourceId: 'ASM-01' }],
        ['Edit worker W02', { kind: 'worker', workerId: 'W02' }],
        ['Overtime windows machine ASM-01', { kind: 'overtime', targetType: 'resource', targetId: 'ASM-01' }],
        ['Overtime windows worker W02', { kind: 'overtime', targetType: 'worker', targetId: 'W02' }],
      ]],
      ['Execution and quality', [
        ['Confirm remaining SO-001-R001-B001-OP20', { kind: 'remaining', operationId: 'SO-001-R001-B001-OP20' }],
        ['Record quality SO-001-R001-B001-OP10', { kind: 'quality', operationId: 'SO-001-R001-B001-OP10' }],
      ]],
    ]
    for (const [title, actions] of groups) {
      expect(screen.getByRole('heading', { level: 2, name: title })).toBeVisible()
      for (const [label, target] of actions) {
        await userEvent.click(screen.getByRole('button', { name: label }))
        expect(onEdit).toHaveBeenLastCalledWith(target)
      }
    }
  })

  it('material search covers all records; quote and receipt states keep their source basis', async () => {
    const snapshot = overviewSnapshot()
    snapshot.profile.materials = Array.from({ length: 25 }, (_, index) => ({ material_id: `MAT-${String(index + 1).padStart(2, '0')}`, name: index === 24 ? 'Grease' : `Material ${index + 1}`, unit: 'EA' }))
    snapshot.inventory = snapshot.profile.materials.map(item => ({ material_id: item.material_id, unit: item.unit, on_hand: 100, reserved: 30, version: 1 }))
    snapshot.business_terms = { ...businessTerms, expedite_quotes: [{ ...businessTerms.expedite_quotes[0]!, cost_minor: null, currency: null, receipt_version: 2 }] }
    snapshot.receipts[1] = { ...snapshot.receipts[1]!, status: 'RECEIVED', received_at: snapshot.snapshot_clock }
    open(snapshot)
    const table = screen.getByRole('table', { name: 'Current raw materials' })
    expect(within(table).getAllByRole('row')).toHaveLength(26)
    const search = screen.getByRole('searchbox', { name: 'Search material name or ID' })
    await userEvent.type(search, 'Grease')
    expect(within(table).getAllByRole('row')).toHaveLength(2)
    fireEvent.change(search, { target: { value: 'mat-03' } })
    expect(screen.getByRole('button', { name: 'Count MAT-03' })).toBeVisible()
    fireEvent.change(search, { target: { value: 'no such material' } })
    expect(screen.getByText('No matching materials.')).toBeVisible()
    expect(screen.getByText(/Cost not confirmed/)).toBeVisible()
    expect(screen.getByText(/Receipt changed; verify again/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Update receipt RCPT-2' })).not.toBeInTheDocument()
  })

  it('batch outcomes and qualified finished goods are accounted separately; unknown goods are not shown as zero', () => {
    const snapshot = overviewSnapshot()
    snapshot.production_batches = [
      { batch_id: 'STOCK-1', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 1, purpose: 'STOCK' },
      { batch_id: 'CANCELLED-1', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 2, purpose: 'CANCELLED' },
    ]
    const { rerender, onEdit } = open(snapshot, null)
    expect(screen.getByText('50 pcs to stock; 50 pcs of unstarted work cancelled')).toBeVisible()
    expect(screen.getByText(/The source has not reported qualified finished goods; they cannot be treated as zero/)).toBeVisible()
    rerender(<Harness snapshot={snapshot} finishedGoods={[{ batch_id: 'SOURCE-CERTIFIED-LOT', product_id: 'BRG-6202', quantity: 50, completed_at: snapshot.snapshot_clock }]} onEdit={onEdit} disabled={false} />)
    const goods = screen.getByRole('table', { name: 'Source-verified qualified surplus · 50 pcs' })
    expect(within(goods).getByText('SOURCE-CERTIFIED-LOT')).toBeVisible()
    expect(within(goods).queryByText('STOCK-1')).not.toBeInTheDocument()
  })

  it('when writing is disabled all groups can still be viewed, searched and filtered to blocked operations', async () => {
    const { onEdit } = open(overviewSnapshot(), [], true)
    expect(screen.getByRole('button', { name: 'Confirm remaining SO-001-R001-B001-OP20' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Record quality SO-001-R001-B001-OP10' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'New order' })).toBeDisabled()
    expect(screen.getByRole('table', { name: 'Current orders · 2' })).toBeVisible()
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search orders or products' }), 'SO-SECOND')
    expect(screen.getByRole('table', { name: 'Current orders · 1' })).toBeVisible()
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Show' }), 'blocked')
    const execution = screen.getByRole('table', { name: 'Shop floor execution records · 1' })
    expect(within(execution).getByRole('button', { name: 'Confirm remaining SO-001-R001-B001-OP20' })).toBeDisabled()
    expect(within(execution).queryByRole('button', { name: 'Record quality SO-001-R001-B001-OP10' })).not.toBeInTheDocument()
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('an empty shop floor gives a next step in every module and fakes no records', () => {
    const base = boardSnapshot()
    const snapshot = boardSnapshot({ profile: { ...base.profile, products: [], materials: [], routes: [] }, orders: [], inventory: [], receipts: [], resources: [], workers: [], actuals: [], production_batches: [] })
    open(snapshot, null)
    for (const [title, explanation] of [
      ['Orders and delivery', /No orders/], ['Inventory and supply', /No raw material records/],
      ['Machines and staff', /No machine records/], ['Execution and quality', /No execution records/],
    ] as const) {
      expect(screen.getByRole('heading', { level: 2, name: title })).toBeVisible()
      expect(screen.getByText(explanation)).toBeVisible()
    }
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
