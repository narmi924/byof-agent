import { Button } from './components/ui/button'
import { useCallback, useEffect, useState } from 'react'
import { entryRole, readConnection, signIn } from './api'
import type { Connection } from './api'
import { Workbench } from './Workbench'
import { BrandMark } from './ui'
import { appNames } from './copy'

type ConnectionState =
  | { kind: 'loading' }
  | { kind: 'loaded'; value: Connection }
  | { kind: 'error'; message: string }

export function App() {
  const [attempt, setAttempt] = useState(0)
  const [connection, setConnection] = useState<ConnectionState>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = window.setTimeout(() => {
      controller.abort()
      if (active) setConnection({ kind: 'error', message: 'Connection timed out. Check the network and service status, then retry.' })
    }, 10_000)

    void readConnection(controller.signal)
      .then(async (value) => {
        if (value.system.state === 'ready' && !value.session && !controller.signal.aborted) {
          await signIn(entryRole(), controller.signal)
          return readConnection(controller.signal)
        }
        return value
      })
      .then((value) => {
        if (active && !controller.signal.aborted) setConnection({ kind: 'loaded', value })
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) return
        const message = error instanceof Error && !(error instanceof TypeError)
          && !(error instanceof SyntaxError)
          ? error.message
          : 'Cannot reach the workbench service. Check the network and service status, then retry.'
        setConnection({ kind: 'error', message })
        controller.abort()
      })
      .finally(() => window.clearTimeout(timeout))

    return () => {
      active = false
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [attempt])

  const reconnect = useCallback(() => {
    setConnection({ kind: 'loading' })
    setAttempt((current) => current + 1)
  }, [])
  /** A 401 from a business endpoint only clears the signed-in identity in place; it does not re-query the session.
   *  Otherwise a valid session without business permission would reconnect forever. */
  const sessionEnded = useCallback(() => {
    setConnection({ kind: 'error', message: 'The session has expired; reconnect.' })
  }, [])

  const loading = connection.kind === 'loading'
  const value = connection.kind === 'loaded' ? connection.value : undefined
  const session = value?.session

  if (value?.system.state === 'ready' && session) {
    return <Workbench
      username={session.username}
      userId={session.user_id}
      onSessionEnded={sessionEnded}
    />
  }

  if (loading) return <div className="app-loading" role="status" aria-label="Connecting" />
  const entryName = entryRole() === 'maintainer' ? appNames.simulator : appNames.agent
  return <div className="connection-page">
    <BrandMark /><h1>{entryName}</h1>
    {connection.kind === 'error' ? <p role="alert">{connection.message}</p> : null}
    {value?.system.state === 'setup_required' ? <p role="alert">{value.system.message}</p> : null}
    <Button variant="outline" type="button" onClick={reconnect}>Reconnect</Button>
  </div>
}
