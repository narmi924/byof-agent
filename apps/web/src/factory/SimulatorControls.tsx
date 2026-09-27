import { useState } from 'react'
import { Button } from '../components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogTitle, DialogTrigger } from '../components/ui/dialog'
import { Input } from '../components/ui/input'
import type { SimulatorStatus } from '../contracts'
import { integer } from './formValue'
import type { FactoryCommand } from './editorTypes'

const speeds = [
  { value: 60000, label: '1 min / 60 s' },
  { value: 30000, label: '1 min / 30 s' },
  { value: 15000, label: '1 min / 15 s' },
  { value: 5000, label: '1 min / 5 s' },
]

/** Run controls live in one persistent bar, apart from editing factory records. */
export function SimulatorControls({ status, disabled, isReplay, hasPlan, actualDay, onStartToday, onReplay, command, onError }: {
  status: SimulatorStatus | null
  disabled: boolean
  isReplay: boolean
  hasPlan: boolean
  actualDay: string
  onStartToday: () => void
  onReplay: () => void
  command: FactoryCommand
  onError: (message: string) => void
}) {
  const [speed, setSpeed] = useState<number | null>(null)
  const interval = speed ?? status?.interval_ms ?? 60000
  const running = status?.mode === 'RUNNING'
  const replayStopped = status?.replay?.done === true || Boolean(status?.replay?.error_code)
  const locked = disabled || !status
  // Before the day's first plan is approved the clock stays still; approval starts it.
  const waitingForPlan = !isReplay && !hasPlan && !running
  return <section className="sim-controls" aria-label="Run controls">
    <span className={`sim-state${running ? ' is-running' : ''}`}>{!status ? 'Reading run status' : running ? 'Running' : 'Paused'}{isReplay ? ' · Replay' : ''}</span>
    {running
      ? <Button variant="outline" disabled={locked} onClick={() => command('clock.pause', {}, 'Pause')}>Pause</Button>
      : <Button disabled={locked || replayStopped || waitingForPlan} onClick={() => command('clock.run', { interval_ms: interval }, 'Run')}>Run</Button>}
    <label className="sim-field">Speed<select aria-label="Run speed" value={interval} disabled={locked || replayStopped} onChange={event => {
      const next = Number(event.target.value)
      setSpeed(next)
      if (running) command('clock.run', { interval_ms: next }, 'Change run speed')
    }}>{speeds.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
    <form className="sim-step" aria-label="Advance step by step" onSubmit={event => {
      event.preventDefault()
      if (locked || running || replayStopped || waitingForPlan) return
      try {
        const minutes = integer(new FormData(event.currentTarget), 'minutes', 1, 60)
        command('clock.step', { minutes }, isReplay ? 'Advance replay records' : 'Advance time')
      } catch (reason) { onError(reason instanceof Error ? reason.message : 'Check the minutes to advance.') }
    }}>
      <Button type="submit" variant="outline" disabled={locked || running || replayStopped || waitingForPlan}>Advance</Button>
      <label className="sim-field"><Input name="minutes" aria-label={isReplay ? 'Records to advance' : 'Minutes to advance'} type="number" min="1" max="60" step="1" defaultValue="15" required disabled={locked || running || replayStopped || waitingForPlan} />{isReplay ? 'records' : 'min'}</label>
    </form>
    {waitingForPlan && status ? <span className="sim-hint">Starts automatically once the manager approves today's plan</span> : null}
    <span className="topbar-spacer" />
    {!isReplay ? <Dialog>
      <DialogTrigger asChild><Button variant="ghost" disabled={locked}>Reset today</Button></DialogTrigger>
      <DialogContent aria-describedby={undefined}>
        <DialogTitle>Reset to {actualDay} before the shift starts</DialogTitle>
        <p>Orders, inventory, machines and staff return to today's 08:30 pre-shift state; the clock is paused and there is no plan. Earlier runs are kept; the manager starts a new conversation.</p>
        <div className="dialog-actions">
          <DialogClose asChild><Button variant="ghost">Cancel</Button></DialogClose>
          <DialogClose asChild><Button disabled={locked} onClick={onStartToday}>Confirm reset</Button></DialogClose>
        </div>
      </DialogContent>
    </Dialog> : null}
    {!isReplay && status ? <Dialog>
      <DialogTrigger asChild><Button variant="ghost" disabled={locked}>More</Button></DialogTrigger>
      <DialogContent aria-describedby={undefined}>
        <DialogTitle>More run options</DialogTitle>
        <div className="sim-more">
          <div>
            <p><b>Random breakdowns</b>: {status.scenario?.enabled ? 'on. ' : ''}While running, one random machine stops every 90 minutes.</p>
            <DialogClose asChild><Button variant="outline" aria-pressed={Boolean(status.scenario?.enabled)} disabled={locked}
              onClick={() => command('scenario.configure', { enabled: !status.scenario?.enabled, seed: 17, every_minutes: 90 }, status.scenario?.enabled ? 'Turn off random breakdowns' : 'Turn on random breakdowns')}>
              {status.scenario?.enabled ? 'Turn off random breakdowns' : 'Turn on random breakdowns'}
            </Button></DialogClose>
          </div>
          <div>
            <p><b>Replay this run</b>: replays the records while keeping the original run; a replay can only be viewed and advanced.</p>
            <DialogClose asChild><Button variant="outline" disabled={locked} onClick={onReplay}>Replay this run</Button></DialogClose>
          </div>
        </div>
        <div className="dialog-actions"><DialogClose asChild><Button variant="ghost">Close</Button></DialogClose></div>
      </DialogContent>
    </Dialog> : null}
  </section>
}
