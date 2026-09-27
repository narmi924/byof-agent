import { useState } from 'react'
import { Button } from './components/ui/button'
import { errorMessage, submitAssistant } from './api'
import type { Priority } from './assistantContracts'
import { priorityNames } from './assistantModel'

type Choice = { kind: 'preference'; priority: Priority } | { kind: 'reset' }

/** Recommendation preference only reorders checked proposals; it never relaxes constraints. */
export function PreferenceSettings({ factoryId, runId }: { factoryId: string; runId: string | undefined }) {
  const [choice, setChoice] = useState<Choice | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const label = (item: Choice) => item.kind === 'reset' ? 'Restore the default ranking' : `Rank ${priorityNames[item.priority].toLowerCase()} first`
  async function confirm() {
    if (!choice || !runId || busy) return
    setBusy(true); setMessage('')
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15_000)
    try {
      await submitAssistant(factoryId, { request_id: crypto.randomUUID(), run_id: runId, kind: choice.kind, payload: choice.kind === 'reset' ? {} : { priority: choice.priority } }, controller.signal)
      setMessage(`Saved: ${label(choice)}.`); setChoice(null)
    } catch (reason) {
      setMessage(controller.signal.aborted ? 'Result pending; check back here shortly.' : errorMessage(reason, 'Could not save; try again.'))
    } finally { window.clearTimeout(timeout); setBusy(false) }
  }
  return <section className="settings-section" aria-labelledby="preference-title">
    <h3 id="preference-title">Recommendation preference</h3>
    <div className="settings-options">
      {(Object.keys(priorityNames) as Priority[]).map(priority => <Button key={priority} variant="outline" disabled={busy || !runId}
        aria-pressed={choice?.kind === 'preference' && choice.priority === priority} onClick={() => setChoice({ kind: 'preference', priority })}>{priorityNames[priority]}</Button>)}
      <Button variant="ghost" disabled={busy || !runId} aria-pressed={choice?.kind === 'reset'} onClick={() => setChoice({ kind: 'reset' })}>Restore default</Button>
    </div>
    {choice ? <div className="settings-confirm"><span>{label(choice)}</span><Button disabled={busy} onClick={() => { void confirm() }}>Confirm</Button><Button variant="ghost" disabled={busy} onClick={() => setChoice(null)}>Cancel</Button></div> : null}
    {message ? <p role="status">{message}</p> : null}
  </section>
}
