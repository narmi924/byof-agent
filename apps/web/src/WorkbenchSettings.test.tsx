import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { WorkbenchSettings } from './WorkbenchSettings'

vi.mock('./ModelSwitch', () => ({ ModelSwitch: () => <label>Agent model<select defaultValue="deepseek"><option value="deepseek">DeepSeek</option></select></label> }))

it('settings are closed by default, work with the keyboard when open, and Escape returns to the trigger', async () => {
  render(<WorkbenchSettings><button type="button">Recommendation preference</button></WorkbenchSettings>)
  const trigger = screen.getByRole('button', { name: 'Settings' })
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  await userEvent.click(trigger)
  expect(screen.getByRole('dialog', { name: 'Settings' })).toBeVisible()
  expect(screen.getByRole('combobox', { name: 'Agent model' })).toBeVisible()
  await userEvent.keyboard('{Escape}')
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(trigger).toHaveFocus()
})
