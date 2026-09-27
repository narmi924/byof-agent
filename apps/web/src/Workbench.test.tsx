import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Workbench } from './Workbench'
import * as api from './api'
import { boardSnapshot } from './test/boardFixture'
import { baselineEffective } from './test/preferenceFixture'
import { declaredExecutionSupport } from './test/executionFixture'

vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(),
  readFactories: vi.fn(), readWorkspace: vi.fn(), readHumanTasks: vi.fn(),
  readAssistant: vi.fn(), readCases: vi.fn(), readRiskSuggestions: vi.fn(), readSimulator: vi.fn(), readRecoveryRequests: vi.fn(), commandSimulator: vi.fn(),
}))

const snapshot = boardSnapshot()
const factoryId = snapshot.factory_id
const workspace = { snapshot, freshness: 'CURRENT' as const, last_synced_at: snapshot.snapshot_clock,
  candidates: [], jobs: [], publications: [], objective_state: baselineEffective,
  execution_support: declaredExecutionSupport() }

function open(roles: string[], path = '/agent/chat', authorizedIds = [factoryId]) {
  window.history.replaceState(null, '', path)
  vi.mocked(api.readFactories).mockResolvedValue(authorizedIds.map(id => ({ factory_id: id, roles, last_synced_at: null, snapshot_id: snapshot.snapshot_id })))
  return render(<Workbench username="demo" userId="demo-user" onSessionEnded={vi.fn()} />)
}

beforeEach(() => {
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.mocked(api.readWorkspace).mockResolvedValue(structuredClone(workspace))
  vi.mocked(api.readHumanTasks).mockResolvedValue([])
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning: { weights: { delivery: 0, stability: 0, overtime: 0 }, samples: 0, active: false, explicit: null, summary: '' }, material_balance: { snapshot_id: null, shortfalls: [] } })
  vi.mocked(api.readCases).mockResolvedValue([])
  vi.mocked(api.readRiskSuggestions).mockResolvedValue({ run_id: snapshot.run_id!, freshness: 'CURRENT', suggestions: [] })
  vi.mocked(api.readSimulator).mockResolvedValue({ factory_id: factoryId, run_id: snapshot.run_id!, mode: 'RUNNING', interval_ms: 5000, business_clock: snapshot.snapshot_clock })
  vi.mocked(api.readRecoveryRequests).mockResolvedValue({ run_id: snapshot.run_id!, requests: [] })
  vi.mocked(api.commandSimulator).mockResolvedValue({} as never)
})

describe('manager and disruption simulator workbench', () => {
  it.each(['business-demand', 'business-material', 'business-urgent'])('an old manager %s bookmark moves to the same shop floor, keeps the timeline date and grouping, and drops other-factory entities', async legacy => {
    open(['manager', 'planner'], `/agent/timeline?factory_id=${legacy}&day=2026-09-15&group=worker&candidate=foreign-candidate&operation=foreign-operation&case_id=foreign-case&task_id=foreign-task`, [legacy, factoryId])
    await screen.findByRole('navigation', { name: 'Workbench navigation' })
    await waitFor(() => expect(new URLSearchParams(window.location.search).get('factory_id')).toBe(factoryId))
    expect(window.location.pathname).toBe('/agent/timeline')
    const search = new URLSearchParams(window.location.search)
    expect(search.get('day')).toBe('2026-09-15')
    expect(search.get('group')).toBe('worker')
    for (const name of ['candidate', 'operation', 'case_id', 'task_id']) expect(search.has(name)).toBe(false)
    expect(screen.queryByRole('combobox', { name: 'Current factory' })).not.toBeInTheDocument()
    expect(screen.getByRole('main')).toBeVisible()
    expect(screen.queryByText(legacy)).not.toBeInTheDocument()
    expect(api.readWorkspace).toHaveBeenCalledWith(factoryId, expect.any(AbortSignal))
    expect(vi.mocked(api.readWorkspace).mock.calls.every(([id]) => id === factoryId)).toBe(true)
  })

  it.each(['business-demand', 'business-material', 'business-urgent'])('an old administrator %s shop floor bookmark only reads the same authorized factory', async legacy => {
    open(['maintainer', 'sim_admin'], `/factory/simulator?factory_id=${legacy}&case_id=foreign-case&task_id=foreign-task&candidate=foreign-candidate`, [legacy, factoryId])
    await screen.findByRole('heading', { name: 'Shop floor' })
    expect(screen.queryAllByRole('form').filter(form => !form.closest('.sim-controls'))).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Change order SO-001' })).toBeVisible()
    await waitFor(() => expect(window.location.search).toBe('?factory_id=skf-workshop'))
    expect(window.location.pathname).toBe('/factory/facts')
    expect(screen.queryByRole('combobox', { name: 'Current factory' })).not.toBeInTheDocument()
    expect(api.readSimulator).toHaveBeenCalledWith(factoryId, expect.any(AbortSignal))
    expect(vi.mocked(api.readWorkspace).mock.calls.every(([id]) => id === factoryId)).toBe(true)
    expect(vi.mocked(api.readSimulator).mock.calls.every(([id]) => id === factoryId)).toBe(true)
    expect(api.readAssistant).not.toHaveBeenCalled()
  })

  it('going back to an old scenario address in the browser still reads no other factory data', async () => {
    open(['manager', 'planner'], '/agent/chat', ['business-urgent', factoryId])
    await screen.findByRole('region', { name: 'Production Agent' })
    window.history.pushState(null, '', '/agent/timeline?factory_id=business-urgent&group=order&candidate=foreign-candidate&operation=foreign-operation')
    fireEvent.popState(window)
    await waitFor(() => expect(window.location.search).toBe('?factory_id=skf-workshop&group=order'))
    expect(window.location.pathname).toBe('/agent/timeline')
    expect(vi.mocked(api.readWorkspace).mock.calls.every(([id]) => id === factoryId)).toBe(true)
  })

  it('refreshing and switching from manager to administrator keeps the same shop floor without a factory choice', async () => {
    const authorized = ['business-demand', 'business-material', 'business-urgent', factoryId]
    const manager = open(['manager', 'planner'], '/agent/chat?factory_id=business-demand', authorized)
    await screen.findByRole('region', { name: 'Production Agent' })
    await waitFor(() => expect(window.location.search).toBe('?factory_id=skf-workshop'))
    const address = window.location.pathname + window.location.search
    manager.unmount()
    const refreshed = open(['manager', 'planner'], address, authorized)
    await screen.findByRole('region', { name: 'Production Agent' })
    expect(window.location.search).toBe('?factory_id=skf-workshop')
    refreshed.unmount()
    open(['maintainer', 'sim_admin'], address, authorized)
    await screen.findByRole('heading', { name: 'Shop floor' })
    expect(window.location.pathname).toBe('/factory/facts')
    expect(window.location.search).toBe('?factory_id=skf-workshop')
    expect(screen.queryByRole('combobox', { name: 'Current factory' })).not.toBeInTheDocument()
    expect(vi.mocked(api.readWorkspace).mock.calls.every(([id]) => id === factoryId)).toBe(true)
  })

  it.each([{ ids: [] }, { ids: ['business-urgent', 'another-factory'] }])('without product factory access it neither falls back to another factory nor reads the shop floor %#', async ({ ids }) => {
    open(['manager', 'planner'], '/agent/chat?factory_id=business-urgent', ids)
    expect(await screen.findByText(/This account has no access to the SKF shop floor/)).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Workbench navigation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Choose an accessible factory' })).not.toBeInTheDocument()
    expect(api.readWorkspace).not.toHaveBeenCalled()
    expect(api.readAssistant).not.toHaveBeenCalled()
    expect(api.readSimulator).not.toHaveBeenCalled()
    vi.mocked(api.readFactories).mockResolvedValue([{ factory_id: factoryId, roles: ['manager', 'planner'], last_synced_at: null, snapshot_id: null }])
    await userEvent.click(screen.getByRole('button', { name: 'Check access again' }))
    await screen.findByRole('region', { name: 'Production Agent' })
    expect(api.readWorkspace).toHaveBeenCalledWith(factoryId, expect.any(AbortSignal))
  })

  it('manager navigation only offers the conversation, timeline and execution log; switching pages neither reloads nor unmounts the sidebar', async () => {
    open(['manager', 'planner'])
    const navigation = await screen.findByRole('navigation', { name: 'Workbench navigation' })
    const sidebar = screen.getByRole('complementary', { name: 'Conversation sidebar' })
    expect(screen.getByRole('region', { name: 'Production Agent' })).toBeVisible()
    await userEvent.click(within(navigation).getByRole('button', { name: 'Execution log' }))
    expect(window.location.pathname).toBe('/agent/records')
    expect(await screen.findByRole('heading', { name: 'Execution log' })).toBeVisible()
    expect(screen.getByRole('complementary', { name: 'Conversation sidebar' })).toBe(sidebar)
    await userEvent.click(screen.getByRole('button', { name: 'New conversation' }))
    expect(window.location.pathname).toBe('/agent/chat')
    expect(screen.getByRole('region', { name: 'Production Agent' })).toBeVisible()
    expect(within(navigation).queryByRole('button', { name: /factory|shop floor/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New order' })).not.toBeInTheDocument()
    expect(api.readSimulator).not.toHaveBeenCalled()
  })

  it('the sidebar hides completely, leaving only a show button at the top left, and stays hidden after a refresh', async () => {
    localStorage.removeItem('byof.sidebar')
    const first = open(['manager', 'planner'])
    await screen.findByRole('complementary', { name: 'Conversation sidebar' })
    expect(screen.queryByRole('button', { name: 'Show sidebar' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Hide sidebar' }))
    expect(screen.queryByRole('complementary', { name: 'Conversation sidebar' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Production Agent' })).toBeVisible()
    first.unmount()
    open(['manager', 'planner'])
    const show = await screen.findByRole('button', { name: 'Show sidebar' })
    expect(screen.queryByRole('complementary', { name: 'Conversation sidebar' })).not.toBeInTheDocument()
    await userEvent.click(show)
    expect(screen.getByRole('complementary', { name: 'Conversation sidebar' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Show sidebar' })).not.toBeInTheDocument()
    localStorage.removeItem('byof.sidebar')
  })

  it('the administrator maintains the shop floor directly from a machine row; viewing and acting are no longer two pages', async () => {
    open(['maintainer', 'sim_admin'])
    await screen.findByRole('heading', { name: 'Shop floor' })
    await waitFor(() => expect(window.location.pathname).toBe('/factory/facts'))
    expect(screen.queryByRole('navigation', { name: 'Workbench navigation' })).not.toBeInTheDocument()
    expect(screen.queryAllByRole('form').filter(form => !form.closest('.sim-controls'))).toHaveLength(0)
    expect(screen.getByRole('heading', { level: 2, name: 'Machines and staff' })).toBeVisible()
    expect(new URLSearchParams(window.location.search).has('module')).toBe(false)
    const edit = screen.getByRole('button', { name: 'Edit machine KIT-01' })
    await waitFor(() => expect(edit).toBeEnabled())
    await userEvent.click(edit)
    const outage = await screen.findByRole('form', { name: 'Machine status' })
    await userEvent.selectOptions(within(outage).getByLabelText('Machine action'), 'outage')
    await userEvent.click(within(outage).getByRole('button', { name: 'Confirm machine status' }))
    await waitFor(() => expect(api.commandSimulator).toHaveBeenCalledWith(factoryId,
      expect.objectContaining({ kind: 'resource.outage', payload: { resource_id: 'KIT-01', minutes: 30 } }), expect.any(AbortSignal)))
    expect(window.location.pathname).toBe('/factory/facts')
  })

  it('old category addresses return to the same factory page where all four groups can be acted on', async () => {
    open(['maintainer', 'sim_admin'], '/factory/facts?factory_id=skf-workshop&module=supply')
    expect(await screen.findByRole('heading', { level: 1, name: 'Shop floor' })).toBeVisible()
    await waitFor(() => expect(window.location.search).toBe('?factory_id=skf-workshop'))
    expect(screen.getByRole('heading', { level: 2, name: 'Inventory and supply' })).toBeVisible()
    expect(screen.getByRole('table', { name: 'Current raw materials' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Change order SO-001' })).toBeVisible()
  })

  it('old specialist page addresses return to the core page of the role', async () => {
    open(['manager', 'planner'], '/factory/plan')
    await waitFor(() => expect(window.location.pathname).toBe('/agent/chat'))
    expect(screen.getByRole('region', { name: 'Production Agent' })).toBeVisible()
  })

  it('other old roles do not enter the new product flow', async () => {
    open(['planner'])
    expect(await screen.findByRole('heading', { name: 'Choose a role' })).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Workbench navigation' })).not.toBeInTheDocument()
  })

  it('the administrator cancels an order in place and after saving returns to the list and sees qualified finished goods', async () => {
    open(['maintainer', 'sim_admin'], '/factory/simulator?factory_id=skf-workshop')
    const edit = await screen.findByRole('button', { name: 'Change order SO-001' })
    await waitFor(() => expect(edit).toBeEnabled())
    await userEvent.click(edit)
    const form = await screen.findByRole('form', { name: 'Change order' })
    await waitFor(() => expect(within(form).getByRole('button', { name: 'Confirm order change' })).toBeEnabled())
    const after = { ...structuredClone(workspace), finished_goods: [{ batch_id: 'SURPLUS-1', product_id: snapshot.orders[0]!.product_id, quantity: 50, completed_at: snapshot.snapshot_clock }], snapshot: { ...structuredClone(snapshot), orders: [{ ...snapshot.orders[0]!, quantity: 0, status: 'CANCELLED', version: snapshot.orders[0]!.version + 1 }], production_batches: [{ batch_id: 'SURPLUS-1', order_id: snapshot.orders[0]!.order_id, product_id: snapshot.orders[0]!.product_id, route_version: snapshot.profile.products[0]!.route_version, quantity: 50, sequence: 1, purpose: 'STOCK' as const }] } }
    vi.mocked(api.commandSimulator).mockImplementationOnce(async () => { vi.mocked(api.readWorkspace).mockResolvedValue(after) })
    await userEvent.clear(within(form).getByRole('spinbutton', { name: /Demand quantity/ }))
    await userEvent.type(within(form).getByRole('spinbutton', { name: /Demand quantity/ }), '0')
    await userEvent.click(within(form).getByRole('checkbox', { name: /Confirm cancelling .* SO-001/ }))
    await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(window.location.pathname).toBe('/factory/facts')
    const orders = await screen.findByRole('table', { name: 'Current orders · 1' })
    expect(within(orders).getByRole('row', { name: /SO-001/ })).toHaveTextContent('Cancelled')
    expect(within(orders).getByRole('row', { name: /SO-001/ })).toHaveTextContent('50 pcs to stock')
    expect(screen.getByRole('table', { name: 'Source-verified qualified surplus · 50 pcs' })).toBeVisible()
    expect(api.commandSimulator).toHaveBeenCalledTimes(1)
  })
})

it('the execution log reads the workspace without the chat filter, and the empty state fakes no executed plan', async () => {
  open(['manager', 'planner'], '/agent/records')
  expect(await screen.findByRole('heading', { level: 1, name: 'Execution log' })).toBeVisible()
  expect(await screen.findByText('No executions yet')).toBeVisible()
  expect(api.readWorkspace).toHaveBeenCalledWith(factoryId, expect.any(AbortSignal))
  expect(api.readSimulator).not.toHaveBeenCalled()
})
