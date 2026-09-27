import { businessJob } from './test/businessFixture'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AssistantPanel, InformationCard } from './AssistantPanel'
import type { ChatRequest } from './AssistantPanel'
import { AgentSidebar } from './AgentSidebar'
import { AgentTopbar } from './Topbar'
import { useConversations } from './useConversations'
import * as api from './api'
import { boardSnapshot, day } from './test/boardFixture'
import { baselineEffective } from './test/preferenceFixture'
import { declaredExecutionSupport } from './test/executionFixture'
import type { CandidateRecord, Workspace } from './contracts'
import type { Learning } from './assistantContracts'
import type { CaseDetail, HumanTask } from './caseContracts'
import type { RiskSuggestion } from './riskContracts'
import { rankCandidates } from './assistantModel'

vi.mock('./api', async importOriginal => ({ ...await importOriginal<typeof import('./api')>(), readAssistant: vi.fn(), readCases: vi.fn(), readCase: vi.fn(), readCaseHistory: vi.fn(), readRiskSuggestions: vi.fn(), postCaseMessage: vi.fn(), stopCaseTurn: vi.fn(), submitAssistant: vi.fn() }))
const snapshot = boardSnapshot()
const selectionKey = `byof.conversation:reviewer:${snapshot.factory_id}`
const learning: Learning = { weights: { delivery: 0.15, stability: 0.15, overtime: 0.7 }, samples: 3, active: true, explicit: null, summary: 'Only adjusts the recommendation order.' }
function candidate(id = 'c1', overtime = 0, tardiness = 0): CandidateRecord {
  return { run_id: 'run-1', snapshot_id: 'snap-1', state: 'CANDIDATE', approvals: [], review: { as_of: snapshot.snapshot_clock, accept_before: `${day}T09:00:00Z`, orders: [], overtime: [] }, candidate: {
    candidate_id: id, factory_id: snapshot.factory_id, content_hash: id.repeat(32), version: 1,
    binding: { snapshot_hash: snapshot.content_hash, profile_version: 'profile-1', policy_version: 'policy-1', objective_version: 'delivery-v1', baseline_plan_version: null },
    has_solution: true, native_status: 'FEASIBLE', termination_reason: 'COMPLETED', proven_objective_levels: 0,
    assignments: [], scenario: [], required_consents: overtime ? ['allow_overtime'] : [], checker: { status: 'PASS', issues: [] },
    effective_not_before: `${day}T01:00:00Z`, accept_before: `${day}T09:00:00Z`,
    objective: [{ name: 'weighted_tardiness', value: tardiness, unit: 'minutes', lower_bound: null, unknown_reason: null }, { name: 'incremental_overtime_metric', value: overtime, unit: 'minutes', lower_bound: null, unknown_reason: null }, { name: 'changed_operations', value: 2, unit: 'operations', lower_bound: null, unknown_reason: null }],
  } }
}
function conversationCase(caseId = 'case-1', patch: Partial<CaseDetail> = {}): CaseDetail {
  return { case_id: caseId, factory_id: snapshot.factory_id, run_id: snapshot.run_id!, owner_id: 'reviewer', title: caseId, state: 'WAITING', version: 1, created_at: snapshot.snapshot_clock, updated_at: snapshot.snapshot_clock, snapshot_id: snapshot.snapshot_id, context: {}, closure: null, error_code: null, operations: [], inputs: [], ...patch }
}
/** Conversations the list returns; the first becomes the tab's remembered selection. */
function withCases(...details: CaseDetail[]) {
  vi.mocked(api.readCases).mockResolvedValue(details)
  vi.mocked(api.readCase).mockImplementation(async (_factory, id) => details.find(item => item.case_id === id) ?? details[0]!)
  if (details[0]) sessionStorage.setItem(selectionKey, details[0].case_id)
}

function Harness({ workspace, roles, preview, suggestions }: { workspace: Workspace; roles: string[]; preview: (id: string) => void; suggestions: RiskSuggestion[] }) {
  const [ended] = useState(() => vi.fn())
  const chats = useConversations(snapshot.factory_id, 'reviewer', snapshot.run_id, ended)
  const [request, setRequest] = useState<ChatRequest | null>(null)
  return <>
    <AgentSidebar conversations={chats.conversations} selection={chats.selection} view="chat" onSelect={chats.select} onView={() => {}} />
    <AgentTopbar snapshot={snapshot} freshness="CURRENT" suggestions={suggestions} onSuggestion={item => { chats.select('new'); setRequest({ key: item.suggestion_id, message: item.prompt, suggestionId: item.suggestion_id }) }} />
    <AssistantPanel factory={{ factory_id: snapshot.factory_id, roles, last_synced_at: null, snapshot_id: null }} userId="reviewer" workspace={workspace} tasks={[]}
      selection={chats.selection} onSelect={chats.select} request={request} onRequestHandled={() => setRequest(null)}
      onChanged={vi.fn().mockResolvedValue(undefined)} onSessionEnded={ended} onPreview={preview} />
  </>
}

function open(records: CandidateRecord[] = [], roles = ['planner', 'manager'], overrides: Partial<Workspace> = {}, suggestions: RiskSuggestion[] = []) {
  if (records.length && !sessionStorage.getItem(selectionKey)) {
    withCases(conversationCase('plan-case', { title: "Today's plan", context: { candidate_ids: records.map(r => r.candidate.candidate_id) } }))
  }
  const workspace: Workspace = { snapshot, freshness: 'CURRENT', last_synced_at: snapshot.snapshot_clock, candidates: records, jobs: records.map(r => ({ job_id: r.candidate.candidate_id, state: 'SUCCEEDED', candidate_id: r.candidate.candidate_id, error_code: null, created_at: snapshot.snapshot_clock, allow_overtime: false })), objective_state: baselineEffective, execution_support: declaredExecutionSupport(), publications: [], ...overrides }
  const preview = vi.fn()
  const result = render(<Harness workspace={workspace} roles={roles} preview={preview} suggestions={suggestions} />)
  return { ...result, preview }
}
const history = () => within(screen.getByRole('navigation', { name: 'Conversations' }))
beforeEach(() => {
  sessionStorage.clear()
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning, material_balance: { snapshot_id: snapshot.snapshot_id, shortfalls: [] } })
  vi.mocked(api.readCases).mockResolvedValue([])
  vi.mocked(api.readCase).mockReset()
  vi.mocked(api.readRiskSuggestions).mockResolvedValue({ run_id: snapshot.run_id!, freshness: 'CURRENT', suggestions: [] })
  vi.mocked(api.submitAssistant).mockImplementation(async (_factory, body) => ({ ...body, action_id: 'a1', state: 'QUEUED', result: null, created_at: snapshot.snapshot_clock } as never))
  vi.mocked(api.postCaseMessage).mockResolvedValue({ case_id: 'case-1' } as never)
  vi.mocked(api.stopCaseTurn).mockResolvedValue({ case_id: 'case-1' } as never)
})

describe('conversation workbench', () => {
  it('opens on a new conversation; existing conversations are only listed on the left and no card is inserted automatically', async () => {
    const earlier = conversationCase('earlier', { title: 'Yesterday shortage', context: { candidate_ids: ['c1'] } })
    vi.mocked(api.readCases).mockResolvedValue([earlier])
    open([], ['planner', 'manager'], { candidates: [candidate('c1')], publications: [] })
    expect(await history().findByRole('button', { name: 'Yesterday shortage' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'What should we handle first today?' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Approve and execute' })).not.toBeInTheDocument()
    expect(api.readCase).not.toHaveBeenCalled()
  })
  it('shows the explanation while waiting for a source change and does not hide finished wait actions', async () => {
    withCases(conversationCase('case-1', { operations: [{ operation_id: 'wait-1', action: 'wait', reason_summary: 'Waiting for the source', parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'WAITING', summary: 'Waiting for the administrator to enter the customer-confirmed quantity before rescheduling.' }, created_at: snapshot.snapshot_clock }] }))
    open()
    expect(await screen.findByText('Waiting for the administrator to enter the customer-confirmed quantity before rescheduling.')).toBeVisible()
  })
  it('a new reply reveals in full after typing out; a refused wait is not shown as the Agent speaking', async () => {
    const now = new Date().toISOString()
    const full = 'The option was approved and released, and the factory accepted it; the plan is waiting to start. New shop floor changes will show in the top bar.'
    withCases(conversationCase('case-1', { operations: [
      { operation_id: 'refused', action: 'wait', reason_summary: 'Waiting', parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'REJECTED', code: 'CASE_FACTS_CHANGED', summary: 'This step did not run: the shop floor just changed or the request was a repeat.' }, created_at: now },
      { operation_id: 'fresh', action: 'reply', reason_summary: 'Explaining', parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'WAITING', summary: full }, created_at: now },
    ] }))
    open()
    expect(await screen.findByText(full, {}, { timeout: 4000 })).toBeVisible()
    expect(screen.queryByText('This step did not run: the shop floor just changed or the request was a repeat.')).not.toBeInTheDocument()
  })
  it('quick replies belong to the message that produced them instead of hanging after all result cards', async () => {
    withCases(conversationCase('case-1', { operations: [{ operation_id: 'reply-1', action: 'reply', reason_summary: 'Asking for direction', parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'WAITING', summary: 'Choose the next step.', choices: ['Verify the resupply'] }, created_at: snapshot.snapshot_clock }] }))
    open()
    const choice = await screen.findByRole('button', { name: 'Verify the resupply' })
    expect(choice.closest('article')).toContainElement(screen.getByText('Choose the next step.'))
  })
  it('old quick replies disappear once a later wait replaced the old question', async () => {
    withCases(conversationCase('case-1', { operations: [
      { operation_id: 'reply-1', action: 'reply', parameters: {}, reason_summary: null, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'WAITING', summary: 'Choose a direction.', choices: ['Verify the resupply'] }, created_at: snapshot.snapshot_clock },
      { operation_id: 'wait-1', action: 'wait', parameters: {}, reason_summary: null, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'WAITING', summary: 'Choice received; waiting for the receipt.' }, created_at: `${day}T12:00:00Z` },
    ] }))
    open()
    await screen.findByText('Choose a direction.')
    expect(screen.queryByRole('button', { name: 'Verify the resupply' })).not.toBeInTheDocument()
  })
  it('internal fields and raw times in Agent text are shown in business wording', async () => {
    withCases(conversationCase('case-1', { operations: [{ operation_id: 'reply-1', action: 'reply', reason_summary: null, parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { summary: 'Both solves returned native_status=UNKNOWN, has_solution=false; earliest start 2026-09-26T00:45:00Z.' }, created_at: snapshot.snapshot_clock }] }))
    open()
    const reply = await screen.findByText(/Both solves returned/)
    expect(reply).toHaveTextContent('no feasible schedule found')
    expect(reply).not.toHaveTextContent('native_status')
    expect(reply).not.toHaveTextContent('has_solution')
    expect(reply).not.toHaveTextContent('T00:45')
  })
  it('when submitting for approval the Agent explanation is shown as its message with status words in business terms', async () => {
    withCases(conversationCase('case-1', { operations: [{ operation_id: 'ask', action: 'request_approval', reason_summary: 'Solve complete: FEASIBLE, independent check PASS; requesting manager approval.', parameters: { candidate_id: 'c1' }, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'PENDING', summary: 'registered' }, created_at: snapshot.snapshot_clock }] }))
    open()
    const message = await screen.findByText('Solve complete: feasible, checked; requesting manager approval.')
    expect(message.closest('article')).toHaveTextContent('Agent')
    expect(screen.queryByText('registered')).not.toBeInTheDocument()
  })
  it('approval is blocked without order impact details and learning preferences are off by default', async () => {
    const record = candidate()
    delete record.review
    open([record])
    expect(await screen.findByRole('button', { name: 'Approve and execute' })).toBeDisabled()
    expect(screen.getByLabelText('Use this choice to adjust future recommendations')).not.toBeChecked()
  })
  it('a manual arrival time uses the factory time zone instead of treating local input as UTC', () => {
    const task: HumanTask = { task_id: 't', factory_id: snapshot.factory_id, case_id: 'case', version: 1, question: 'When does it arrive', subject_id: 'receipt', owner_id: 'reviewer', owner_role: 'manager', fields: ['receipt_eta'], state: 'OPEN', response: null, created_at: snapshot.snapshot_clock, updated_at: snapshot.snapshot_clock, due_at: snapshot.snapshot_clock, clock: 'real', reminders_count: 0, send_state: 'NONE', delivery_state: 'NONE' }
    const answer = vi.fn()
    render(<InformationCard task={task} zone="Asia/Singapore" disabled={false} onAnswer={answer} />)
    fireEvent.change(screen.getByLabelText('Expected arrival time (Asia/Singapore)'), { target: { value: '2026-09-25T17:30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    expect(answer).toHaveBeenCalledWith({ receipt_eta: '2026-09-25T09:30:00.000Z' })
  })
  it('a queued analysis shows a visible state and the stop button stops the current input, not the factory clock', async () => {
    withCases(conversationCase('case-1', { title: 'Analyze the shop floor disruption', state: 'INVESTIGATING', activity: { phase: 'QUEUED', stop_target: 'input-1', started_at: snapshot.snapshot_clock }, inputs: [{ input_id: 'input-1', kind: 'USER', payload: { message: 'Analyze the shop floor disruption' }, created_at: snapshot.snapshot_clock, available_at: snapshot.snapshot_clock, turn_id: null }] }))
    open()
    expect(await screen.findByText('Message received; waiting for the Agent')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Stop this analysis' }))
    expect(api.stopCaseTurn).toHaveBeenCalledWith(snapshot.factory_id, 'case-1', expect.objectContaining({ expected_target: 'input-1' }), expect.any(AbortSignal))
  })
  it('a submitted plan calculation shows the calculation state and is not mistaken for a stoppable model call', async () => {
    withCases(conversationCase('case-2', { title: 'Scheduling', state: 'PLANNING', activity: { phase: 'SOLVING', stop_target: null, started_at: snapshot.snapshot_clock } }))
    open()
    expect(await screen.findByText('Calculating plans')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Stop this analysis' })).not.toBeInTheDocument()
  })
  it('when the same text is sent again the new message shows its submission state until the matching request is recorded', async () => {
    withCases(conversationCase('case-1', { title: 'Risk', inputs: [{ input_id: 'old-input', input_key: 'user:older-request', kind: 'USER', payload: { message: 'Analyze again' }, created_at: snapshot.snapshot_clock, available_at: snapshot.snapshot_clock, turn_id: 'old-turn' }] }))
    open()
    await screen.findByText('Analyze again')
    await userEvent.type(screen.getByRole('textbox', { name: 'Request to the Production Agent' }), 'Analyze again')
    await userEvent.click(screen.getByRole('button', { name: 'Send to the Production Agent' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalled())
    expect(screen.getByLabelText('Message being sent')).toHaveTextContent('Analyze again')
    expect(await screen.findByText('Message received; waiting for the Agent')).toBeVisible()
  })
  it('after a refresh conversations of an old run neither occupy the current input nor mix into the new run list', async () => {
    const old = conversationCase('old-case', { run_id: 'previous-run', title: 'Discussion of the old run' })
    sessionStorage.setItem(selectionKey, old.case_id)
    vi.mocked(api.readCases).mockResolvedValue([old])
    vi.mocked(api.readCase).mockResolvedValue(old)
    open()
    expect(await screen.findByRole('heading', { name: 'What should we handle first today?' })).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Request to the Production Agent' })).toBeEnabled()
    expect(history().queryByRole('button', { name: /Discussion of the old run/ })).not.toBeInTheDocument()
  })
  it.each([
    ['QUEUED', null, 'Queued'],
    ['RUNNING', null, 'Calculating'],
    ['FAILED', null, 'Calculation not completed'],
    ['SUCCEEDED', 'failed', 'No feasible schedule found within the time limit'],
    ['SUCCEEDED', 'succeeded', 'Plan found'],
  ] as const)('schedule calculations show the actual job result: %s %s', async (state, candidateId, label) => {
    const failed = candidate('failed'), succeeded = candidate('succeeded')
    failed.candidate.has_solution = false
    failed.candidate.native_status = 'UNKNOWN'
    failed.candidate.checker = { status: 'FAIL', issues: [] }
    withCases(conversationCase('case-1', { operations: [{ operation_id: 'solve', action: 'solve_scenario', reason_summary: "Schedule today's production without overtime first.", parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'PENDING', summary: 'Solve job registered; wait for the actual result.', job_id: 'job' }, created_at: snapshot.snapshot_clock }] }))
    open([], undefined, { candidates: [failed, succeeded], jobs: [{ job_id: 'job', state, candidate_id: candidateId, error_code: null, created_at: snapshot.snapshot_clock, allow_overtime: false }] })
    expect(await screen.findByText(label)).toBeVisible()
    expect(screen.getByText("Schedule today's production without overtime first.")).toBeVisible()
    expect(screen.queryByText('Solve job registered; wait for the actual result.')).not.toBeInTheDocument()
  })
  it('a blocked calculation under the same conditions shows as a system notice, not as Agent speech', async () => {
    withCases(conversationCase('case-1', { operations: [{ operation_id: 'guard', action: 'solve_scenario', reason_summary: 'Calculate again', parameters: {}, snapshot_id: snapshot.snapshot_id, state: 'DONE', result: { status: 'WAITING', code: 'UNCHANGED_SEARCH', await_user: true, summary: 'Already calculated under the same conditions; no feasible schedule was found within the time limit (this does not mean there is no solution). Automatic recalculation stopped.' }, created_at: snapshot.snapshot_clock }] }))
    open()
    const notice = await screen.findByText(/Automatic recalculation stopped/)
    expect(notice.closest('.chat-system')).toHaveTextContent('System')
    expect(notice.closest('article')).toBeNull()
  })
  it('conversations can be switched and a new conversation does not append messages to an old one', async () => {
    const make = (id: string) => conversationCase(id, { title: id, inputs: [{ input_id: id, kind: 'USER', payload: { message: `${id} message` }, created_at: snapshot.snapshot_clock, available_at: snapshot.snapshot_clock, turn_id: null }] })
    const first = make('yesterday'), second = make('today')
    vi.mocked(api.readCases).mockResolvedValue([second, first])
    vi.mocked(api.readCase).mockImplementation(async (_factory, id) => id === first.case_id ? first : second)
    open()
    await userEvent.click(await history().findByRole('button', { name: second.title }))
    await screen.findByText('today message')
    await userEvent.click(history().getByRole('button', { name: first.title }))
    await screen.findByText('yesterday message')
    expect(screen.queryByText('today message')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'New conversation' }))
    expect(screen.queryByText('yesterday message')).not.toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: 'Request to the Production Agent' }), 'Analyze the current shop floor again')
    await userEvent.click(screen.getByRole('button', { name: 'Send to the Production Agent' }))
    expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, null, expect.objectContaining({ start_new: true }), expect.any(AbortSignal))
  })
  it('after the per-turn limit the analysis can continue in the same conversation', async () => {
    withCases(conversationCase('case-paused', { title: 'Analyze the shop floor disruption', error_code: 'MODEL_BUDGET_EXHAUSTED', inputs: [{ input_id: 'input-1', kind: 'USER', payload: { message: 'Analyze the current shop floor disruption' }, created_at: snapshot.snapshot_clock, available_at: snapshot.snapshot_clock, turn_id: null }] }))
    open()
    await screen.findByText('Analyze the current shop floor disruption')
    await userEvent.click(screen.getByRole('button', { name: 'Continue this conversation' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, 'case-paused', expect.objectContaining({ message: expect.stringContaining('Continue analyzing') }), expect.any(AbortSignal)))
    expect(vi.mocked(api.postCaseMessage).mock.calls.at(-1)![2]).not.toHaveProperty('start_new')
  })
  it('when the whole conversation reaches its limit it leads to a new conversation', async () => {
    withCases(conversationCase('case-exhausted', { title: 'Old conversation', error_code: 'MODEL_CASE_BUDGET_EXHAUSTED' }))
    open()
    await screen.findByText(/This conversation has reached its analysis limit/)
    expect(screen.queryByRole('button', { name: 'Continue this conversation' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Start a new conversation' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Request to the Production Agent' }), 'Please continue')
    await userEvent.click(screen.getByRole('button', { name: 'Send to the Production Agent' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, null, expect.objectContaining({ start_new: true }), expect.any(AbortSignal)))
  })
  it('ordinary chat only creates user input and never writes hypothetical questions into the simulated source', async () => {
    open()
    const box = screen.getByRole('textbox', { name: 'Request to the Production Agent' })
    await userEvent.type(box, 'What if a machine goes down?')
    await userEvent.click(screen.getByRole('button', { name: 'Send to the Production Agent' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalled())
    expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, null, expect.objectContaining({ message: 'What if a machine goes down?', start_new: true }), expect.any(AbortSignal))
    expect(api.submitAssistant).not.toHaveBeenCalled()
  })
  it('field changes are sent to the Agent as the first message of a new conversation only after the manager clicks them in the top bar', async () => {
    const suggestion = { suggestion_id: 'a'.repeat(64), run_id: snapshot.run_id!, source_revision: '2', title: 'Handle the material supply change', detail: 'Expected receipt reduced', prompt: 'Check the short delivery and propose feasible plans.' }
    open([], undefined, {}, [suggestion])
    await userEvent.click(screen.getByRole('button', { name: /Field changes 1/ }))
    expect(api.postCaseMessage).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /Handle the material supply change/ }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, null,
      expect.objectContaining({ message: suggestion.prompt, suggestion_id: suggestion.suggestion_id, start_new: true }), expect.any(AbortSignal)))
    expect(api.submitAssistant).not.toHaveBeenCalled()
  })
  it('an explicit gateway rejection explains the 429 and can continue later in the same conversation', async () => {
    withCases(conversationCase('case-limited', { title: 'Shortage', error_code: 'MODEL_GATEWAY_429' }))
    open()
    expect(await screen.findByText(/The model service is busy/)).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Continue analysis later' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, 'case-limited',
      expect.objectContaining({ message: expect.stringContaining('Continue analyzing') }), expect.any(AbortSignal)))
  })
  it('an input method confirmation and Shift+Enter do not send by mistake', async () => {
    open()
    const box = screen.getByRole('textbox', { name: 'Request to the Production Agent' })
    fireEvent.change(box, { target: { value: 'Less overtime' } })
    fireEvent.keyDown(box, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(api.postCaseMessage).not.toHaveBeenCalled()
  })
  it('Plan today asks for a plan in natural language, reserves no review time and has no shop floor injection entry', async () => {
    open([], ['planner', 'manager'])
    expect(screen.queryByRole('button', { name: /Simulated event|Random event/ })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Plan today' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, null, expect.objectContaining({ message: expect.stringContaining('feasible plan'), start_new: true }), expect.any(AbortSignal)))
    expect(vi.mocked(api.postCaseMessage).mock.calls[0]![2].message).not.toContain('reserve')
    expect(api.submitAssistant).not.toHaveBeenCalled()
  })
  it('an overtime plan needs its own approval checkbox and a preview creates no approval', async () => {
    const { preview } = open([candidate('c1', 30)])
    await userEvent.click(await screen.findByRole('button', { name: 'Preview on the timeline →' }))
    expect(preview).toHaveBeenCalledWith('c1')
    expect(api.submitAssistant).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Approve and execute' })).toBeDisabled()
    await userEvent.click(screen.getByLabelText('Also approve the overtime this plan needs'))
    await userEvent.click(screen.getByRole('button', { name: 'Approve and execute' }))
    expect(api.submitAssistant).toHaveBeenCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'approve', payload: expect.objectContaining({ allow_overtime: true, candidate_id: 'c1', remember: false }) }), expect.any(AbortSignal))
  })
  it('an expired card keeps the preview but cannot be executed', async () => {
    const old = candidate()
    old.candidate.accept_before = snapshot.snapshot_clock
    open([old])
    expect(await screen.findByRole('button', { name: 'Approve and execute' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Preview on the timeline →' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Recalculate with latest facts' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, 'plan-case', expect.objectContaining({ message: expect.stringContaining('recalculate') }), expect.any(AbortSignal)))
  })
  it('an unknown network result keeps the request and checking reuses the same request ID', async () => {
    vi.mocked(api.postCaseMessage).mockRejectedValueOnce(new TypeError('network'))
    open()
    await userEvent.click(screen.getByRole('button', { name: 'Plan today' }))
    await screen.findByRole('button', { name: 'Check the original request' })
    const first = vi.mocked(api.postCaseMessage).mock.calls[0]![2]
    await userEvent.click(screen.getByRole('button', { name: 'Check the original request' }))
    await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.postCaseMessage).mock.calls[1]![2]).toEqual(first)
  })
})

it('learning only reorders plans computed from the same facts and keeps the order with too few samples', () => {
  const urgent = candidate('c1', 30, 0), economical = candidate('c2', 0, 10), old = candidate('c3', 0, 0)
  old.candidate.binding.snapshot_hash = 'older-facts'
  expect(rankCandidates([urgent, economical, old], learning)).toEqual([economical, urgent, old])
  expect(rankCandidates([urgent, economical], { ...learning, active: false })).toEqual([urgent, economical])
})

it('plans and executions not belonging to the current conversation are not shown in it', async () => {
  withCases(conversationCase())
  open([], undefined, { candidates: [candidate('other-case-plan')], jobs: [{ job_id: 'other', state: 'SUCCEEDED', candidate_id: 'other-case-plan', error_code: null, created_at: snapshot.snapshot_clock, allow_overtime: false }] })
  await waitFor(() => expect(api.readCase).toHaveBeenCalled())
  expect(screen.queryByRole('button', { name: 'Approve and execute' })).not.toBeInTheDocument()
})

it('a failed option comparison can continue in the same conversation without triggering approval or source writes', async () => {
  withCases(conversationCase())
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning, material_balance: { snapshot_id: snapshot.snapshot_id, shortfalls: [] }, business_studies: [{ ...businessJob(), state: 'FAILED', study: null, error_code: 'WORKER_FAILURE' }] })
  open()
  await screen.findByLabelText('Option comparison result')
  await userEvent.click(screen.getByRole('button', { name: 'Continue in this conversation' }))
  await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, 'case-1', expect.objectContaining({ message: expect.stringContaining('did not finish') }), expect.any(AbortSignal)))
  expect(api.submitAssistant).not.toHaveBeenCalled()
})

it('the current conversation shows option comparisons; the manager states choices in chat and there is no new order form', async () => {
  withCases(conversationCase())
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning, material_balance: { snapshot_id: snapshot.snapshot_id, shortfalls: [] }, business_studies: [businessJob({ allow_overtime: true, cost_minor: 12500, currency: 'CNY', quote_id: 'Q1' })] })
  open()
  expect(await screen.findByText('Regular-shift fulfilment')).toBeVisible()
  expect(api.readAssistant).toHaveBeenCalledWith(snapshot.factory_id, expect.any(AbortSignal), 'case-1')
  expect(screen.queryByRole('button', { name: /Compare rush order fulfilment|Compare shortage options|Confirm and record business terms/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('form', { name: /rush order|new order|shortage comparison/i })).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox', { name: 'Order ID' })).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: /verified/i })).not.toBeInTheDocument()
  await userEvent.type(screen.getByRole('textbox', { name: 'Request to the Production Agent' }), 'Protect existing due dates first; continue analyzing resupply options.')
  await userEvent.click(screen.getByRole('button', { name: 'Send to the Production Agent' }))
  await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, 'case-1', expect.objectContaining({ message: 'Protect existing due dates first; continue analyzing resupply options.' }), expect.any(AbortSignal)))
  expect(api.submitAssistant).not.toHaveBeenCalled()
})

it('new conversations and different conversations isolate business results; unlinked old trials stay out of the current conversation', async () => {
  const first = conversationCase(), second = conversationCase('case-2')
  const jobs = [
    businessJob({ title: 'This rush order comparison' }),
    { ...businessJob({ title: 'Shortage comparison of another conversation' }), job_id: 'study-2', case_id: second.case_id },
    { ...businessJob({ title: 'Unlinked old trial' }), job_id: 'study-old', case_id: null },
    { ...businessJob(), job_id: 'other-pending', case_id: 'case-3', state: 'RUNNING' as const, study: null },
  ]
  withCases(first, second)
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning, material_balance: { snapshot_id: snapshot.snapshot_id, shortfalls: [] }, business_studies: jobs })
  open()
  expect(await screen.findByText('This rush order comparison')).toBeVisible()
  expect(screen.queryByText('Shortage comparison of another conversation')).not.toBeInTheDocument()
  expect(screen.queryByText('Unlinked old trial')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'New conversation' }))
  expect(await screen.findByRole('heading', { name: 'What should we handle first today?' })).toBeVisible()
  expect(screen.queryByRole('region', { name: 'Response option comparison' })).not.toBeInTheDocument()
  await userEvent.click(history().getByRole('button', { name: second.title }))
  expect(await screen.findByText('Shortage comparison of another conversation')).toBeVisible()
  expect(api.readAssistant).toHaveBeenLastCalledWith(snapshot.factory_id, expect.any(AbortSignal), second.case_id)
  expect(screen.queryByText('This rush order comparison')).not.toBeInTheDocument()
  await userEvent.click(history().getByRole('button', { name: first.title }))
  expect(await screen.findByText('This rush order comparison')).toBeVisible()
  expect(screen.queryByText('Shortage comparison of another conversation')).not.toBeInTheDocument()
})

it('after archiving an old browser trial the chat can continue without resending the retired request', async () => {
  const key = `byof.pending.v1:reviewer:${snapshot.factory_id}`
  const pending = JSON.stringify({ version: 1, userId: 'reviewer', factoryId: snapshot.factory_id, slots: { assistant: { kind: 'study', caseId: null, taskId: null, body: { request_id: 'old-study', run_id: snapshot.run_id, expected_snapshot_hash: snapshot.content_hash, request: businessJob().request } } } })
  sessionStorage.setItem(key, pending)
  open()
  expect(await screen.findByText(/The old option comparison or acceptance entry has been retired/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Check the original request' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Plan today' })).toBeDisabled()
  expect(sessionStorage.getItem(key)).toBe(pending)
  await userEvent.click(screen.getByRole('button', { name: 'Keep old records and continue' }))
  expect(screen.queryByText(/The old option comparison or acceptance entry has been retired/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Plan today' })).toBeEnabled()
  const archive = Object.keys(sessionStorage).find(item => item.startsWith(`${key}:retired-business:`))
  expect(archive).toBeDefined()
  expect(sessionStorage.getItem(archive!)).toBe(pending)
  expect(api.postCaseMessage).not.toHaveBeenCalled()
  await userEvent.type(screen.getByRole('textbox', { name: 'Request to the Production Agent' }), 'Please analyze the current shop floor.')
  await userEvent.click(screen.getByRole('button', { name: 'Send to the Production Agent' }))
  await waitFor(() => expect(api.postCaseMessage).toHaveBeenCalledTimes(1))
  expect(api.postCaseMessage).toHaveBeenCalledWith(snapshot.factory_id, null, expect.objectContaining({ message: 'Please analyze the current shop floor.' }), expect.any(AbortSignal))
  expect(api.submitAssistant).not.toHaveBeenCalled()
})

it('the comparison card follows the Agent explanation after it and belongs to the same message', async () => {
  withCases(conversationCase('case-1', { operations: [
    { operation_id: 'before', action: 'reply', parameters: {}, state: 'DONE', snapshot_id: snapshot.snapshot_id, result: { summary: 'Check costs first.' }, created_at: '2026-09-14T00:00:00Z' },
    { operation_id: 'after', action: 'reply', parameters: {}, state: 'DONE', snapshot_id: snapshot.snapshot_id, result: { summary: 'Please comment on the comparison above.' }, created_at: '2026-09-14T23:00:00Z' },
  ] }))
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning, material_balance: { snapshot_id: snapshot.snapshot_id, shortfalls: [] }, business_studies: [{ ...businessJob(), created_at: '2026-09-14T12:00:00Z' }] })
  open()
  const card = await screen.findByLabelText('Option comparison result')
  expect(screen.getByText('Check costs first.').compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const explanation = screen.getByText('Please comment on the comparison above.')
  expect(explanation.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(explanation.closest('article')).toBe(card.closest('article'))
})

it('switching conversations and back keeps each draft, and editing can continue after a refresh', async () => {
  const first = conversationCase(), second = conversationCase('case-2')
  withCases(first, second)
  const initial = open()
  await waitFor(() => expect(api.readCase).toHaveBeenCalled())
  const input = screen.getByRole('textbox', { name: 'Request to the Production Agent' })
  fireEvent.change(input, { target: { value: 'First draft' } })
  await userEvent.click(await history().findByRole('button', { name: second.title }))
  await waitFor(() => expect(input).toHaveValue(''))
  fireEvent.change(input, { target: { value: 'Second draft' } })
  await userEvent.click(history().getByRole('button', { name: first.title }))
  await waitFor(() => expect(input).toHaveValue('First draft'))
  initial.unmount()
  open()
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Request to the Production Agent' })).toHaveValue('First draft'))
})

it('loads earlier chat by cursor, keeps the current messages and stops paging once history runs out', async () => {
  const current = conversationCase()
  current.history_cursor = { at: snapshot.snapshot_clock, id: 'cursor-id' }
  current.operations = [{ operation_id: 'new-reply', action: 'reply', parameters: {}, state: 'DONE', snapshot_id: snapshot.snapshot_id, result: { summary: 'Latest result' }, created_at: snapshot.snapshot_clock }]
  const earlier = { ...current, history_cursor: null, operations: [{ ...current.operations[0]!, operation_id: 'old-reply', result: { summary: 'Earlier cost discussion' } }] }
  withCases(current)
  vi.mocked(api.readCaseHistory).mockResolvedValue(earlier)
  open()
  await userEvent.click(await screen.findByRole('button', { name: 'Load earlier messages' }))
  expect(await screen.findByText('Earlier cost discussion')).toBeVisible()
  expect(screen.getByText('Latest result')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Load earlier messages' })).not.toBeInTheDocument()
  expect(api.readCaseHistory).toHaveBeenCalledWith(snapshot.factory_id, current.case_id, current.history_cursor, expect.any(AbortSignal))
})

it('while a plan card can be approved, chat buttons offer no options that look like approval', async () => {
  const job = businessJob({ economics: {
    catalog_version: 'byof-demo-economics/1', evidence_mode: 'synthetic', currency: 'SGD', status: 'ESTIMATED',
    revenue_minor: 100000, variable_cost_minor: 70000, additional_cost_minor: 0, late_deduction_minor: 0,
    net_contribution_minor: 30000, incremental_cash_outlay_minor: 0, improvement_minor: null, comparison_option_id: null,
    lines: [], assumptions: [], missing: [],
  } })
  job.request = { ...job.request, kind: 'production_exception', order: null }
  job.study = { ...job.study!, run_id: snapshot.run_id! }
  withCases(conversationCase('case-1', { operations: [
    { operation_id: 'explain', action: 'reply', parameters: {}, state: 'DONE', snapshot_id: snapshot.snapshot_id, result: { summary: 'Regular-shift fulfilment recommended.', choices: ['Submit option 1 for approval', 'Allow overtime and compare again'] }, created_at: '2026-09-14T23:00:00Z' },
  ] }))
  vi.mocked(api.readAssistant).mockResolvedValue({ actions: [], learning, material_balance: { snapshot_id: snapshot.snapshot_id, shortfalls: [] }, business_studies: [{ ...job, created_at: '2026-09-14T12:00:00Z' }] })
  open()
  expect(await screen.findByRole('button', { name: 'Approve and execute' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Allow overtime and compare again' })).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Submit option 1 for approval' })).not.toBeInTheDocument()
})
