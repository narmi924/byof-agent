import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { App } from './App'

const ready = { name: 'BYOF', version: '1.0', state: 'ready', message: 'Service connected.' }
function server({ initialized = true, existing = false, denied = false } = {}) {
  let signedIn = existing
  const request = vi.fn<typeof fetch>(async (url) => {
    if (url === '/api/system/status') return Response.json(ready)
    if (url === '/api/session') return signedIn ? Response.json({ username: 'demo', user_id: 'demo-user', grants: [] }) : new Response(null, { status: 401 })
    if (url === '/api/role-session') { if (!initialized) return new Response(null, { status: 503 }); signedIn = true; return Response.json({ csrf_token: 'test-csrf' }) }
    if (url === '/api/csrf') return Response.json({ csrf_token: 'test-csrf' })
    if (url === '/api/factories') return denied ? new Response(null, { status: 401 }) : Response.json({ factories: [] })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', request)
  return request
}

describe('direct route entry', () => {
  it.each([['/agent/chat', 'manager', 'agent'], ['/factory/facts', 'maintainer', 'simulator']])('%s creates its own demo session', async (path, role, surface) => {
    window.history.replaceState(null, '', path)
    const request = server()
    render(<App />)
    await screen.findByText(/This account has no access to the SKF/)
    expect(request).toHaveBeenCalledWith('/api/role-session', expect.objectContaining({ body: JSON.stringify({ role }), headers: expect.objectContaining({ 'X-BYOF-Surface': surface }) }))
    for (const [, init] of request.mock.calls) expect(init?.headers).toMatchObject({ 'X-BYOF-Surface': surface })
    expect(screen.queryByText('Choose a demo identity')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })
  it('reuses an existing session without choosing the identity again', async () => {
    const request = server({ existing: true }); render(<App />)
    await screen.findByText(/This account has no access to the SKF/)
    expect(request.mock.calls.filter(([url]) => url === '/api/role-session')).toHaveLength(0)
  })
  it('stops and offers a retry when the identities are not set up', async () => {
    const request = server({ initialized: false }); render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The demo identities are not set up yet')
    expect(request.mock.calls.filter(([url]) => url === '/api/role-session')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeEnabled()
  })
  it('a business 401 does not reconnect automatically or clear pending requests', async () => {
    sessionStorage.setItem('byof-pending-test', 'original-request')
    const request = server({ existing: true, denied: true }); render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The session has expired')
    expect(request.mock.calls.filter(([url]) => url === '/api/session')).toHaveLength(1)
    expect(sessionStorage.getItem('byof-pending-test')).toBe('original-request')
  })
  it('a failed connection does not leak the raw network error and a retry recovers', async () => {
    const request = server(); request.mockRejectedValueOnce(new TypeError('private upstream details'))
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the workbench service')
    expect(screen.queryByText('private upstream details')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    await screen.findByText(/This account has no access to the SKF/)
  })
  it('the connecting phase shows only a status, without a duplicate connect button', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    render(<App />)
    expect(screen.getByRole('status', { name: 'Connecting' })).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
  })
  it('allows a retry after a timeout; a late result does not overwrite the error', async () => {
    vi.useFakeTimers()
    let resolve: ((response: Response) => void) | undefined
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(done => { resolve = done })))
    render(<App />)
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(screen.getByRole('alert')).toHaveTextContent('Connection timed out')
    await act(async () => { resolve?.(Response.json(ready)) })
    expect(screen.getByRole('alert')).toHaveTextContent('Connection timed out')
  })
  it('cancels requests on unmount', async () => {
    const request = vi.fn<typeof fetch>(() => new Promise(() => {})); vi.stubGlobal('fetch', request)
    const view = render(<App />)
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    const signal = request.mock.calls[0]?.[1]?.signal
    view.unmount(); expect(signal?.aborted).toBe(true)
  })
  it.each(['invented_ready', 'setup_required'])('configuration state %s creates no session', async state => {
    const request = server(); request.mockImplementation(async url => url === '/api/session' ? new Response(null, { status: 401 }) : Response.json({ ...ready, state, message: '<img src=x onerror=alert(1)>' }))
    render(<App />)
    await screen.findByRole('alert')
    expect(request.mock.calls.filter(([url]) => url === '/api/role-session')).toHaveLength(0)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })
})
