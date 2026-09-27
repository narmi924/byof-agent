import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { FactoryEditorDialog } from './FactoryEditorDialog'

it('keeps focus in the dialog while editing and returns it to the record button on close', async () => {
  const opener = document.createElement('button')
  document.body.append(opener); opener.focus()
  const onClose = vi.fn()
  const view = render(<FactoryEditorDialog label="Change order SO-001" onClose={onClose}><label>Quantity<input /></label><button>Save</button><p tabIndex={-1}>Check hint</p></FactoryEditorDialog>)
  expect(screen.getByRole('dialog')).toHaveFocus()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Close editor' })).toHaveFocus()
  await userEvent.tab({ shift: true })
  expect(screen.getByRole('button', { name: 'Save' })).toHaveFocus()
  screen.getByText('Check hint').focus()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Close editor' })).toHaveFocus()
  await userEvent.keyboard('{Escape}')
  expect(onClose).toHaveBeenCalledOnce()
  view.unmount()
  expect(opener).toHaveFocus()
  opener.remove()
})

it('keeps the form while input is unsaved and only closes on an explicit discard', async () => {
  const onClose = vi.fn()
  render(<FactoryEditorDialog label="Change order" dirty onClose={onClose}><input defaultValue="50" /></FactoryEditorDialog>)
  await userEvent.click(screen.getByRole('button', { name: 'Close editor' }))
  expect(onClose).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('unsaved')
  await userEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
  expect(screen.getByRole('textbox')).toHaveValue('50')
  await userEvent.keyboard('{Escape}')
  await userEvent.click(screen.getByRole('button', { name: 'Discard and close' }))
  expect(onClose).toHaveBeenCalledOnce()
})

it('cannot close while the result is being checked; closes directly once submitted without unsaved changes', async () => {
  const onClose = vi.fn()
  const view = render(<FactoryEditorDialog label="Enter quote" busy dirty onClose={onClose}><fieldset disabled><input aria-label="Quote amount" /><button>Save quote</button></fieldset></FactoryEditorDialog>)
  await userEvent.keyboard('{Escape}')
  expect(onClose).not.toHaveBeenCalled()
  await userEvent.tab()
  expect(screen.getByRole('dialog')).toHaveFocus()
  expect(screen.getByRole('button', { name: 'Close editor' })).toBeDisabled()
  view.rerender(<FactoryEditorDialog label="Enter quote" onClose={onClose}><p>Confirmed</p></FactoryEditorDialog>)
  await userEvent.click(screen.getByRole('button', { name: 'Close editor' }))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
})
