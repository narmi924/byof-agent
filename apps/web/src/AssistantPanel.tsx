import { ArrowUp, Square } from 'lucide-react'
import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { Textarea } from './components/ui/textarea'
import { Card } from './components/ui/card'
import { useCallback, useEffect, useEffectEvent, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { actOnHumanTask, ApiError, decideExecution, definitiveRejection, errorMessage, postCaseMessage, readAssistant, readCase, readCaseHistory, stopCaseTurn, submitAssistant } from './api'
import type { AssistantAttempt, AssistantRequest, AssistantState, Priority } from './assistantContracts'
import { actionableOptions } from './businessContracts'
import type { BusinessStudyJob } from './businessContracts'
import type { CaseDetail, HumanTask } from './caseContracts'
import type { CandidateRecord, Factory, Workspace } from './contracts'
import { usePendingSlot } from './pendingJournal'
import { Badge, MessageStrip } from './ui'
import { dateTime } from './presentation'
import { priorityNames, rankCandidates } from './assistantModel'
import { canPublishAutomatically } from './executionContracts'
import { progressReviewReason } from './revalidationContracts'
import { BusinessStudyCard } from './BusinessStudyCard'
import { PlanReview } from './PlanReview'
import { factoryLocalToUtc } from './dayWindow'
import { AssistantText } from './AssistantText'
import { useConversationDraft } from './useConversationDraft'
import { plainBusinessText } from './businessText'
import { metricNames, unitNames } from './copy'

export interface ChatRequest { key: string; message: string; suggestionId?: string }

interface Props {
  factory: Factory; userId: string | undefined; workspace: Workspace | null; tasks: HumanTask[]
  selection: string; onSelect: (caseId: string) => void
  request?: ChatRequest | null; onRequestHandled?: () => void
  onConversation?: (caseId: string | null) => void
  onSessionEnded: () => void; onChanged: () => Promise<void>; onPreview: (id: string) => void
  focusPlan?: string | null; onFocused?: () => void
}
type Operation = CaseDetail['operations'][number]
const terminalCases = ['RESOLVED', 'CANCELLED', 'HANDED_OFF']
const boundedCodes = ['UNCHANGED_INFEASIBLE', 'UNCHANGED_SEARCH', 'PROBLEM_SEARCH_LIMIT', 'SOLVER_BUDGET_EXHAUSTED', 'EXECUTION_IN_PROGRESS']
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const toolNames: Record<string, string> = { query: 'Checking shop floor facts', report_production: 'Checking delivery and materials', solve_scenario: 'Schedule calculation', evaluate_business_options: 'Comparing response options' }
const phaseText = {
  QUEUED: 'Message received; waiting for the Agent',
  ANALYZING: 'The Agent is analyzing',
  THINKING: 'The Agent is analyzing',
  READING_FACTS: 'Checking shop floor facts',
  SOLVING: 'Calculating plans',
  STOPPED: '',
  IDLE: '',
}
const startDay = "Schedule today's production from the latest shop floor facts and give me a feasible plan to approve."
const analyseNow = 'Check the current shop floor changes and production risks. If adjustments are needed, give me feasible plans and the decisions I need to make.'

export function AssistantPanel({ factory, userId, workspace, tasks, selection, onSelect, request, onRequestHandled, onSessionEnded, onChanged, onPreview, onConversation, focusPlan, onFocused }: Props) {
  const [data, setData] = useState<AssistantState | null>(null)
  const [loaded, setLoaded] = useState<CaseDetail | null>(null)
  const olderHistory = useRef<CaseDetail | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const detail = loaded && loaded.case_id === selection ? loaded : null
  const draftScope = `${userId}:${factory.factory_id}:${workspace?.snapshot?.run_id ?? 'loading'}`
  const [draft, setDraft] = useConversationDraft(`${draftScope}:${selection}`)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [busy, setBusy] = useState(false)
  const [outgoing, setOutgoing] = useState<{ message: string; caseId: string | null; requestId: string; accepted: boolean } | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [refreshKey, setRefreshKey] = useState(0)
  const journal = usePendingSlot('assistant', userId, factory.factory_id)
  const sending = useRef(false)
  const mounted = useRef(false)
  const writing = useRef<AbortController | null>(null)
  const feed = useRef<HTMLDivElement | null>(null)
  const composerInput = useRef<HTMLTextAreaElement | null>(null)
  const nearBottom = useRef(true)
  const lastFeedHeight = useRef(0)
  const lastOutgoing = useRef<string | null>(null)
  const selectionRef = useRef(selection)
  const fastPoll = useRef(false)
  const [readingEarlier, setReadingEarlier] = useState(false)
  const snapshot = workspace?.snapshot ?? null
  const zone = snapshot?.profile.timezone
  const runId = snapshot?.run_id
  const historical = Boolean(detail && detail.run_id !== runId)
  const canWrite = factory.roles.includes('planner') && Boolean(snapshot) && !historical && snapshot?.source.source_system !== 'factory-simulator-replay'
  const blocked = busy || journal.pending !== null || Boolean(journal.error) || !canWrite || (selection !== 'new' && !detail)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; writing.current?.abort() } }, [])
  useEffect(() => {
    const input = composerInput.current
    if (input) {
      input.style.height = 'auto'
      input.style.height = `${Math.min(input.scrollHeight, 200)}px`
    }
  }, [draft])

  // Switching conversations starts from a clean view; an accepted first message follows its new case.
  const [shownSelection, setShownSelection] = useState(selection)
  if (shownSelection !== selection) {
    setShownSelection(selection)
    setError(''); setReadingEarlier(false)
    setOutgoing(current => current && current.caseId === selection ? current : null)
  }
  useEffect(() => {
    selectionRef.current = selection
    olderHistory.current = null
    nearBottom.current = true; lastFeedHeight.current = 0
  }, [selection])

  useEffect(() => {
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | undefined
    const caseId = selection !== 'new' ? selection : undefined
    async function load() {
      controller = new AbortController()
      const signal = controller.signal
      const timeout = window.setTimeout(() => controller?.abort(), 10000)
      try {
        const [state, conversation] = await Promise.all([
          readAssistant(factory.factory_id, signal, caseId),
          caseId ? readCase(factory.factory_id, caseId, signal) : Promise.resolve(null),
        ])
        if (!stopped && !signal.aborted) {
          setData(state); setLoaded(conversation ? mergeHistory(conversation, olderHistory.current) : null); setReadError('')
          setOutgoing(current => current && conversation?.case_id === current.caseId
            && conversation.inputs.some(input => input.input_key === `user:${current.requestId}`) ? null : current)
        }
      } catch (reason) {
        if (!stopped) {
          setReadError(signal.aborted ? 'Connection timed out; reconnecting.' : errorMessage(reason, 'Connection interrupted; reconnecting.'))
          if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
        }
      } finally {
        window.clearTimeout(timeout)
        // Follow active work closely; idle conversations poll gently.
        if (!stopped) timer = window.setTimeout(() => { void load() }, fastPoll.current ? 1000 : 3000)
      }
    }
    void load()
    return () => { stopped = true; controller?.abort(); window.clearTimeout(timer) }
  }, [factory.factory_id, runId, onSessionEnded, refreshKey, selection])

  async function loadHistory() {
    if (!detail?.history_cursor || loadingHistory) return
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 10000)
    setLoadingHistory(true)
    try {
      const page = await readCaseHistory(factory.factory_id, detail.case_id, detail.history_cursor, controller.signal)
      if (!mounted.current) return
      olderHistory.current = mergeHistory(page, olderHistory.current, false)
      nearBottom.current = false
      setLoaded(current => current?.case_id === page.case_id ? mergeHistory(current, olderHistory.current) : current)
    } catch (reason) { if (mounted.current) setError(errorMessage(reason, 'Could not load the history; you can retry.')) }
    finally { window.clearTimeout(timeout); if (mounted.current) setLoadingHistory(false) }
  }

  useEffect(() => { onConversation?.(detail?.case_id ?? null) }, [detail?.case_id, onConversation])

  const operations = detail?.operations ?? []
  const actions = (data?.actions ?? []).filter(a => detail && a.run_id === detail.run_id
    && (a.result?.case_id === detail.case_id || (a.kind === 'recover' && a.payload.case_id === detail.case_id) || (a.kind === 'approve' && Array.isArray(detail.context.candidate_ids) && detail.context.candidate_ids.includes(a.payload.candidate_id))))
  const studies = detail ? (data?.business_studies ?? []).filter(job => job.case_id === detail.case_id
    && (!job.study || job.study.run_id === detail.run_id)).sort((a, b) => b.created_at.localeCompare(a.created_at)) : []
  const phase = detail?.activity?.phase ?? (detail?.state === 'PLANNING' ? 'SOLVING' : 'IDLE')
  const processing = busy || Boolean(outgoing) || !['IDLE', 'STOPPED'].includes(phase)
  useEffect(() => { fastPoll.current = processing }, [processing])
  useEffect(() => {
    if (!processing) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [processing])
  const contentVersion = `${detail?.case_id}:${detail?.version}:${detail?.inputs.length}:${operations.length}:${operations.at(-1)?.state}:${actions.map(a => `${a.state}:${a.result?.stage}`).join(',')}:${studies.map(j => `${j.job_id}:${j.state}:${j.study_hash}`).join(',')}:${outgoing?.requestId}`
  useEffect(() => {
    const el = feed.current
    if (!el) return
    if (outgoing && outgoing.requestId !== lastOutgoing.current) {
      // The manager's own message is always brought into view.
      nearBottom.current = true
      el.scrollTop = el.scrollHeight
    } else if (nearBottom.current) {
      // Long new results open at their beginning rather than skipping to the last card.
      el.scrollTop = lastFeedHeight.current ? Math.min(lastFeedHeight.current, el.scrollHeight - el.clientHeight) : el.scrollHeight
    }
    lastOutgoing.current = outgoing?.requestId ?? null
    lastFeedHeight.current = el.scrollHeight
  }, [contentVersion, outgoing])

  const send = useCallback(async (attempt: AssistantAttempt) => {
    if (sending.current || !canWrite) return
    if (!journal.save(attempt)) { if (attempt.kind === 'chat') setOutgoing(null); return }
    const recovering = journal.pending !== null
    const origin = selectionRef.current
    sending.current = true; setBusy(true); setError('')
    const controller = new AbortController()
    writing.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      if (attempt.kind === 'action') await submitAssistant(factory.factory_id, attempt.body, controller.signal)
      else if (attempt.kind === 'execution') await decideExecution(factory.factory_id, attempt.taskId!, attempt.body, controller.signal)
      else if (attempt.kind === 'response') await actOnHumanTask(factory.factory_id, attempt.taskId!, 'responses', attempt.body, controller.signal)
      else if (attempt.kind === 'stop') await stopCaseTurn(factory.factory_id, attempt.caseId!, attempt.body as { request_id: string; expected_target: string }, controller.signal)
      else {
        const result = await postCaseMessage(factory.factory_id, attempt.caseId, attempt.body, controller.signal)
        if (mounted.current && !controller.signal.aborted) {
          setOutgoing(current => current ? { ...current, caseId: result.case_id, accepted: true } : null)
          if (selectionRef.current === origin) onSelect(result.case_id)
        }
      }
      if (!mounted.current || controller.signal.aborted) return
      journal.clear(attempt)
      if (attempt.kind === 'chat') setDraft(current => current === attempt.body.message ? '' : current)
      setRefreshKey(v => v + 1)
      void onChanged()
    } catch (reason) {
      if (!mounted.current) return
      if (!controller.signal.aborted && definitiveRejection(reason, recovering)) {
        journal.clear(attempt)
        if (attempt.kind === 'chat') setOutgoing(null)
      }
      setError(controller.signal.aborted ? 'Result pending; check the original request. The system never creates the operation twice.' : errorMessage(reason, 'Result pending; check the original request.'))
      if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
    } finally {
      window.clearTimeout(timeout); sending.current = false
      if (mounted.current) setBusy(false)
    }
  }, [canWrite, factory.factory_id, journal, onChanged, onSelect, onSessionEnded, setDraft])

  function command(kind: AssistantRequest['kind'], payload: Record<string, unknown>) {
    if (blocked || !runId) return
    void send({ kind: 'action', body: { request_id: crypto.randomUUID(), run_id: runId, kind, payload }, caseId: null, taskId: null })
  }
  function chat(message: string, suggestionId?: string) {
    if (blocked || !message.trim()) return
    const caseId = !suggestionId && detail && !terminalCases.includes(detail.state) ? detail.case_id : null
    const requestId = crypto.randomUUID()
    setOutgoing({ message: message.trim(), caseId, requestId, accepted: false })
    void send({ kind: 'chat', body: { request_id: requestId, message: message.trim(), ...(caseId ? {} : { start_new: true }), ...(suggestionId ? { suggestion_id: suggestionId } : {}) }, caseId, taskId: null })
  }
  function stopAnalysis() {
    if (blocked || !detail?.activity?.stop_target) return
    void send({ kind: 'stop', body: { request_id: crypto.randomUUID(), expected_target: detail.activity.stop_target }, caseId: detail.case_id, taskId: null })
  }

  // A change picked from the top bar opens as the first message of a new conversation.
  const runRequest = useEffectEvent((item: ChatRequest) => {
    if (selection !== 'new' || blocked || !runId) return false
    chat(item.message, item.suggestionId)
    return true
  })
  useEffect(() => {
    if (!request) return
    const timer = window.setTimeout(() => { if (runRequest(request)) onRequestHandled?.() }, 0)
    return () => window.clearTimeout(timer)
  }, [request, onRequestHandled, selection, blocked, runId])

  const caseCandidates = new Set(Array.isArray(detail?.context.candidate_ids)
    ? detail.context.candidate_ids.filter((x): x is string => typeof x === 'string') : [])
  const recommendedId = [...operations].reverse().find(o => o.action === 'request_approval' && o.state === 'DONE' && o.result?.status !== 'REJECTED')?.parameters.candidate_id
  const publications = workspace?.publications ?? []
  const releaseCards = detail ? publications.filter(p => caseCandidates.has(p.candidate_id)) : []
  const candidates = rankCandidates((workspace?.candidates ?? []).filter(r =>
    (!r.run_id || r.run_id === runId) && r.candidate.has_solution && r.candidate.checker.status === 'PASS'
    && caseCandidates.has(r.candidate.candidate_id)
    && !publications.some(p => p.candidate_id === r.candidate.candidate_id && p.release.source_state !== 'REJECTED')
  ).sort((a, b) => Number(b.candidate.candidate_id === recommendedId) - Number(a.candidate.candidate_id === recommendedId)), data?.learning ?? null)
  // A finished comparison belongs to the Agent's next reply: explanation first, then the options.
  const attachedStudies = new Map<string, BusinessStudyJob>()
  for (const job of [...studies].reverse()) {
    if (['QUEUED', 'RUNNING'].includes(job.state)) continue
    const reply = operations.find(o => o.action === 'reply' && o.state === 'DONE' && o.created_at > job.created_at && !attachedStudies.has(o.operation_id))
    if (reply) attachedStudies.set(reply.operation_id, job)
  }
  const attachedJobs = new Set([...attachedStudies.values()].map(job => job.job_id))
  const timeline = [
    ...(detail?.inputs.filter(i => i.kind === 'USER').map(i => ({ id: i.input_id, at: i.created_at, type: 'user' as const, message: text(i.payload.message) })) ?? []),
    // A refused wait is an internal retry, not something the Agent said.
    ...operations.filter(o => !(o.action === 'wait' && o.result?.status === 'REJECTED')).filter(o => ['reply', 'report_production', 'handoff', 'wait', 'query', 'solve_scenario', 'evaluate_business_options'].includes(o.action) || (o.action === 'request_approval' && o.state === 'DONE' && o.result?.status !== 'REJECTED' && Boolean(o.reason_summary)) || (o.result?.status === 'WAITING' && o.result?.await_user === true) || (o.action === 'finish' && o.result?.status === 'RESOLVED')).map(o => ({ id: o.operation_id, at: o.created_at, type: 'agent' as const, operation: o })),
    ...actions.map(a => ({ id: a.action_id, at: detail?.inputs.find(i => i.input_key === `user:recovery:${a.action_id}`)?.created_at ?? a.created_at, type: 'action' as const, action: a })),
    ...studies.filter(job => !attachedJobs.has(job.job_id)).map(job => ({ id: job.job_id, at: job.created_at, type: 'study' as const, job })),
    // A plan the Agent asked the manager to approve follows the Agent's explanation.
    ...candidates.map(record => {
      const asked = operations.find(o => o.action === 'request_approval' && o.parameters.candidate_id === record.candidate.candidate_id)
      return { id: record.candidate.candidate_id, at: asked ? `${asked.created_at}~` : workspace?.jobs.find(job => job.candidate_id === record.candidate.candidate_id)?.created_at ?? detail?.created_at ?? '', type: 'candidate' as const, record }
    }),
    ...releaseCards.map(publication => ({ id: publication.release.release_id, at: publication.release.committed_at, type: 'publication' as const, publication })),
  ].sort((a, b) => a.at.localeCompare(b.at) || Number(a.type === 'action') - Number(b.type === 'action') || a.id.localeCompare(b.id))
    // Consecutive identical Agent notes (for example repeated waits) are shown once.
    .filter((item, index, all) => {
      if (item.type !== 'agent') return true
      const previous = all.slice(0, index).reverse().find(other => other.type === 'agent')
      const key = (o: typeof item.operation) => text(o.result?.summary) || o.reason_summary || ''
      return !(previous && previous.type === 'agent' && key(previous.operation) === key(item.operation) && !Object.hasOwn(toolNames, item.operation.action))
    })
  const informationTasks = tasks.filter(t => t.case_id === detail?.case_id && ['OPEN', 'ESCALATED'].includes(t.state)
    && (!t.task_type || t.task_type === 'INFORMATION') && factory.roles.includes(t.owner_role) && (t.owner_id === null || t.owner_id === userId))
  const latestReply = [...operations].reverse().find(o => o.state === 'DONE')
  // The option a manager approved from a comparison; the comparison is then decided.
  function approvedOption(job: BusinessStudyJob): string | undefined {
    const approval = actions.find(a => a.kind === 'treatment_execute' && !['CANCELLED', 'FAILED'].includes(a.state)
      && (a.payload.approval as { job_id?: unknown } | undefined)?.job_id === job.job_id)?.payload.approval as { option_id?: unknown } | undefined
    return typeof approval?.option_id === 'string' ? approval.option_id : undefined
  }
  const decisionOpen = candidates.length > 0 || studies.some(job => job.current && !approvedOption(job) && actionableOptions(job).length > 0)
  const choices = latestReply?.action === 'reply' && !historical && !processing && !terminalCases.includes(detail?.state ?? '') && !detail?.inputs.some(i => i.kind === 'USER' && Date.parse(i.created_at) >= Date.parse(latestReply.created_at))
    && Array.isArray(latestReply.result?.choices) ? latestReply.result.choices.filter((v): v is string => typeof v === 'string')
      // Approval only happens on a card; a chat button must not look like one.
      .filter(v => !decisionOpen || !/approv|execut|submit|agree/i.test(v)) : []
  const turnBudgetExhausted = detail?.error_code === 'MODEL_TURN_BUDGET_EXHAUSTED'
  const legacyBudgetExhausted = detail?.error_code === 'MODEL_BUDGET_EXHAUSTED'
  const caseBudgetExhausted = detail?.error_code === 'MODEL_CASE_BUDGET_EXHAUSTED'
  const gateway429 = detail?.error_code === 'MODEL_GATEWAY_429'
  const invalidAction = detail?.error_code === 'INVALID_MODEL_ACTION'
  const budgetExhausted = turnBudgetExhausted || legacyBudgetExhausted
  const startedAt = detail?.activity?.started_at ? Date.parse(detail.activity.started_at) : NaN
  const elapsed = Number.isFinite(startedAt) ? Math.max(0, Math.round((now - startedAt) / 1000)) : null
  const activity = busy ? 'Submitting…' : outgoing && phase === 'IDLE' ? outgoing.accepted ? 'Message received; waiting for the Agent' : 'Sending…' : phaseText[phase]

  function toolStatus(o: Operation): string {
    if (o.result?.status === 'REJECTED') return 'Not run'
    if (o.state !== 'DONE') return 'In progress'
    if (o.action === 'solve_scenario') {
      const job = workspace?.jobs.find(item => item.job_id === o.result?.job_id)
      if (!job || job.state === 'QUEUED') return 'Queued'
      if (job.state === 'RUNNING') return 'Calculating'
      if (job.state === 'FAILED') return 'Calculation not completed'
      const record = workspace?.candidates.find(item => item.candidate.candidate_id === job.candidate_id)
      if (!record) return 'Done'
      return record.candidate.has_solution && record.candidate.checker.status === 'PASS' ? 'Plan found'
        : record.candidate.native_status === 'INFEASIBLE' ? 'Cannot be scheduled under current conditions' : 'No feasible schedule found within the time limit'
    }
    if (o.action === 'evaluate_business_options') {
      const job = studies.find(item => item.job_id === o.result?.job_id)
      return !job ? 'Submitted' : ({ QUEUED: 'Queued', RUNNING: 'Comparing', SUCCEEDED: 'Comparison done', FAILED: 'Comparison not completed', CANCELLED: 'Cancelled' } as Record<string, string>)[job.state] ?? 'Done'
    }
    return 'Done'
  }

  // Opened from a plan on the timeline: bring that plan's card into view once it is rendered.
  useEffect(() => {
    if (!focusPlan || !detail) return
    const card = document.getElementById(`plan-${focusPlan}`)
    if (!card) return
    nearBottom.current = false
    card.scrollIntoView({ block: 'center' })
    onFocused?.()
  }, [focusPlan, detail, onFocused])

  function studyCard(job: BusinessStudyJob) {
    const approval = approvedOption(job)
    return snapshot ? <div className="chat-card" key={job.job_id} aria-label="Option comparison result"><BusinessStudyCard job={job} snapshot={snapshot} approvedOptionId={approval} disabled={blocked || Boolean(readError) || Boolean(detail && terminalCases.includes(detail.state))} onContinue={chat}
      onDiscuss={canWrite ? prefix => { setDraft(current => [current, prefix].filter(Boolean).join('\n')); composerInput.current?.focus() } : undefined}
      onExecute={factory.roles.includes('manager') ? (optionId, overtime, customer) => {
        if (!job.study_hash || !runId) return
        void send({ kind: 'action', body: { request_id: crypto.randomUUID(), run_id: runId, kind: 'treatment_execute', payload: { job_id: job.job_id, option_id: optionId, study_hash: job.study_hash, allow_overtime: overtime, accept_customer_change: customer } }, caseId: detail?.case_id ?? null, taskId: null })
      } : undefined} /></div> : null
  }

  return <section className="assistant-panel" aria-label="Production Agent">
    <div className="assistant-feed" ref={feed} onScroll={() => { const el = feed.current; if (el) { nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; setReadingEarlier(!nearBottom.current) } }}>
      <div className="chat-column">
        {detail?.history_cursor ? <Button variant="ghost" className="load-earlier" disabled={loadingHistory} onClick={() => { void loadHistory() }}>{loadingHistory ? 'Loading…' : 'Load earlier messages'}</Button> : null}
        {historical ? <MessageStrip tone="info"><p>This conversation belongs to an earlier run and is read-only.</p></MessageStrip> : null}
        {selection === 'new' && !outgoing ? <div className="chat-empty">
          <h1>What should we handle first today?</h1>
          <div className="chat-empty-actions">
            <Button disabled={blocked} onClick={() => chat(snapshot?.active_plan_version ? analyseNow : startDay)}>{snapshot?.active_plan_version ? 'Review the shop floor' : 'Plan today'}</Button>
            <Button variant="outline" disabled={!canWrite} onClick={() => { setDraft(current => current || 'A disruption happened on the shop floor: '); composerInput.current?.focus() }}>Report a disruption</Button>
          </div>
        </div> : null}
        {selection !== 'new' && !detail && !readError ? <div className="chat-skeleton" role="status" aria-label="Loading the conversation"><span /><span /><span /></div> : null}
        {readError ? <MessageStrip tone="critical"><p>{readError}</p></MessageStrip> : null}
        {timeline.map(item => {
          if (item.type === 'study') return studyCard(item.job)
          if (item.type === 'candidate') {
            const record = item.record
            const index = candidates.indexOf(record)
            return <div className="chat-card" key={record.candidate.candidate_id} id={`plan-${record.candidate.candidate_id}`} aria-label="Schedule plan"><ProposalCard record={record} workspace={workspace!} roles={factory.roles} preferred={index === 0 && (Boolean(data?.learning.active) || record.candidate.candidate_id === recommendedId)} disabled={blocked || Boolean(readError)} submitted={actions.some(a => a.kind === 'approve' && a.payload.candidate_hash === record.candidate.content_hash && a.state !== 'FAILED')}
              onPreview={() => onPreview(record.candidate.candidate_id)} onApprove={payload => command('approve', payload)} onRevise={() => { setDraft(current => [current, 'Please adjust this plan: '].filter(Boolean).join('\n')); composerInput.current?.focus() }} onRecalculate={() => chat('The current plan has expired. Check the latest factory facts, recalculate, and give me a new plan to approve.')} /></div>
          }
          if (item.type === 'publication') {
            const p = item.publication
            return <Card asChild key={p.release.release_id}><article className="execution-card" id={`plan-${p.candidate_id}`}><Badge tone={p.release.source_state === 'ACTIVE' ? 'positive' : p.release.source_state === 'REJECTED' ? 'negative' : 'info'}>{p.release.source_state === 'ACTIVE' ? 'Accepted by factory' : p.release.source_state === 'REJECTED' ? 'Rejected by factory' : 'Checking the factory receipt'}</Badge><p>{{ NOT_STARTED: 'Not started', IN_PROGRESS: 'In production', COMPLETED: 'Plan completed', BLOCKED: 'Execution blocked; following up' }[p.release.execution_state]}</p><Button variant="ghost" onClick={() => onPreview(p.candidate_id)}>View on the timeline →</Button></article></Card>
          }
          if (item.type === 'user') return <div className="chat-message is-user" key={item.id}><p>{item.message}</p></div>
          if (item.type === 'action') {
            const a = item.action
            return <Card asChild key={item.id}><article className={`chat-receipt${a.state === 'FAILED' || a.state === 'ATTENTION' ? ' is-error' : ''}`}>
              <span className="chat-receipt-icon" aria-hidden="true">{a.state === 'DONE' ? '✓' : a.state === 'QUEUED' ? '◷' : '!'}</span>
              <div><strong>{{ start: "Today's production", approve: 'Approve and execute', recover: 'Recovery record', outage: 'Machine down', scenario: 'Random event', preference: 'Recommendation preference', reset: 'Preference reset', business_accept: 'Business terms confirmed', treatment_execute: 'Plan execution' }[a.kind]}</strong><p>{plainBusinessText(text(a.result?.summary), zone) || (a.state === 'QUEUED' ? 'Received and processing. It continues if you close the page.' : 'Recorded.')}</p>{a.state === 'ATTENTION' && a.kind !== 'treatment_execute' ? <Button variant="outline" disabled={blocked} onClick={() => { void send({ kind: 'action', body: { request_id: a.request_id, run_id: a.run_id, kind: a.kind, payload: a.payload }, caseId: null, taskId: null }) }}>Check and continue the original action</Button> : null}{a.kind === 'treatment_execute' && !['DONE', 'CANCELLED'].includes(a.state) ? <div className="proposal-actions">
                {a.state === 'ATTENTION' && a.result?.can_resume === true ? <Button variant="outline" disabled={blocked} onClick={() => { void send({ kind: 'execution', body: { request_id: crypto.randomUUID(), decision: 'resume' }, taskId: a.action_id, caseId: detail?.case_id ?? null }) }}>Check and continue the original execution</Button> : null}
                <Button variant="outline" disabled={blocked || a.result?.stage === 'PUBLISHING'} onClick={() => { void send({ kind: 'execution', body: { request_id: crypto.randomUUID(), decision: 'cancel' }, taskId: a.action_id, caseId: detail?.case_id ?? null }) }}>Cancel remaining steps</Button>
                <small>Cancelling does not undo completed measures or costs already incurred.</small>
              </div> : null}</div>
            </article></Card>
          }
          const o = item.operation
          // Asking for approval is the Agent's hand-over; its reason is the short explanation of the card.
          const summary = plainBusinessText((o.action === 'request_approval' ? o.reason_summary : text(o.result?.summary) || o.reason_summary) || '', zone)
          if (o.result?.await_user === true && boundedCodes.includes(text(o.result?.code))) return <div className="chat-system" key={item.id}><span>System</span><p>{summary}</p></div>
          if (Object.hasOwn(toolNames, o.action) && o.result?.await_user !== true) {
            const reason = plainBusinessText(o.reason_summary ?? '', zone)
            const rejected = o.result?.status === 'REJECTED' ? plainBusinessText(text(o.result?.summary), zone) : ''
            return <div className="chat-tool" key={item.id}>
              <div><b>{toolNames[o.action]}</b><span>{toolStatus(o)}</span></div>
              {reason || rejected ? <p>{rejected || reason}</p> : null}
            </div>
          }
          return <article className="chat-message is-agent" key={item.id}>
            <span className="chat-byline">Agent</span>
            {summary ? <RevealText value={summary} createdAt={o.created_at} /> : null}
            {o.state !== 'DONE' ? <span className="muted">Processing…</span> : null}
            {attachedStudies.has(o.operation_id) ? studyCard(attachedStudies.get(o.operation_id)!) : null}
            {o.operation_id === latestReply?.operation_id && choices.length ? <div className="assistant-choices">{choices.map(choice => <Button variant="outline" key={choice} disabled={blocked} onClick={() => chat(choice)}>{choice}</Button>)}</div> : null}
          </article>
        })}
        {outgoing && !detail?.inputs.some(input => input.input_key === `user:${outgoing.requestId}` && detail.case_id === outgoing.caseId) ? <div className="chat-message is-user is-pending" aria-label="Message being sent"><p>{outgoing.message}</p>{journal.pending?.kind === 'chat' && !busy ? <small>Submission result pending</small> : null}</div> : null}
        {detail?.error_code ? <MessageStrip tone="critical">
          <p>{gateway429 ? 'The model service is busy or out of quota, so this analysis did not finish. The record is kept; switch the model in Settings or continue later.' : invalidAction ? 'The Agent did not produce a runnable action this turn; the shop floor was not changed. You can continue from the latest facts.' : turnBudgetExhausted ? 'This analysis is paused; the facts checked so far stay in the conversation.' : legacyBudgetExhausted ? 'Analysis paused. You can continue this conversation; if it pauses again, start a new one.' : caseBudgetExhausted ? 'This conversation has reached its analysis limit; the record is kept. Start a new conversation to continue.' : 'Automatic handling is blocked for now; the record is kept. Add requirements and continue.'}</p>
          {invalidAction && !historical && !terminalCases.includes(detail.state) ? <Button variant="outline" disabled={blocked} onClick={() => chat('Continue the current issue from the latest factory facts: first check the shop floor changes I mentioned, then analyze the impact and give me feasible plans to approve.')}>Check facts and continue</Button> : null}
          {gateway429 && !historical && !terminalCases.includes(detail.state) ? <Button variant="outline" disabled={blocked} onClick={() => chat('Continue analyzing the current issue from the latest factory facts and give me feasible plans to approve.')}>Continue analysis later</Button> : null}
          {budgetExhausted && !historical && !terminalCases.includes(detail.state) ? <Button variant="outline" disabled={blocked} onClick={() => chat('Continue analyzing the current disruption from the shop floor facts already checked in this conversation and give me feasible plans to review.')}>Continue this conversation</Button> : null}
          {caseBudgetExhausted ? <Button variant="outline" disabled={busy || Boolean(journal.pending)} onClick={() => onSelect('new')}>Start a new conversation</Button> : null}
        </MessageStrip> : null}
        {informationTasks.map(task => <InformationCard key={`${task.task_id}:${task.version}`} task={task} zone={zone ?? 'UTC'} disabled={blocked} onAnswer={answer => { void send({ kind: 'response', caseId: task.case_id, taskId: task.task_id, body: { request_id: crypto.randomUUID(), expected_task_version: task.version, answer } }) }} />)}
        {processing && activity ? <div className="chat-activity" role="status">
          <span className="status-dot" aria-hidden="true" /><span>{activity}</span>{elapsed !== null && !busy && phase !== 'IDLE' ? <span className="chat-activity-time">{elapsed} s</span> : null}
          {detail?.activity?.stop_target ? <Button variant="ghost" disabled={blocked} aria-label="Stop this analysis" title="Stop this analysis; factory time keeps running" onClick={stopAnalysis}><Square size={12} aria-hidden="true" />Stop</Button> : null}
        </div> : phase === 'STOPPED' ? <p className="chat-activity is-stopped" role="status">Analysis stopped. You can type new requirements.</p> : null}
      </div>
    </div>
    <footer className="assistant-footer">
      {readingEarlier ? <Button variant="outline" className="to-latest" onClick={() => { if (feed.current) feed.current.scrollTop = feed.current.scrollHeight; nearBottom.current = true; setReadingEarlier(false) }}>Back to latest</Button> : null}
      {journal.error || error ? <MessageStrip tone="negative" alert><p>{journal.error || error}</p></MessageStrip> : null}
      {journal.canArchiveRetiredBusiness ? <Button variant="outline" disabled={busy} onClick={() => { journal.archiveRetiredBusiness() }}>Keep old records and continue</Button> : null}
      {journal.pending ? <Button variant="outline" disabled={busy} onClick={() => { if (journal.pending) void send(journal.pending) }}>Check the original request</Button> : null}
      <form className="assistant-composer" onSubmit={e => { e.preventDefault(); chat(draft) }}>
        <label className="visually-hidden" htmlFor="assistant-message">Request to the Production Agent</label>
        <Textarea id="assistant-message" ref={composerInput} value={draft} maxLength={8000} rows={1} disabled={!canWrite} placeholder="Type a message…" onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); chat(draft) } }} />
        <Button type="submit" className="composer-send" disabled={blocked || !draft.trim()} aria-label="Send to the Production Agent"><ArrowUp size={18} aria-hidden="true" /></Button>
      </form>
    </footer>
  </section>
}

function mergeHistory(current: CaseDetail, older: CaseDetail | null, keepOlderCursor = true): CaseDetail {
  if (!older || current.case_id !== older.case_id) return current
  return {
    ...current,
    history_cursor: (keepOlderCursor ? older.history_cursor : current.history_cursor) ?? null,
    inputs: [...new Map([...older.inputs, ...current.inputs].map(input => [input.input_id, input])).values()].sort((a, b) => a.created_at.localeCompare(b.created_at)),
    operations: [...new Map([...older.operations, ...current.operations].map(operation => [operation.operation_id, operation])).values()].sort((a, b) => a.created_at.localeCompare(b.created_at)),
  }
}

function ProposalCard({ record, workspace, roles, preferred, disabled, submitted, onPreview, onApprove, onRevise, onRecalculate }: {
  record: CandidateRecord; workspace: Workspace; roles: string[]; preferred: boolean; disabled: boolean; submitted: boolean
  onPreview: () => void; onApprove: (payload: Record<string, unknown>) => void; onRevise: () => void; onRecalculate: () => void
}) {
  const [overtime, setOvertime] = useState(false)
  const [remember, setRemember] = useState(false)
  const [priority, setPriority] = useState('auto')
  const c = record.candidate
  const needsOvertime = c.required_consents.includes('allow_overtime')
  const progress = record.state === 'STALE' && progressReviewReason(record, workspace.snapshot, workspace.objective_state ?? null) === null
  const expired = !workspace.snapshot || Date.parse(c.accept_before) <= Date.parse(workspace.snapshot.snapshot_clock)
  const stale = expired || (record.state === 'STALE' && !progress) || workspace.objective_state?.objective_version !== c.binding.objective_version
  const ready = Boolean(record.review) && !disabled && !submitted && !stale && canPublishAutomatically(workspace.execution_support) && (!needsOvertime || (overtime && roles.includes('manager')))
  const proposalMetrics = ['weighted_tardiness', 'incremental_overtime_metric', 'changed_operations']
  return <Card asChild><article className={`proposal-card${preferred ? ' is-recommended' : ''}`}>
    <div className="proposal-top"><span className="eyebrow">Schedule plan</span><Badge tone={stale ? 'critical' : preferred ? 'positive' : 'neutral'}>{stale ? 'Needs recalculation' : preferred ? 'Recommended' : 'Ready for approval'}</Badge></div>
    <h3>{needsOvertime ? 'Production schedule with overtime' : 'Regular-shift production schedule'}</h3>
    <p className="proposal-scope">No extra purchases or resource measures</p>
    <div className="proposal-metrics">{c.objective.filter(m => proposalMetrics.includes(m.name)).map(m => <div key={m.name}><span>{metricNames[m.name]}</span><strong>{m.value ?? 'Pending'}</strong><small>{unitNames[m.unit] ?? m.unit}</small></div>)}</div>
    {record.review ? <PlanReview review={record.review} zone={workspace.snapshot?.profile.timezone ?? 'UTC'} /> : <p className="field-hint">Order impact details are not available yet; approval is not possible.</p>}
    <Button variant="ghost" onClick={onPreview}>Preview on the timeline →</Button>
    {needsOvertime ? <label className="checkbox-label"><Input type="checkbox" checked={overtime} disabled={!roles.includes('manager') || disabled || submitted} onChange={e => setOvertime(e.target.checked)} />Also approve the overtime this plan needs</label> : null}
    <details className="proposal-learning"><summary>Remember this choice as a preference</summary><label className="checkbox-label"><Input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} />Use this choice to adjust future recommendations</label><label>Reason<select value={priority} onChange={e => setPriority(e.target.value)}><option value="auto">Infer from the plan differences</option>{(Object.keys(priorityNames) as Priority[]).map(p => <option value={p} key={p}>{priorityNames[p]}</option>)}</select></label></details>
    <div className="proposal-actions"><Button className="approve" disabled={!ready} onClick={() => onApprove({ candidate_id: c.candidate_id, candidate_hash: c.content_hash, allow_overtime: overtime, remember, priority })}>{submitted ? 'Submitted, executing' : 'Approve and execute'}</Button><Button variant="outline" disabled={disabled} onClick={stale ? onRecalculate : onRevise}>{stale ? 'Recalculate with latest facts' : 'Request changes'}</Button></div>
    <p className="proposal-source">{c.termination_reason === 'COMPLETED' && !c.solver_passes?.length ? 'Preset plan · Checked' : progress ? 'Production in progress; the remaining schedule is checked on approval' : 'Checked · Not proven optimal'}</p>
  </article></Card>
}

export function InformationCard({ task, zone, disabled, onAnswer }: { task: HumanTask; zone: string; disabled: boolean; onAnswer: (answer: Record<string, unknown>) => void }) {
  const [error, setError] = useState('')
  const names: Record<string, string> = { repair_eta: `Expected recovery time (${zone})`, receipt_eta: `Expected arrival time (${zone})`, remaining_minutes: 'Remaining production minutes', remaining_setup_minutes: 'Remaining changeover minutes', comment: 'Note' }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (disabled) return
    setError('')
    try {
      const values = new FormData(event.currentTarget)
      const answer = Object.fromEntries(task.fields.map(field => {
        const value = String(values.get(field) ?? '')
        return [field, field.endsWith('_minutes') ? Number(value) : field.endsWith('_eta') ? factoryLocalToUtc(value, zone) : value]
      }))
      onAnswer(answer)
    } catch { setError('The time was not recognized; enter a valid date and time in factory local time.') }
  }
  return <form className="information-card" onSubmit={submit}><Badge tone="critical">Your input needed</Badge><h3>{task.question}</h3>{task.fields.map(field => <label key={field}>{names[field]}<Input name={field} required disabled={disabled} type={field.endsWith('_minutes') ? 'number' : field.endsWith('_eta') ? 'datetime-local' : 'text'} min={0} step={field.endsWith('_minutes') ? 1 : undefined} /></label>)}<span className="field-hint">Due: {dateTime(task.due_at, zone)}</span>{error && <p role="alert">{error}</p>}<Button variant="outline" disabled={disabled}>Submit</Button></form>
}

/** Newly arrived Agent text unfolds progressively; opening an older conversation shows it at once. */
function RevealText({ value, createdAt }: { value: string; createdAt: string }) {
  const [start] = useState(() => (Date.now() - Date.parse(createdAt) < 15_000 ? performance.now() : null))
  const [shown, setShown] = useState(() => (start === null ? value.length : 0))
  useEffect(() => {
    if (start === null) return
    const duration = Math.min(1600, 250 + value.length * 12)
    // A timer keeps revealing in a hidden or background window, where animation frames stop.
    // It runs until the text is complete: an unchanged count must not end the reveal.
    const timer = window.setInterval(() => {
      const next = Math.min(value.length, Math.ceil(((performance.now() - start) / duration) * value.length))
      setShown(next)
      if (next >= value.length) window.clearInterval(timer)
    }, 16)
    return () => window.clearInterval(timer)
  }, [start, value.length])
  return <AssistantText value={shown >= value.length ? value : value.slice(0, shown)} />
}
