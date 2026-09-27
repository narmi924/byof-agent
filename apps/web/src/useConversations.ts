import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage, readCases } from './api'
import type { CaseRecord } from './caseContracts'

/** Conversation list and selection for the manager; shared by the sidebar and the chat.
 *  A new browser tab starts with a new conversation; a refresh keeps the tab's selection. */
export function useConversations(factoryId: string, userId: string | undefined, runId: string | undefined, onSessionEnded: () => void) {
  const key = `byof.conversation:${userId}:${factoryId}`
  const [selection, setSelection] = useState<string>(() => {
    try { return sessionStorage.getItem(key) || 'new' } catch { return 'new' }
  })
  const [list, setList] = useState<{ cases: CaseRecord[]; loadedAt: number } | null>(null)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const selectedAt = useRef(0)
  const previousRun = useRef(runId)

  const select = useCallback((id: string) => {
    selectedAt.current = performance.now()
    setSelection(id)
    setReload(value => value + 1)
    try { sessionStorage.setItem(key, id) } catch { /* Selection still works for this page. */ }
  }, [key])

  useEffect(() => {
    if (runId && previousRun.current && runId !== previousRun.current) select('new')
    previousRun.current = runId
  }, [runId, select])

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | undefined
    async function load() {
      controller = new AbortController()
      const started = performance.now()
      const timeout = window.setTimeout(() => controller?.abort(), 10_000)
      try {
        const cases = (await readCases(factoryId, controller.signal)).filter(item => item.run_id === runId)
        if (stopped) return
        setList({ cases, loadedAt: started }); setError('')
      } catch (reason) {
        if (stopped) return
        setError(controller.signal.aborted ? 'Reading the conversation list timed out; retrying.' : errorMessage(reason, 'Reading the conversation list was interrupted; retrying.'))
        if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
      } finally {
        window.clearTimeout(timeout)
        if (!stopped) timer = window.setTimeout(() => { void load() }, 4000)
      }
    }
    void load()
    return () => { stopped = true; controller?.abort(); window.clearTimeout(timer) }
  }, [factoryId, runId, onSessionEnded, reload])

  // A remembered conversation from another run, or one that no longer exists, falls back to new.
  // Only a list requested after the selection can prove that the selection is gone.
  useEffect(() => {
    if (list && selection !== 'new' && list.loadedAt >= selectedAt.current
      && !list.cases.some(item => item.case_id === selection)) select('new')
  }, [list, selection, select])

  return { conversations: list?.cases ?? null, selection, select, error }
}
