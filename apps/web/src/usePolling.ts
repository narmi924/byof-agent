import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from './api'

export function usePolling<T>(load: (signal: AbortSignal) => Promise<T>, onSessionEnded: () => void) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const active = useRef(false)
  const reading = useRef<AbortController | null>(null)
  const timeout = useRef<number | undefined>(undefined)
  const next = useRef<number | undefined>(undefined)
  const refresh = useCallback(async function read() {
    reading.current?.abort(); window.clearTimeout(timeout.current); window.clearTimeout(next.current)
    const controller = new AbortController(); reading.current = controller
    timeout.current = window.setTimeout(() => { controller.abort(); if (active.current) { setData(null); setError('Reading timed out; refresh to check.') } }, 10_000)
    try {
      const result = await load(controller.signal)
      if (active.current && !controller.signal.aborted) { setData(result); setError('') }
    } catch (reason) {
      if (active.current && !controller.signal.aborted) {
        setData(null); setError(errorMessage(reason, 'Reading failed; check the connection and refresh.'))
        if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
      }
    } finally {
      if (reading.current === controller) { window.clearTimeout(timeout.current); if (active.current) next.current = window.setTimeout(() => { void read() }, 3000) }
    }
  }, [load, onSessionEnded])
  useEffect(() => {
    active.current = true; void refresh()
    return () => { active.current = false; reading.current?.abort(); window.clearTimeout(timeout.current); window.clearTimeout(next.current) }
  }, [refresh])
  return { data, error, refresh }
}
