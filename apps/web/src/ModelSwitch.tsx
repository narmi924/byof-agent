import { Button } from './components/ui/button'
import { useEffect, useRef, useState } from 'react'
import { changeModelSelection, readModelSelection } from './api'
import type { ModelSelection } from './modelContracts'

export function ModelSwitch() {
  const [selection, setSelection] = useState<ModelSelection | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const active = useRef<AbortController | null>(null)
  const writing = useRef(false)
  useEffect(() => {
    async function refresh() {
      if (writing.current) return
      active.current?.abort()
      const controller = new AbortController()
      active.current = controller
      try {
        const value = await readModelSelection(AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))
        if (!controller.signal.aborted) { setSelection(value); setError('') }
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Could not read the model selection.')
      }
    }
    void refresh()
    const focus = () => { void refresh() }
    window.addEventListener('focus', focus)
    return () => { active.current?.abort(); window.removeEventListener('focus', focus) }
  }, [attempt])
  async function change(modelId: string) {
    if (!selection || writing.current) return
    writing.current = true
    setBusy(true)
    setError('')
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    try {
      const value = await changeModelSelection(modelId, selection.version, crypto.randomUUID(), AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))
      if (!controller.signal.aborted) setSelection(value)
    } catch (reason) {
      if (!controller.signal.aborted) {
        // Reconcile uncertain writes and cross-window conflicts with server truth.
        try { setSelection(await readModelSelection(AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))) } catch { setSelection(null) }
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'The switch was not confirmed; try again.')
      }
    } finally {
      writing.current = false
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  return <div className="model-switch">
    <label>Agent model <select aria-label="Agent model" value={selection?.selected_model_id ?? ''} disabled={!selection || busy} onChange={event => void change(event.target.value)}>
      {!selection && <option value="">Loading</option>}
      {selection && !selection.models.some(model => model.model_id === selection.selected_model_id) && <option value={selection.selected_model_id}>Previous model retired</option>}
      {selection?.models.map(model => <option key={model.model_id} value={model.model_id} disabled={!model.available}>{model.label}{model.available ? '' : ' (not configured)'}</option>)}
    </select></label>
    <small>All conversations of this account · from the next turn</small>
    {error && <span role="alert">{error} <Button variant="outline" type="button" disabled={busy} onClick={() => setAttempt(value => value + 1)}>Refresh</Button></span>}
  </div>
}
