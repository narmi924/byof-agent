import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App } from '../App'

/** Simulates a user reconnecting the workbench: unmount and mount again; pending requests can only be recovered from the local record, never resent.
 *  This path replaces the old "Reconnect" button; the new shell puts the recovery entry for a failed connection on the matching message strip. */
export async function reconnectWorkbench(view = 'Plan'): Promise<void> {
  cleanup()
  render(<App />)
  await screen.findByRole('button', { name: 'Refresh data' })
  if (view) await userEvent.click(screen.getByRole('button', { name: view }))
}
