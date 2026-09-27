import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { ModelSwitch } from './ModelSwitch'
import { changeModelSelection, readModelSelection } from './api'
import { parseModelSelection } from './modelContracts'

vi.mock('./api', () => ({ readModelSelection: vi.fn(), changeModelSelection: vi.fn() }))
const initial = { selected_model_id: 'deepseek', version: 0, scope: 'CURRENT_USER' as const, takes_effect: 'NEXT_TURN' as const,
  models: [{ model_id: 'deepseek', label: 'DeepSeek Flash', available: true }, { model_id: 'claude', label: 'Claude', available: true }] }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(readModelSelection).mockResolvedValue(initial) })

it('saves the account preference with a version and no configuration fields', async () => {
  vi.mocked(changeModelSelection).mockResolvedValue({ ...initial, selected_model_id: 'claude', version: 1 })
  render(<ModelSwitch />)
  const select = screen.getByRole('combobox')
  await waitFor(() => expect(select).toHaveValue('deepseek'))
  fireEvent.change(select, { target: { value: 'claude' } })
  await waitFor(() => expect(select).toHaveValue('claude'))
  expect(changeModelSelection).toHaveBeenCalledWith('claude', 0, expect.any(String), expect.any(AbortSignal))
  expect(screen.getByText(/All conversations of this account/)).toBeInTheDocument()
})

it('reconciles a conflicting or uncertain write without falsely displaying success', async () => {
  vi.mocked(changeModelSelection).mockRejectedValue(new Error('Another window switched the model'))
  render(<ModelSwitch />)
  await waitFor(() => expect(screen.getByRole('combobox')).toHaveValue('deepseek'))
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'claude' } })
  expect(await screen.findByRole('alert')).toHaveTextContent('Another window switched the model')
  expect(screen.getByRole('combobox')).toHaveValue('deepseek')
  expect(readModelSelection).toHaveBeenCalledTimes(2)
})

it('disables an unconfigured provider and refreshes on window focus', async () => {
  vi.mocked(readModelSelection).mockResolvedValue({ ...initial, models: initial.models.map(m => ({ ...m, available: m.model_id === 'deepseek' })) })
  render(<ModelSwitch />)
  expect(await screen.findByRole('option', { name: 'Claude (not configured)' })).toBeDisabled()
  fireEvent.focus(window)
  await waitFor(() => expect(readModelSelection).toHaveBeenCalledTimes(2))
})

it('rejects malformed or duplicate model menus', () => {
  expect(() => parseModelSelection({ ...initial, version: -1 })).toThrow()
  expect(() => parseModelSelection({ ...initial, models: [initial.models[0], initial.models[0]] })).toThrow()
})
