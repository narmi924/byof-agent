import { Button } from './components/ui/button'
/** The product has two people: a manager decides with the Agent; a maintainer changes the simulated factory. */

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage, readFactories, readHumanTasks, readRiskSuggestions, readWorkspace } from './api'
import type { Factory, Workspace } from './contracts'
import type { HumanTask } from './caseContracts'
import type { RiskSuggestion } from './riskContracts'
import { AgentSidebar } from './AgentSidebar'
import { AssistantPanel } from './AssistantPanel'
import type { ChatRequest } from './AssistantPanel'
import { PreferenceSettings } from './PreferenceSettings'
import { ExecutionRecords } from './ExecutionRecords'
import { Shell } from './Shell'
import { AgentTopbar, FactoryTopbar } from './Topbar'
import { BrandMark, MessageStrip } from './ui'
import { appNames } from './copy'
import { SignOut } from './SignOut'
import { useConversations } from './useConversations'
import type { ViewName, ViewState } from './viewState'
import { agentViews, readViewState, viewNames, viewPath, viewUrl } from './viewState'

const SIDEBAR_KEY = 'byof.sidebar'
const BoardView = lazy(() => import('./BoardView').then(module => ({ default: module.BoardView })))
const SimulatorPanel = lazy(() => import('./SimulatorPanel').then(module => ({ default: module.SimulatorPanel })))

const factoryPages = ['facts'] as const
const productFactoryId = 'skf-workshop'
const managerRoles = (roles: string[]) => roles.includes('manager') && roles.includes('planner')
const maintainerRoles = (roles: string[]) => roles.includes('maintainer') && roles.includes('sim_admin')

export function Workbench({ username, userId, onSessionEnded }: { username: string; userId: string | undefined; onSessionEnded: () => void }) {
  const [state, setState] = useState<ViewState>(() => readViewState(window.location.search, window.location.pathname))
  const route = useRef(state)
  const [factories, setFactories] = useState<Factory[] | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)

  const navigate = useCallback((patch: Partial<ViewState>, push = false) => {
    const previous = route.current
    const next = { ...previous, ...patch }
    // A delayed navigation effect must not carry entity IDs from a foreign bookmark.
    if ((previous.factoryId && previous.factoryId !== productFactoryId) || (next.factoryId && next.factoryId !== productFactoryId)) {
      Object.assign(next, { factoryId: productFactoryId, caseId: '', taskId: '', operationId: '', candidateId: '', error: '' })
    }
    route.current = next
    const url = viewUrl(next)
    if (push) window.history.pushState(window.history.state, '', url)
    else window.history.replaceState(window.history.state, '', url)
    setState(next)
  }, [])

  useEffect(() => {
    function onPop() {
      const next = readViewState(window.location.search, window.location.pathname)
      route.current = next
      setState(next)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  useEffect(() => {
    if (!viewNames.some(name => viewPath(name) === window.location.pathname) || (state.view === 'facts' && new URLSearchParams(window.location.search).has('module'))) {
      window.history.replaceState(window.history.state, '', viewUrl(state))
    }
  }, [state])

  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => {
      controller.abort()
      setError('The factory connection timed out; retry.')
    }, 10_000)
    void readFactories(controller.signal).then(result => {
      if (!controller.signal.aborted) { setFactories(result); setError('') }
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return
      if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
      setError(errorMessage(reason, 'Could not connect to the factory; retry.'))
    }).finally(() => window.clearTimeout(timeout))
    return () => { controller.abort(); window.clearTimeout(timeout) }
  }, [attempt, onSessionEnded])

  const factory = factories?.find(item => item.factory_id === productFactoryId)
  const foreignFactory = Boolean(state.factoryId && state.factoryId !== productFactoryId)
  const productState = { ...state, factoryId: productFactoryId, ...(foreignFactory ? { caseId: '', taskId: '', operationId: '', candidateId: '', error: '' } : {}) }
  useEffect(() => {
    if (factory && state.factoryId !== productFactoryId) navigate({
      factoryId: productFactoryId,
      ...(foreignFactory ? { caseId: '', taskId: '', operationId: '', candidateId: '', error: '' } : {}),
    })
  }, [factory, state.factoryId, foreignFactory, navigate])
  if (!factories && !error) return <div className="app-loading" role="status" aria-label="Connecting" />
  if (!factory) return <div className="signin-page"><div className="signin-card">
    <BrandMark /><h1>{appNames.agent}</h1><p className="muted">Signed in · {username}</p>
    {error ? <MessageStrip tone="negative" alert><p>{error}</p><Button variant="outline" type="button" onClick={() => { setError(''); setAttempt(value => value + 1) }}>Retry</Button></MessageStrip> : null}
    {factories && !factory ? <MessageStrip tone="critical"><p>This account has no access to the SKF shop floor. Use an authorized manager or disruption simulator account.</p><Button variant="outline" type="button" onClick={() => { setError(''); setAttempt(value => value + 1) }}>Check access again</Button></MessageStrip> : null}
    <SignOut onSignedOut={onSessionEnded} />
  </div></div>

  const isManager = managerRoles(factory.roles)
  const isMaintainer = maintainerRoles(factory.roles)
  if (!isManager && !isMaintainer) return <div className="signin-page"><div className="signin-card">
    <BrandMark /><h1>Choose a role</h1><p>This workbench is only for the manager and the disruption simulator. Sign in with the matching account.</p><SignOut onSignedOut={onSessionEnded} />
  </div></div>

  return <FactoryWorkspace key={`${userId}:${factory.factory_id}`} factory={factory}
    username={username} userId={userId} state={productState} navigate={navigate}
    isManager={isManager} isMaintainer={isMaintainer} onSessionEnded={onSessionEnded} />
}

function FactoryWorkspace({ factory, userId, state, navigate, isManager, isMaintainer, onSessionEnded }: {
  factory: Factory; username: string; userId: string | undefined
  state: ViewState; navigate: (patch: Partial<ViewState>, push?: boolean) => void
  isManager: boolean; isMaintainer: boolean; onSessionEnded: () => void
}) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [tasks, setTasks] = useState<HumanTask[]>([])
  const [readError, setReadError] = useState('')
  const [taskError, setTaskError] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [chatRequest, setChatRequest] = useState<ChatRequest | null>(null)
  // A plan picked on the timeline; the chat scrolls to its card once.
  const [focusPlan, setFocusPlan] = useState<string | null>(null)
  // A hidden sidebar gives the chat and timeline the full width; remembered in this browser.
  const [sidebarHidden, setSidebarHidden] = useState(() => { try { return localStorage.getItem(SIDEBAR_KEY) === 'hidden' } catch { return false } })
  function showSidebar(show: boolean) {
    setSidebarHidden(!show)
    try { localStorage.setItem(SIDEBAR_KEY, show ? 'shown' : 'hidden') } catch { /* A convenience only. */ }
  }
  const historyCaseId = state.view === 'chat' ? conversationId : null
  const previewCandidateId = state.view === 'board' ? state.candidateId : null
  const active = useRef(false)
  const reading = useRef<AbortController | null>(null)
  const pollTimer = useRef<number | undefined>(undefined)
  const readTimer = useRef<number | undefined>(undefined)
  const runId = workspace?.snapshot?.run_id
  const chats = useConversations(factory.factory_id, userId, isManager ? runId : undefined, onSessionEnded)
  const suggestions = useRiskSuggestions(factory.factory_id, isManager && factory.roles.includes('manager') ? runId : undefined)

  const refresh = useCallback(async function load(requireSuccess = false) {
    window.clearTimeout(pollTimer.current)
    window.clearTimeout(readTimer.current)
    reading.current?.abort()
    const controller = new AbortController()
    reading.current = controller
    readTimer.current = window.setTimeout(() => {
      controller.abort()
      if (active.current) setReadError('Reading the shop floor timed out; reconnecting.')
    }, 10_000)
    try {
      const result = historyCaseId || previewCandidateId
        ? await readWorkspace(factory.factory_id, controller.signal, { caseId: historyCaseId || undefined, candidateId: previewCandidateId || undefined })
        : await readWorkspace(factory.factory_id, controller.signal)
      if (active.current && !controller.signal.aborted) { setWorkspace(result); setReadError('') }
      else if (requireSuccess) throw new Error('The shop floor data did not refresh.')
    } catch (reason: unknown) {
      if (!active.current || controller.signal.aborted) {
        if (requireSuccess) throw reason
        return
      }
      if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
      setReadError(errorMessage(reason, 'Reading the shop floor was interrupted; reconnecting.'))
      if (requireSuccess) throw reason
    } finally {
      if (reading.current === controller) {
        window.clearTimeout(readTimer.current)
        if (active.current) pollTimer.current = window.setTimeout(() => { void load() }, 3000)
      }
    }
  }, [factory.factory_id, onSessionEnded, historyCaseId, previewCandidateId])

  useEffect(() => {
    active.current = true
    void refresh()
    return () => { active.current = false; reading.current?.abort(); window.clearTimeout(pollTimer.current); window.clearTimeout(readTimer.current) }
  }, [refresh])

  useEffect(() => {
    if (!isManager) return
    let stopped = false
    let timer: number | undefined
    let controller = new AbortController()
    async function load() {
      controller = new AbortController()
      const timeout = window.setTimeout(() => controller.abort(), 10_000)
      try {
        const result = await readHumanTasks(factory.factory_id, controller.signal)
        if (!stopped && !controller.signal.aborted) { setTasks(result); setTaskError('') }
      } catch (reason: unknown) {
        if (!stopped && !controller.signal.aborted) {
          setTaskError(errorMessage(reason, 'Reading pending information requests was interrupted; reconnecting.'))
          if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
        }
      } finally {
        window.clearTimeout(timeout)
        if (!stopped) timer = window.setTimeout(() => { void load() }, 5000)
      }
    }
    void load()
    return () => { stopped = true; controller.abort(); window.clearTimeout(timer) }
  }, [factory.factory_id, isManager, onSessionEnded])

  const allowed = isManager ? [...agentViews] : [...factoryPages]
  const view = allowed.includes(state.view) ? state.view : isManager ? 'chat' : 'facts'
  useEffect(() => { if (state.view !== view) navigate({ view }, false) }, [state.view, view, navigate])
  const snapshot = workspace?.snapshot ?? null
  const freshness = workspace?.freshness ?? 'UNKNOWN'
  const { select } = chats
  const onView = useCallback((next: ViewName) => navigate({ view: next }, true), [navigate])
  const startSuggestion = useCallback((item: RiskSuggestion) => {
    select('new')
    setChatRequest({ key: crypto.randomUUID(), message: item.prompt, suggestionId: item.suggestion_id })
    navigate({ view: 'chat' }, true)
  }, [select, navigate])
  const notices = <>
    {readError ? <MessageStrip tone="critical"><p>{readError}</p></MessageStrip> : null}
    {taskError && view === 'chat' ? <MessageStrip tone="critical"><p>{taskError}</p></MessageStrip> : null}
  </>

  if (!isManager) return <Shell surface="factory" topbar={<FactoryTopbar snapshot={snapshot} freshness={freshness} />}>
    <div className="view factory-view">
      {notices}
      {isMaintainer ? <Suspense fallback={<ViewSkeleton />}><SimulatorPanel factoryId={factory.factory_id} userId={userId} snapshot={snapshot} finishedGoods={workspace?.finished_goods} onChanged={() => refresh(true)} onSessionEnded={onSessionEnded} /></Suspense> : null}
    </div>
  </Shell>

  return <Shell surface="agent"
    sidebar={sidebarHidden ? undefined : <AgentSidebar conversations={chats.conversations} selection={chats.selection} view={view} onSelect={select} onView={onView}
      onHide={() => showSidebar(false)} settings={<PreferenceSettings factoryId={factory.factory_id} runId={runId} />} />}
    topbar={<AgentTopbar snapshot={snapshot} freshness={freshness} suggestions={suggestions} onSuggestion={startSuggestion}
      onShowSidebar={sidebarHidden ? () => showSidebar(true) : undefined} />}>
    <div className={`view agent-view view-${view}`}>
      {notices}
      {view === 'chat' ? <AssistantPanel factory={factory} userId={userId} workspace={workspace} tasks={tasks}
        selection={chats.selection} onSelect={select} request={chatRequest} onRequestHandled={() => setChatRequest(null)}
        onConversation={setConversationId} onSessionEnded={onSessionEnded} onChanged={refresh} onPreview={candidateId => navigate({ view: 'board', candidateId }, true)}
        focusPlan={focusPlan} onFocused={() => setFocusPlan(null)} /> : null}
      {view === 'board' ? <Suspense fallback={<ViewSkeleton />}><BoardView workspace={workspace} state={state} navigate={navigate} tasks={tasks}
        conversations={chats.conversations} onOpenConversation={(caseId, candidateId) => { select(caseId); setFocusPlan(candidateId); navigate({ view: 'chat' }, true) }} /></Suspense> : null}
      {view === 'records' ? <ExecutionRecords factoryId={factory.factory_id} snapshot={snapshot} publications={workspace?.publications ?? []} /> : null}
    </div>
  </Shell>
}

function ViewSkeleton() {
  return <div className="view-skeleton" role="status" aria-label="Loading"><span /><span /><span /></div>
}

function useRiskSuggestions(factoryId: string, runId: string | undefined): RiskSuggestion[] {
  const [items, setItems] = useState<RiskSuggestion[]>([])
  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | undefined
    async function load() {
      controller = new AbortController()
      const timeout = window.setTimeout(() => controller?.abort(), 10_000)
      try {
        const result = await readRiskSuggestions(factoryId, runId, controller.signal)
        if (!stopped) setItems(result.freshness === 'CURRENT' ? result.suggestions : [])
      } catch { /* Suggestions are optional; the chat stays usable without them. */ }
      finally {
        window.clearTimeout(timeout)
        if (!stopped) timer = window.setTimeout(() => { void load() }, 5000)
      }
    }
    void load()
    return () => { stopped = true; controller?.abort(); window.clearTimeout(timer) }
  }, [factoryId, runId])
  return runId ? items : []
}
