import { dispatchCsv, downloadText } from './dispatchSheet'
import { SlidersHorizontal, X } from 'lucide-react'
import { Button } from './components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogTitle, DialogTrigger } from './components/ui/dialog'
/** Daily schedule board: one screen per day.
 *  The thick upper bar is the plan, the thin lower bar the actual execution segments from the factory; gaps in the thin bar are real interruptions.
 *  Shift shading comes from machine and staff calendars; gaps between windows are breaks and non-working time. */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { Assignment, ProductionBatch, Workspace } from './contracts'
import type { CaseRecord, HumanTask } from './caseContracts'
import type { Grouping, Lane, OperationRow, RunBar } from './scheduleModel'
import {
  activeCandidateId, buildBoard, clip, mergeRuns, operationIndex, orderProjections,
  packRows, rowEndMs, rowStartMs,
} from './scheduleModel'
import { legendOrder, operationStates, shiftBands } from './palette'
import type { OperationState } from './palette'
import {
  clockOf, dayBounds, dayKeyOf, dayKeysIn, dayLabel, parseMs, safeZone, weekdayLabel, zoneOffsetLabel,
} from './dayWindow'
import { dateTime } from './presentation'
import type { ViewState } from './viewState'
import { Badge, Drawer, EmptyState, KeyValues, MessageStrip, SummaryChip } from './ui'

const HOUR = 3_600_000
const LANE_HEAD = 170
const MIN_TRACK = 480
const FALLBACK_TRACK = 900
/** Minimum block width that fits text; narrower blocks keep only color and the accessible name. */
const LABEL_WIDTH = 46
/** Changeover segments narrower than this are shown as part of the production segment to avoid unreadable slivers. */
const SETUP_WIDTH = 6
/** When a lane has so many operations that the average width drops below this, similar operations merge by default:
 *  blocks narrower than this cannot fit text, and drawing every batch only produces dense unreadable stripes. */
const CROWDED_WIDTH = 40
const zoomLevels = [1, 2, 4] as const
const groupLabels: Record<Grouping, string> = { resource: 'By machine', worker: 'By worker', order: 'By order' }

interface PlanChoice { id: string; label: string; assignments: Assignment[]; effective: boolean; note: string; caseId: string | null }

export function BoardView({ workspace, state, navigate, tasks, conversations, onOpenConversation }: {
  workspace: Workspace | null
  state: ViewState
  navigate: (patch: Partial<ViewState>, push?: boolean) => void
  tasks: HumanTask[]
  conversations?: Pick<CaseRecord, 'case_id' | 'title'>[] | null
  onOpenConversation?: (caseId: string, candidateId: string) => void
}) {
  const snapshot = workspace?.snapshot ?? null
  const zone = safeZone(snapshot?.profile.timezone)
  const clockMs = parseMs(snapshot?.snapshot_clock)
  const [fullDay, setFullDay] = useState(false)
  const [slide, setSlide] = useState('')
  const [zoom, setZoom] = useState<number>(1)
  /** null means merging is decided by density; fixed once the user chooses explicitly. */
  const [mergeChoice, setMergeChoice] = useState<boolean | null>(null)
  const [openRun, setOpenRun] = useState<RunBar | null>(null)
  const [wallNow, setWallNow] = useState(() => Date.now())
  useEffect(() => { const timer = window.setInterval(() => setWallNow(Date.now()), 30_000); return () => window.clearInterval(timer) }, [])

  const days = useMemo(() => {
    if (!snapshot) return []
    const start = parseMs(snapshot.horizon?.start_at) || clockMs
    const end = parseMs(snapshot.horizon?.end_at) || clockMs
    const keys = dayKeysIn(Math.min(start, clockMs), Math.max(end, clockMs), zone)
    return keys.length ? keys : [dayKeyOf(clockMs, zone)]
  }, [snapshot, clockMs, zone])

  const today = Number.isFinite(clockMs) ? dayKeyOf(clockMs, zone) : ''
  const actualDay = dayKeyOf(workspace?.server_time ? Date.parse(workspace.server_time) : wallNow, zone)
  const day = state.day && days.includes(state.day) ? state.day : days.includes(today) ? today : days[0] ?? ''
  const dayIndex = days.indexOf(day)

  const choices = useMemo<PlanChoice[]>(() => {
    if (!workspace) return []
    const activeId = activeCandidateId(workspace.publications)
    const list: PlanChoice[] = []
    const current = workspace.candidates.filter(record => !record.run_id || record.run_id === snapshot?.run_id)
    for (const record of current) {
      if (!record.candidate.has_solution) continue
      const effective = record.candidate.candidate_id === activeId
      // A preview is named by the conversation that produced it, so several previews stay distinguishable.
      const title = conversations?.find(item => item.case_id === record.case_id)?.title.replace(/^Check against the latest factory facts:\s*/, '')
      list.push({
        id: record.candidate.candidate_id,
        label: effective ? 'Active plan' : title ? (title.length > 40 ? `${title.slice(0, 40)}…` : title) : `Plan ${current.indexOf(record) + 1}`,
        assignments: record.candidate.assignments,
        effective,
        note: effective ? 'Accepted by factory' : (workspace.publications ?? []).some(item => item.candidate_id === record.candidate.candidate_id) ? 'Released before, replaced by a newer plan' : record.state === 'APPROVED' ? 'Approved, not yet confirmed effective' : 'Plan, not approved yet',
        caseId: record.case_id ?? null,
      })
    }
    return list.sort((left, right) => Number(right.effective) - Number(left.effective))
  }, [workspace, snapshot?.run_id, conversations])

  const chosen = choices.find((item) => item.id === state.candidateId) ?? choices[0] ?? null
  const effective = choices.find((item) => item.effective) ?? null
  const baseline = chosen && !chosen.effective && effective ? effective.assignments : null

  const bounds = day ? dayBounds(day, zone) : { key: '', startMs: Number.NaN, endMs: Number.NaN }
  const board = useMemo(() => buildBoard({
    snapshot,
    assignments: chosen?.assignments ?? [],
    baseline,
    dayStartMs: bounds.startMs,
    dayEndMs: bounds.endMs,
    grouping: state.group,
  }), [snapshot, chosen, baseline, bounds.startMs, bounds.endMs, state.group])

  const index = useMemo(() => operationIndex(snapshot), [snapshot])
  const risk = useMemo(() => orderProjections(snapshot?.orders ?? [], chosen?.assignments ?? [], index)
    .filter((item) => item.late), [snapshot, chosen, index])

  // Display window: by default shrunk to the envelope of the day's calendars and work to avoid large blanks; can expand to the full day.
  const axis = useMemo(() => {
    if (!Number.isFinite(bounds.startMs)) return { startMs: Number.NaN, endMs: Number.NaN }
    if (fullDay) return { startMs: bounds.startMs, endMs: bounds.endMs }
    let min = Number.POSITIVE_INFINITY
    let max = Number.NEGATIVE_INFINITY
    for (const lane of board.lanes) {
      for (const band of lane.bands) {
        if (band.kind === 'NORMAL' || band.kind === 'OVERTIME') { min = Math.min(min, band.startMs); max = Math.max(max, band.endMs) }
      }
      for (const row of lane.rows) {
        for (const span of [...clip(row.planSpans, bounds.startMs, bounds.endMs), ...clip(row.actualSpans, bounds.startMs, bounds.endMs)]) {
          min = Math.min(min, span.startMs); max = Math.max(max, span.endMs)
        }
      }
    }
    if (clockMs >= bounds.startMs && clockMs < bounds.endMs) { min = Math.min(min, clockMs); max = Math.max(max, clockMs + HOUR) }
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return { startMs: bounds.startMs, endMs: bounds.endMs }
    return {
      startMs: Math.max(bounds.startMs, Math.floor(min / HOUR) * HOUR),
      endMs: Math.min(bounds.endMs, Math.ceil(max / HOUR) * HOUR),
    }
  }, [board, bounds.startMs, bounds.endMs, clockMs, fullDay])

  const span = axis.endMs - axis.startMs
  const percent = (ms: number) => ((Math.min(Math.max(ms, axis.startMs), axis.endMs) - axis.startMs) / span) * 100
  const place = (startMs: number, endMs: number) => ({
    left: `${percent(startMs)}%`,
    width: `${Math.max(percent(endMs) - percent(startMs), 0.25)}%`,
  })

  // Measure the real width of the time axis to decide whether blocks fit text and whether similar operations should merge.
  const scroll = useRef<HTMLDivElement | null>(null)
  const [available, setAvailable] = useState(FALLBACK_TRACK)
  useEffect(() => {
    const element = scroll.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      setAvailable(Math.max(element.clientWidth - LANE_HEAD, MIN_TRACK))
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const trackWidth = Math.max(available * zoom, MIN_TRACK)
  const pxPerMs = trackWidth / span
  const widthOf = (startMs: number, endMs: number) => Math.max(endMs - startMs, 0) * pxPerMs

  function goto(next: string, direction: 'next' | 'prev') {
    if (!next || next === day) return
    setSlide(direction === 'next' ? 'slide-next' : 'slide-prev')
    navigate({ day: next, operationId: '' })
  }
  useEffect(() => {
    if (!slide) return
    const timer = window.setTimeout(() => setSlide(''), 200)
    return () => window.clearTimeout(timer)
  }, [slide])

  const pager = useRef<HTMLDivElement | null>(null)
  const drag = useRef<{ x: number; id: number } | null>(null)

  const selected = state.operationId ? board.rows.find((row) => row.operationId === state.operationId) ?? null : null

  const freezeMinutes = snapshot?.profile.policy?.freeze_window_min ?? 0
  const legend = legendOrder.filter((item) => board.summary.byState[item] > 0)

  // The densest lane decides the default merge: merge when batches do not fit, otherwise show each batch.
  const busiest = Math.max(0, ...board.lanes.map((lane) => lane.rows.length))
  const crowded = busiest > 0 && trackWidth / busiest < CROWDED_WIDTH
  const merged = mergeChoice ?? crowded

  if (!snapshot) {
    return <>
      <Header day={day} today={today} actualDay={actualDay} zone={zone} clockMs={clockMs} days={days} dayIndex={dayIndex} goto={goto} disabled />
      <section className="card">
        <EmptyState title="Factory facts not synced yet">
          <p>Reading the shop floor from the factory interface; it appears automatically once connected.</p>
        </EmptyState>
      </section>
    </>
  }

  return <>
    <Header day={day} today={today} actualDay={actualDay} zone={zone} clockMs={clockMs} days={days} dayIndex={dayIndex} goto={goto} />

    <div className="board-toolbar">
      {choices.length ? <label className="select-label">
        <span>View plan</span>
        <select aria-label="Plan shown" value={chosen?.id ?? ''} onChange={(event) => navigate({ candidateId: event.target.value, operationId: '' })}>
          {choices.map((choice) => <option key={choice.id} value={choice.id}>{choice.label} · {choice.note}</option>)}
        </select>
      </label> : null}
      {chosen && !chosen.effective ? <Badge tone="info">Preview{effective ? ' · changed operations marked' : ''}</Badge> : null}
      {chosen?.caseId && onOpenConversation ? <Button variant="outline" type="button" onClick={() => onOpenConversation(chosen.caseId!, chosen.id)}>Back to its conversation</Button> : null}
      <span className="spacer" />
      <div className="summary-line">
        <SummaryChip label="Today's work" value={board.summary.total} unit={`${board.summary.byState.DONE} done`} />
        <SummaryChip label="In progress" value={board.summary.byState.RUNNING + board.summary.byState.SETUP} unit="ops" tone="positive" />
        {board.summary.byState.BLOCKED + board.summary.byState.UNCONFIRMED
          ? <SummaryChip label="Interrupted" value={board.summary.byState.BLOCKED + board.summary.byState.UNCONFIRMED}
            unit={board.summary.byState.UNCONFIRMED ? `${board.summary.byState.UNCONFIRMED} awaiting shop floor confirmation` : 'ops'} tone="negative"
            onClick={() => navigate({ view: 'chat' }, true)} />
          : null}
        <SummaryChip label="Due date risk" value={risk.length} unit="orders" tone={risk.length ? 'negative' : 'neutral'} />
      </div>
      {chosen && Number.isFinite(bounds.startMs) ? <Button variant="outline" type="button" onClick={() => downloadText(dispatchCsv(chosen.assignments, index, bounds.startMs, bounds.endMs, zone), `dispatch-sheet-${day}${chosen.effective ? '' : '-preview'}.csv`)}>Export dispatch sheet</Button> : null}
      <Dialog>
        <DialogTrigger asChild><Button variant="outline" type="button" className="secondary"><SlidersHorizontal size={14} aria-hidden="true" />Display settings</Button></DialogTrigger>
        <DialogContent aria-describedby={undefined} className="board-display-dialog">
          <div className="section-heading"><DialogTitle>Display settings</DialogTitle><DialogClose asChild><Button variant="ghost" aria-label="Close display settings"><X size={16} /></Button></DialogClose></div>
          <div className="board-display-content">
          <Button variant="outline" type="button" className="secondary" aria-pressed={merged} onClick={() => setMergeChoice(!merged)}>
            {merged ? 'Show each batch' : 'Merge similar operations'}
          </Button>
          <div className="segmented" role="group" aria-label="Time axis width">
            {zoomLevels.map((level) => <Button variant="outline" key={level} type="button" aria-pressed={zoom === level} onClick={() => setZoom(level)}>
              {level === 1 ? 'Fit width' : `${level}×`}
            </Button>)}
          </div>
          <Button variant="outline" type="button" className="secondary" aria-pressed={fullDay} onClick={() => setFullDay((value) => !value)}>
            {fullDay ? 'Shift hours only' : 'Show full day'}
          </Button>
          <h4>Legend</h4>
          <ul className="legend-list">
            {legendOrder.map((item) => <li key={item} className="legend-item">
              <span className={`legend-swatch ${operationStates[item].modifier}`} aria-hidden="true" />
              <b>{operationStates[item].label}</b>
              <span className="muted">{operationStates[item].basis}</span>
            </li>)}
            {(['NORMAL', 'OVERTIME', 'CLOSED', 'UNAVAILABLE'] as const).map((item) => <li key={item} className="legend-item">
              <span className={`legend-swatch ${shiftBands[item].modifier}`} aria-hidden="true" />
              <b>{shiftBands[item].label}</b>
              <span className="muted">{shiftBands[item].basis}</span>
            </li>)}
          </ul>
          <p className="field-hint">The thick upper bar is the plan, the thin lower bar the actual execution segments from the factory; gaps in the thin bar are real interruptions.</p>
          </div>
        </DialogContent>
      </Dialog>
    </div>

    <div className={`board-page ${slide}`}>
      {!choices.length ? <MessageStrip tone="neutral">
        <p>No production scheduled.</p>
        <Button variant="outline" type="button" className="secondary" onClick={() => navigate({ view: 'chat' }, true)}>Open the Agent conversation</Button>
      </MessageStrip> : null}

      {board.lanesWithoutCalendar ? <MessageStrip tone="neutral">
        <p>{board.lanesWithoutCalendar} lane(s) have no shift calendar from the source; these rows show no shift shading.</p>
      </MessageStrip> : null}

      <div className="board">
        <div className="board-scroll" ref={(node) => { pager.current = node; scroll.current = node }}
          onPointerDown={(event) => {
            // Swipes are only recognized for touch or pen, and only when the time axis needs no horizontal scroll, so scrolling is never taken over.
            const surface = pager.current
            if (event.pointerType === 'mouse' || !surface || surface.scrollWidth > surface.clientWidth) return
            drag.current = { x: event.clientX, id: event.pointerId }
          }}
          onPointerUp={(event) => {
            const started = drag.current
            drag.current = null
            if (!started || started.id !== event.pointerId) return
            const moved = event.clientX - started.x
            if (moved <= -60 && dayIndex + 1 < days.length) goto(days[dayIndex + 1]!, 'next')
            if (moved >= 60 && dayIndex > 0) goto(days[dayIndex - 1]!, 'prev')
          }}
        >
          <div className="board-grid" role="group"
            aria-label={`Schedule for ${dayLabel(day)}; use the left and right arrow keys to change day`}
            style={zoom > 1 ? { minWidth: LANE_HEAD + trackWidth } : undefined}
            tabIndex={0}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight' && dayIndex + 1 < days.length) { event.preventDefault(); goto(days[dayIndex + 1]!, 'next') }
              if (event.key === 'ArrowLeft' && dayIndex > 0) { event.preventDefault(); goto(days[dayIndex - 1]!, 'prev') }
            }}
          >
            <div className="board-row board-ruler">
              <div className="lane-head">
                <select className="lane-group" aria-label="Group by" value={state.group} onChange={(event) => navigate({ group: event.target.value as Grouping })}>
                  {(['resource', 'worker', 'order'] as Grouping[]).map((group) => <option key={group} value={group}>{groupLabels[group]}</option>)}
                </select>
                <span className="muted">{clockOf(axis.startMs, zone)} – {clockOf(axis.endMs, zone)}</span>
              </div>
              <div className="ruler-track">
                {ticks(axis.startMs, axis.endMs).map((ms) => <span key={ms} className="ruler-tick" style={{ left: `${percent(ms)}%` }}>
                  <span>{clockOf(ms, zone)}</span>
                </span>)}
              </div>
            </div>

            {board.lanes.map((lane) => <LaneRow
              key={lane.id}
              lane={lane}
              zone={zone}
              clockMs={clockMs}
              freezeMinutes={freezeMinutes}
              dayStartMs={bounds.startMs}
              dayEndMs={bounds.endMs}
              place={place}
              percent={percent}
              widthOf={widthOf}
              merged={merged}
              selectedId={state.operationId}
              onSelect={(operationId) => navigate({ operationId })}
              onSelectRun={setOpenRun}
            />)}

            {board.lanes.length === 0 ? <div className="board-row">
              <div className="lane-head"><b>—</b></div>
              <div className="lane-track is-empty" />
            </div> : null}
          </div>
        </div>

        <div className="board-legend">
          {(legend.length ? legend : (['PLANNED', 'RUNNING', 'DONE', 'BLOCKED'] as OperationState[])).map((item) => <span key={item} className="legend-item">
            <span className={`legend-swatch ${operationStates[item].modifier}`} aria-hidden="true" />
            {operationStates[item].label}
          </span>)}
          <span className="legend-item"><span className="legend-swatch band-normal" aria-hidden="true" />Regular shift</span>
          <span className="legend-item"><span className="legend-swatch band-overtime" aria-hidden="true" />Overtime window</span>
          <span className="legend-item"><span className="legend-swatch band-closed" aria-hidden="true" />Non-working time</span>
        </div>
      </div>
    </div>

    {selected ? <OperationDrawer
      row={selected}
      batchPurpose={snapshot?.production_batches?.find(batch => batch.batch_id === selected.identity?.batchId)?.purpose}
      zone={zone}
      clockMs={clockMs}
      tasks={tasks}
      onClose={() => navigate({ operationId: '' })}
      onDiscuss={() => navigate({ view: 'chat' }, true)}
    /> : null}

    {openRun && !selected ? <RunDrawer
      run={openRun}
      rows={board.rows}
      zone={zone}
      onClose={() => setOpenRun(null)}
      onSelect={(operationId) => { setOpenRun(null); navigate({ operationId }) }}
    /> : null}
  </>
}

function ticks(startMs: number, endMs: number): number[] {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return []
  const total = (endMs - startMs) / HOUR
  const step = total > 16 ? 2 : 1
  const list: number[] = []
  for (let ms = Math.ceil(startMs / HOUR) * HOUR; ms < endMs; ms += step * HOUR) list.push(ms)
  return list
}

function Header({ day, today, actualDay, zone, clockMs, days, dayIndex, goto, disabled = false }: {
  day: string; today: string; actualDay: string; zone: string; clockMs: number
  days: string[]; dayIndex: number; goto: (next: string, direction: 'next' | 'prev') => void; disabled?: boolean
}) {
  return <>
    <div className="view-header board-view-header">
      <div className="board-title">
        <h1>{day === today ? 'Today · ' : ''}{dayLabel(day)} {weekdayLabel(day, zone)}</h1>
        {Number.isFinite(clockMs) ? <p className="muted">Factory time {clockOf(clockMs, zone)} · {zoneOffsetLabel(clockMs, zone)}{actualDay !== today ? ` · actual date ${actualDay}` : ''}</p> : null}
      </div>
      {days.length > 1 ? <div className="day-strip" role="group" aria-label="Days in the scheduling window">
        {days.map((key) => <Button variant="outline" key={key} type="button" className="day-chip" disabled={disabled}
          aria-current={key === day ? 'date' : undefined}
          onClick={() => goto(key, days.indexOf(key) > dayIndex ? 'next' : 'prev')}>
          <span>{key === today ? 'Today' : weekdayLabel(key, zone)}</span>
          <b>{dayLabel(key)}</b>
        </Button>)}
      </div> : null}
    </div>
  </>
}

function LaneRow({ lane, zone, clockMs, freezeMinutes, dayStartMs, dayEndMs, place, percent, widthOf, merged, selectedId, onSelect, onSelectRun }: {
  lane: Lane
  zone: string
  clockMs: number
  freezeMinutes: number
  dayStartMs: number
  dayEndMs: number
  place: (startMs: number, endMs: number) => { left: string; width: string }
  percent: (ms: number) => number
  widthOf: (startMs: number, endMs: number) => number
  merged: boolean
  selectedId: string
  onSelect: (operationId: string) => void
  onSelectRun: (run: RunBar) => void
}) {
  const rows = lane.rows
  const runs = merged ? mergeRuns(rows) : []
  const slots = merged ? runs.map(() => 0) : packRows(rows.map((row) => {
    const spans = [...clip(row.planSpans, dayStartMs, dayEndMs), ...clip(row.actualSpans, dayStartMs, dayEndMs)]
    return spans.length
      ? { startMs: Math.min(...spans.map((span) => span.startMs)), endMs: Math.max(...spans.map((span) => span.endMs)) }
      : { startMs: dayStartMs, endMs: dayStartMs }
  }))
  const subRows = Math.max(1, ...slots.map((slot) => slot + 1))
  const inDay = clockMs >= dayStartMs && clockMs < dayEndMs
  return <div className="board-row">
    <div className="lane-head">
      <b>{lane.title}</b>
      <span className="muted">{lane.subtitle}</span>
      {lane.statusValue !== 'AVAILABLE' && lane.statusValue !== 'CONFIRMED'
        ? <Badge tone={lane.statusValue === 'DOWN' || lane.statusValue === 'ABSENT' ? 'negative' : 'neutral'}>{lane.statusLabel}</Badge>
        : null}
    </div>
    <div className="lane-track" style={{ minHeight: subRows * 34 }}>
      {lane.bands.map((band, position) => <span key={`${band.kind}-${position}`}
        className={`band ${shiftBands[band.kind].modifier}`}
        style={place(band.startMs, band.endMs)}
        title={band.reason ?? shiftBands[band.kind].label}
      />)}
      {inDay && freezeMinutes > 0
        ? <span className="freeze-veil" style={place(clockMs, Math.min(clockMs + freezeMinutes * 60_000, dayEndMs))}
          title={`Freeze window ${freezeMinutes} min: no new actions are scheduled in this period`} />
        : null}
      {inDay ? <span className="now-line" style={{ left: `${percent(clockMs)}%` }} title={`Current factory time ${clockOf(clockMs, zone)}`} /> : null}

      {merged
        ? <div className="sub-row">
          {runs.map((run) => <RunBlock
            key={run.key}
            run={run}
            zone={zone}
            place={place}
            widthOf={widthOf}
            selected={run.operationIds.includes(selectedId)}
            onSelect={onSelectRun}
          />)}
        </div>
        : [...Array(subRows).keys()].map((subIndex) => <div key={subIndex} className="sub-row">
          {rows.map((row, position) => slots[position] === subIndex ? <Bars
            key={row.operationId}
            row={row}
            zone={zone}
            place={place}
            widthOf={widthOf}
            dayStartMs={dayStartMs}
            dayEndMs={dayEndMs}
            selected={row.operationId === selectedId}
            onSelect={onSelect}
          /> : null)}
        </div>)}
      {!rows.length ? <div className="sub-row" /> : null}
    </div>
  </div>
}

/** One merged bar: consecutive batches of the same order and operation. */
function RunBlock({ run, zone, place, widthOf, selected, onSelect }: {
  run: RunBar
  zone: string
  place: (startMs: number, endMs: number) => { left: string; width: string }
  widthOf: (startMs: number, endMs: number) => number
  selected: boolean
  onSelect: (run: RunBar) => void
}) {
  const style = operationStates[run.state]
  const label = run.count > 1
    ? `${run.orderId} · ${run.operationCode} · ${run.count} batches`
    : `${run.orderId} · ${run.operationCode}`
  const name = `${label} ${style.label}`
  const wide = widthOf(run.startMs, run.endMs) >= LABEL_WIDTH
  return <>
    <span
      className={`op-fill ${style.modifier}${run.changed ? ' is-changed' : ''}`}
      style={place(run.startMs, run.endMs)}
      aria-hidden="true"
    />
    <Button variant="outline" type="button"
      className={`op-hit ink-${style.ink}`}
      style={place(run.startMs, run.endMs)}
      aria-pressed={selected}
      aria-label={name}
      title={`${label}\n${style.label} (${style.basis})\n${clockOf(run.startMs, zone)} → ${clockOf(run.endMs, zone)}`}
      onClick={() => onSelect(run)}
    >{wide ? <span>{label}</span> : null}</Button>
  </>
}

function Bars({ row, zone, place, widthOf, dayStartMs, dayEndMs, selected, onSelect }: {
  row: OperationRow
  zone: string
  place: (startMs: number, endMs: number) => { left: string; width: string }
  widthOf: (startMs: number, endMs: number) => number
  dayStartMs: number
  dayEndMs: number
  selected: boolean
  onSelect: (operationId: string) => void
}) {
  const style = operationStates[row.state]
  const label = row.identity
    ? `${row.identity.batchId.split('-').slice(-1)[0]}·${row.identity.operationCode}`
    : row.operationId
  const title = [
    row.identity ? `${row.identity.batchId} ${row.identity.operationCode} ${row.identity.stepName}` : row.operationId,
    `${style.label} (${style.basis})`,
    row.assignment ? `Plan ${clockOf(parseMs(row.assignment.changeover_start), zone)} → ${clockOf(parseMs(row.assignment.end_at), zone)}` : 'Not in the current plan',
    row.actual ? `Actual start ${row.actual.actual_start ? clockOf(parseMs(row.actual.actual_start), zone) : 'not started'} · ${row.actual.completed_quantity} pcs done` : 'No factory execution record',
  ].join('\n')
  const clipped = clip(row.planSpans, dayStartMs, dayEndMs)
  const real = clip(row.actualSpans, dayStartMs, dayEndMs)
  const extent = [...clipped, ...real]
  if (!extent.length) return null
  const startMs = Math.min(...extent.map((part) => part.startMs))
  const endMs = Math.max(...extent.map((part) => part.endMs))
  // Changeover segments that are too narrow merge into the adjacent production segment to avoid unreadable slivers.
  const plan = clipped.filter((part, position) => part.phase !== 'setup'
    || widthOf(part.startMs, part.endMs) >= SETUP_WIDTH
    || clipped[position + 1] === undefined)
  const wide = widthOf(startMs, endMs) >= LABEL_WIDTH
  return <>
    {plan.map((part, position) => <span key={`plan-${position}`}
      className={`op-fill ${part.phase === 'setup' && (row.state === 'PLANNED' || row.state === 'REPLAN') ? 'is-setup' : style.modifier}${row.changed ? ' is-changed' : ''}`}
      style={place(position === 0 ? startMs : part.startMs, part.endMs)}
      aria-hidden="true"
    />)}
    {!plan.length ? <span
      className={`op-fill ${style.modifier}`}
      style={place(startMs, endMs)}
      aria-hidden="true"
    /> : null}
    {real.map((part, position) => <span key={`real-${position}`}
      className={`actual${row.actualApproximate ? ' is-approximate' : ''}${row.state === 'DONE' ? ' is-done' : ''}${row.state === 'BLOCKED' || row.state === 'UNCONFIRMED' ? ' is-blocked' : ''}`}
      style={place(part.startMs, part.endMs)}
      aria-hidden="true"
    />)}
    <Button variant="outline" type="button"
      className={`op-hit ink-${style.ink}`}
      style={place(startMs, endMs)}
      aria-pressed={selected}
      aria-label={`${label} ${style.label}`}
      title={title}
      onClick={() => onSelect(row.operationId)}
    >{wide ? <span>{label}</span> : null}</Button>
  </>
}

/** Detail of a merged bar: which batches it contains, with each operation one click away. */
function RunDrawer({ run, rows, zone, onClose, onSelect }: {
  run: RunBar
  rows: OperationRow[]
  zone: string
  onClose: () => void
  onSelect: (operationId: string) => void
}) {
  const style = operationStates[run.state]
  const members = run.operationIds
    .map((operationId) => rows.find((row) => row.operationId === operationId))
    .filter((row): row is OperationRow => row !== undefined)
  const first = members[0]
  return <Drawer label={`Consecutive operations ${run.orderId} ${run.operationCode}`} onClose={onClose} footer={
    <Button variant="outline" type="button" className="ghost" onClick={onClose}>Close</Button>
  }>
    <div className="drawer-header">
      <p className="muted">{run.orderId} · {run.operationCode}</p>
      <h2>{run.stepName || 'Operation name not confirmed'}</h2>
      <div className="summary-line">
        <Badge tone={style.tone}>{style.label}</Badge>
        <Badge tone="neutral">{run.count} consecutive batches</Badge>
      </div>
    </div>
    <div className="drawer-body">
      <KeyValues items={[
        { label: 'Interval', value: `${clockOf(run.startMs, zone)} → ${clockOf(run.endMs, zone)}` },
        { label: 'Machine / worker', value: first ? `${first.resourceId || 'unassigned'} · ${first.workerId || 'unassigned'}` : 'Unassigned' },
        { label: 'Product', value: first?.identity?.productName ?? 'Unidentified' },
        { label: 'Total quantity', value: first?.identity ? `${run.count * first.identity.quantity} pcs` : 'Unidentified' },
      ]} />
      <p className="field-hint">This bar is several consecutive batches of the same order and operation, merged only for readability; each batch is still its own operation record.</p>
      <div>
        <h4>Batches included</h4>
        <ul className="inbox-list">
          {members.slice(0, 40).map((row) => <li key={row.operationId} className="inbox-item">
            <div className="inbox-head">
              <Badge tone={operationStates[row.state].tone}>{operationStates[row.state].label}</Badge>
              <h3>{row.identity?.batchId ?? row.operationId}</h3>
            </div>
            <div className="inbox-meta">
              <span>{clockOf(rowStartMs(row), zone)} → {clockOf(rowEndMs(row), zone)}</span>
              {row.actual ? <span>{row.actual.completed_quantity} pcs done</span> : <span>No factory execution record</span>}
            </div>
            <div className="inbox-actions">
              <Button variant="outline" type="button" className="secondary" onClick={() => onSelect(row.operationId)}>View this operation</Button>
            </div>
          </li>)}
        </ul>
        {members.length > 40 ? <p className="field-hint">Only the first 40 of {members.length} batches are listed. Switch to "Show each batch" to see them all.</p> : null}
      </div>
    </div>
  </Drawer>
}

function OperationDrawer({ row, batchPurpose, zone, clockMs, tasks, onClose, onDiscuss }: {
  row: OperationRow
  batchPurpose: ProductionBatch['purpose'] | undefined
  zone: string
  clockMs: number
  tasks: HumanTask[]
  onClose: () => void
  onDiscuss: () => void
}) {
  const style = operationStates[row.state]
  const identity = row.identity
  const actual = row.actual
  const assignment = row.assignment
  const related = tasks.filter((task) => task.subject_id === row.operationId || task.subject_id === row.resourceId)
  const remaining = (value: number | null | undefined) => value == null ? 'Pending' : `${value} min`

  return <Drawer label={`Operation ${row.operationId}`} onClose={onClose} footer={<>
    <Button variant="outline" type="button" onClick={onDiscuss}>Ask the Agent</Button>
    <Button variant="outline" type="button" className="ghost" onClick={onClose}>Close</Button>
  </>}>
    <div className="drawer-header">
      <p className="muted">{row.operationId}</p>
      <h2>{identity ? `${identity.operationCode} ${identity.stepName}` : 'Operation name not confirmed'}</h2>
      <div className="summary-line">
        <Badge tone={style.tone}>{style.label}</Badge>
        {row.changed ? <Badge tone="info">Differs from the active plan</Badge> : null}
        {row.actualApproximate ? <Badge tone="neutral">Actual segments not provided</Badge> : null}
      </div>
    </div>

    <div className="drawer-body">
      <KeyValues items={[
        { label: 'Order / product', value: identity ? `${identity.orderId} · ${identity.productName}` : 'Unidentified' },
        { label: 'Batch / size', value: identity ? `${identity.batchId} · ${identity.quantity} pcs per batch` : 'Unidentified' },
        ...(batchPurpose ? [{ label: 'Purpose', value: { CUSTOMER: 'Customer delivery', STOCK: 'To stock', CANCELLED: 'Cancelled' }[batchPurpose] }] : []),
        { label: 'Machine / worker', value: `${row.resourceId || 'unassigned'} · ${row.workerId || 'unassigned'}` },
        {
          label: 'Planned changeover → end',
          value: assignment
            ? `${clockOf(parseMs(assignment.changeover_start), zone)} → ${clockOf(parseMs(assignment.end_at), zone)}`
            : 'Not in the current plan',
        },
        ...(assignment?.resume_at ? [{
          label: 'Resumption',
          value: `Preparation again from ${clockOf(parseMs(assignment.resume_changeover_start ?? assignment.resume_at), zone)}, production resumes ${clockOf(parseMs(assignment.resume_at), zone)}`,
        }] : []),
        { label: 'Actual start', value: actual?.actual_start ? clockOf(parseMs(actual.actual_start), zone) : 'Not started' },
        { label: 'Actual end', value: actual?.actual_end ? clockOf(parseMs(actual.actual_end), zone) : 'Not finished' },
        { label: 'Completed', value: identity ? `${actual?.completed_quantity ?? 0} / ${identity.quantity} pcs` : `${actual?.completed_quantity ?? 0} pcs` },
        { label: 'Remaining production / changeover', value: actual ? `${remaining(actual.remaining_minutes)} / ${remaining(actual.remaining_setup_minutes)}` : 'No execution record' },
        { label: 'Quality', value: actual ? { PASSED: 'Passed', FAILED: 'Failed', PENDING: 'Awaiting check', UNKNOWN: 'Not provided' }[actual.quality_state] ?? 'Not provided' : 'No execution record' },
      ]} />

      {batchPurpose === 'STOCK' ? <MessageStrip tone="info"><p>This batch kept producing after a demand change. Once all operations are done and it passes quality checks it counts as finished stock, not customer delivery.</p></MessageStrip> : null}

      {row.state === 'UNCONFIRMED' ? <MessageStrip tone="negative">
        <p>The remaining work has no confirmed source. Before rescheduling this operation, the shop floor must confirm the recovery time and remaining minutes.</p>
      </MessageStrip> : null}
      {row.state === 'REPLAN' ? <MessageStrip tone="critical">
        <p>The planned slot has passed without an execution record. The factory does not start missed dispatches, so it must be rescheduled.</p>
      </MessageStrip> : null}

      <div>
        <h4>Actual execution segments</h4>
        {actual?.segments?.length
          ? <ul className="execution-segments">
            {actual.segments.map((segment, position) => <li key={`${segment.source_event_id}-${position}`}>
              {segment.phase === 'SETUP' ? 'Changeover' : 'Production'} {clockOf(parseMs(segment.start_at), zone)} → {clockOf(parseMs(segment.end_at), zone)}
            </li>)}
            {actual.state === 'BLOCKED' ? <li className="muted">No segments after this (interrupted)</li> : null}
          </ul>
          : <p className="muted">{actual ? 'The source has not provided segment records; the actual interval above is approximate.' : 'No factory execution record.'}</p>}
      </div>

      {actual?.consumed?.length ? <div>
        <h4>Material consumed</h4>
        <ul className="execution-segments">
          {actual.consumed.map((item) => <li key={item.event_id}>{item.material_id} {item.quantity} {item.unit}</li>)}
        </ul>
      </div> : null}

      {related.length ? <div>
        <h4>Related tasks</h4>
        <ul className="inbox-list">
          {related.map((task) => <li key={task.task_id} className="inbox-item">
            <div className="inbox-head"><Badge tone={task.state === 'OPEN' || task.state === 'ESCALATED' ? 'critical' : 'neutral'}>{task.state === 'OPEN' ? 'Open' : task.state === 'ESCALATED' ? 'Escalated' : 'Closed'}</Badge></div>
            <p className="inbox-detail">{task.question}</p>
            <div className="inbox-meta"><span>Due {dateTime(task.due_at)} UTC</span></div>
          </li>)}
        </ul>
      </div> : null}

      <details className="detail-block">
        <summary>Full evidence and business boundaries</summary>
        <KeyValues items={[
          { label: 'Operation ID', value: row.operationId },
          { label: 'Route version', value: identity?.routeVersion ?? 'Not confirmed' },
          { label: 'Route position', value: identity ? `Operation ${identity.sequence} of this product` : 'Not confirmed' },
          { label: 'Current factory time', value: Number.isFinite(clockMs) ? dateTime(new Date(clockMs).toISOString(), zone) : 'Not confirmed' },
        ]} />
        <p className="field-hint">After approval the factory accepts and runs the plan; actual progress follows what the factory reports.</p>
      </details>
    </div>
  </Drawer>
}
