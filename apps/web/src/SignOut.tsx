import { Button } from './components/ui/button'
import { useEffect, useRef, useState } from 'react'
import { errorMessage, signOut } from './api'

export function SignOut({ onSignedOut }: { onSignedOut: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => { controller.current?.abort(); window.clearTimeout(timer.current) }, [])
  async function submit() {
    if (controller.current) return
    const request = new AbortController()
    controller.current = request
    setBusy(true)
    setError('')
    const timeout = window.setTimeout(() => { request.abort(); onSignedOut() }, 10_000)
    timer.current = timeout
    try {
      await signOut(request.signal)
      if (!request.signal.aborted) onSignedOut()
    } catch (reason: unknown) {
      if (!request.signal.aborted) setError(errorMessage(reason, 'Sign-out could not be confirmed; reconnect to check.'))
    } finally {
      window.clearTimeout(timeout)
      if (!request.signal.aborted) { setBusy(false); controller.current = null }
    }
  }
  return <div className="popover-anchor signout">
    <Button variant="outline" type="button" className="secondary" disabled={busy} onClick={() => { void submit() }}>{busy ? 'Signing out…' : 'Sign out'}</Button>
    {error ? <p className="popover" role="alert">{error}</p> : null}
  </div>
}
