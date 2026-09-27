import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { Textarea } from './components/ui/textarea'
import { Card } from './components/ui/card'
import type { FormEvent } from 'react'
import { useEffect, useRef } from 'react'
import type { CaseRecord, HumanTask, TaskField, TaskRole } from './caseContracts'
import { taskRoles } from './caseContracts'
import { dateTime } from './presentation'
import { notificationStateText } from './notificationContracts'
import { TaskNotifications } from './TaskNotifications'

const roleNames: Record<TaskRole, string> = { maintainer: 'Maintenance lead', warehouse: 'Warehouse lead', team_lead: 'Team lead', planner: 'Planner', manager: 'Manager' }
const fieldNames: Record<TaskField, string> = { repair_eta: 'Expected recovery time (UTC)', receipt_eta: 'Expected arrival time (UTC)', remaining_minutes: 'Remaining production minutes', remaining_setup_minutes: 'Remaining changeover minutes', comment: 'Note' }
const taskStates: Record<string, string> = { OPEN: 'Open', ESCALATED: 'Escalated, open', RESPONDED: 'Answered, awaiting case check', CANCELLED: 'Cancelled', REVIEWED: 'Review closed', ACCEPTED: 'Explicitly taken over' }
const reviewOutcomes = { PENDING: 'Waiting for the responsible role', APPROVED: 'Required approvals recorded; release conditions being checked', REJECTED: 'Plan rejected', STALE: 'Plan basis is out of date; review closed', HANDED_OFF: 'Taken over by an owner; review closed' }
export type TaskAction = 'responses' | 'transfers' | 'cancellations' | 'handoffs'

export function HumanTasks({ tasks, cases, roles, userId, disabled, readOnly, onAction, onError, onSessionEnded, linkedTaskId, onReviewCandidate }: {
  tasks: HumanTask[]; cases: CaseRecord[]; roles: string[]; userId: string | undefined; disabled: boolean; readOnly: boolean
  onAction: (task: HumanTask, action: TaskAction, body: Record<string, unknown>) => void; onError: (message: string) => void
  onSessionEnded: () => void; linkedTaskId: string
  onReviewCandidate?: ((candidateId: string) => void) | undefined
}) {
  const linked = useRef<HTMLElement | null>(null)
  const focused = useRef('')
  useEffect(() => { if (linkedTaskId && linked.current && focused.current !== linkedTaskId) { linked.current.focus(); focused.current = linkedTaskId } }, [linkedTaskId, tasks])
  return <section className="production-panel human-tasks" aria-labelledby="tasks-title"><h2 id="tasks-title">Tasks and information requests</h2>
    {tasks.length === 0 ? <p className="muted">No information tasks are assigned to you.</p> : null}
    {tasks.map((task) => {
      const assigned = roles.includes(task.owner_role) && (task.owner_id === null || task.owner_id === userId)
      const canManage = assigned || roles.includes('manager') || Boolean(userId && cases.some((record) => record.case_id === task.case_id && record.owner_id === userId))
      const active = ['OPEN', 'ESCALATED'].includes(task.state)
      const editable = active && !readOnly
      const information = task.task_type === undefined || task.task_type === 'INFORMATION'
      const handoff = task.task_type === 'HANDOFF' && assigned && ['planner', 'manager'].includes(task.owner_role)
      const response = task.response
      function submit(event: FormEvent<HTMLFormElement>, action: TaskAction) {
        event.preventDefault()
        if (disabled || !editable || (action === 'responses' ? !assigned || !information : action === 'handoffs' ? !handoff : !canManage)) return
        const data = new FormData(event.currentTarget)
        if (action === 'handoffs') {
          const responsibility = String(data.get('responsibility_summary') ?? '').trim(), risk = String(data.get('risk_summary') ?? '').trim()
          if (!responsibility || !risk || responsibility.length > 2000 || risk.length > 2000) { onError('Describe the responsibility you take over and the remaining risks, up to 2000 characters each.'); return }
          if (data.get('accept_responsibility') !== 'on' || data.get('accept_risks') !== 'on') { onError('Confirm both the responsibility and the follow-up of remaining risks.'); return }
          if (!task.case_version || !task.snapshot_hash) { onError('The case version or factory facts are not confirmed; refresh before taking over.'); return }
          onAction(task, action, { expected_case_version: task.case_version, expected_snapshot_hash: task.snapshot_hash, accept_responsibility: true, accept_risks: true, responsibility_summary: responsibility, risk_summary: risk })
        } else if (action === 'responses') {
          const answer: Record<string, string | number> = {}
          for (const field of task.fields) {
            const value = String(data.get(field) ?? '').trim()
            if (!value) { onError(`Enter ${fieldNames[field]}; unknown information cannot be treated as zero.`); return }
            if (field.endsWith('_minutes')) {
              const minutes = Number(value)
              if (!Number.isSafeInteger(minutes) || minutes < 0) { onError('Remaining minutes must be a non-negative whole number.'); return }
              answer[field] = minutes
            } else if (field.endsWith('_eta')) {
              if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) || !Number.isFinite(Date.parse(`${value}Z`))) { onError('Enter a valid UTC date and time.'); return }
              const time = new Date(`${value}Z`).toISOString()
              if (!time.startsWith(value)) { onError('That date does not exist; check it again.'); return }
              answer[field] = time
            } else answer[field] = value
          }
          onAction(task, action, { answer })
        } else {
          const reason = String(data.get('reason') ?? '').trim()
          if (!reason) { onError('Enter a reason.'); return }
          onAction(task, action, { reason, ...(action === 'transfers' ? { target_role: data.get('role'), target_owner_id: null } : {}) })
        }
      }
      return <Card asChild><article className={`human-task${task.task_id === linkedTaskId ? ' linked-task' : ''}`} key={`${task.task_id}:${task.version}`} tabIndex={task.task_id === linkedTaskId ? -1 : undefined} ref={task.task_id === linkedTaskId ? linked : undefined} aria-label={task.question}>
        <div className="section-heading"><h3>{task.question}</h3><span className="status-badge">{taskStates[task.state] ?? 'Status not confirmed'}</span></div>
        <p className="muted">{roleNames[task.owner_role]}{task.owner_id ? task.owner_id === userId ? ' · assigned to me' : ' · owner assigned' : ' · role task'}{information ? ` · subject ${task.subject_id}` : ''} · reply by {dateTime(task.due_at)} UTC</p>
        <p className="muted">{notificationStateText(task.send_state)}</p>
        <TaskNotifications factoryId={task.factory_id} taskId={task.task_id} admin={roles.includes('admin')} onSessionEnded={onSessionEnded} />
        {response && 'source' in response && response.source === 'authenticated_human_information' ? <div className="notice"><p>The owner answered while signed in; the information still has to be checked against factory facts.</p><dl className="evidence-list">{Object.entries(response.answer).map(([field, value]) => <div key={field}><dt>{fieldNames[field as TaskField] ?? 'Answer'}</dt><dd>{field.endsWith('_eta') ? dateTime(String(value)) : String(value)}</dd></div>)}</dl></div> : null}
        {response && 'source' in response && response.source === 'authenticated_human_handoff' ? <div className="notice"><p>{response.actor_id} explicitly took over at {dateTime(response.received_at)} UTC.</p><dl className="evidence-list"><div><dt>Responsibility taken over</dt><dd>{response.responsibility_summary}</dd></div><div><dt>Follow-up of risks</dt><dd>{response.risk_summary}</dd></div></dl><p>The takeover record keeps the follow-up responsibility; it does not mean production is complete or the risk is gone.</p></div> : null}
        {task.task_type === 'APPROVAL' && task.review ? <div className="notice"><p>{reviewOutcomes[task.review.outcome]}</p><p>This plan needs {task.review.required_scopes.map((scope) => scope === 'publish_plan' ? 'planner approval of the plan' : 'manager approval of overtime').join(' and ')}.</p>{onReviewCandidate ? <Button variant="outline" type="button" className="secondary" onClick={() => onReviewCandidate(task.review!.candidate_id)}>View this plan</Button> : null}<p className="muted">Check the facts on the plan page and make an explicit decision. A reply is not an approval.</p></div> : null}
        {readOnly && active ? <p className="muted">The current factory data does not allow formal handling; tasks are view-only.</p> : null}
        {assigned && editable && information ? <form aria-label={`Reply: ${task.question}`} className="task-form" onSubmit={(event) => submit(event, 'responses')}><fieldset disabled={disabled}><legend>Provide the requested information</legend>
          {task.fields.map((field) => <label key={field}>{fieldNames[field]}{field === 'comment' ? <Textarea name={field} rows={3} maxLength={4000} required /> : <Input name={field} type={field.endsWith('_eta') ? 'datetime-local' : 'number'} {...(field.endsWith('_eta') ? { step: 60 } : { min: 0, step: 1 })} required />}</label>)}
          <Button variant="outline" type="submit">Submit reply</Button></fieldset></form> : null}
        {handoff && editable ? <form aria-label={`Take over: ${task.question}`} className="task-form" onSubmit={(event) => submit(event, 'handoffs')}><fieldset disabled={disabled || !task.case_version || !task.snapshot_hash}><legend>Take over this case</legend><p>After checking the current production facts and case record, describe the responsibility and remaining risks you take on. The case is then yours.</p><label>Responsibility taken over<Textarea name="responsibility_summary" maxLength={2000} rows={3} required /></label><label>Remaining risks and follow-up<Textarea name="risk_summary" maxLength={2000} rows={3} required /></label><div key={`${task.case_version}:${task.snapshot_hash}`}><label className="checkbox-label"><Input type="checkbox" name="accept_responsibility" required />I take over the responsibility above</label><label className="checkbox-label"><Input type="checkbox" name="accept_risks" required />I acknowledge the remaining risks and own their follow-up</label></div><Button variant="outline" type="submit">Confirm takeover</Button></fieldset>{!task.case_version || !task.snapshot_hash ? <p className="notice">The case version or factory facts are not confirmed; refresh.</p> : null}</form> : null}
        {canManage && editable ? <details className="detail-block"><summary>Transfer or cancel this task</summary><div className="task-management">
          <form aria-label={`Transfer: ${task.question}`} className="task-form" onSubmit={(event) => submit(event, 'transfers')}><fieldset disabled={disabled}><legend>Transfer to another role</legend><label>Receiving role<select name="role" defaultValue={task.owner_role}>{taskRoles.filter((role) => information || ['planner', 'manager'].includes(role)).map((role) => <option value={role} key={role}>{roleNames[role]}</option>)}</select></label><label>Transfer reason<Textarea name="reason" maxLength={500} rows={2} required /></label><Button variant="outline" type="submit" className="secondary">Confirm transfer</Button></fieldset></form>
          <form aria-label={`Cancel: ${task.question}`} className="task-form" onSubmit={(event) => submit(event, 'cancellations')}><fieldset disabled={disabled}><legend>Cancel an invalid task</legend><label>Cancellation reason<Textarea name="reason" maxLength={500} rows={2} required /></label><Button variant="outline" type="submit" className="secondary">Confirm cancellation</Button></fieldset></form>
        </div></details> : null}
      </article></Card>
    })}
  </section>
}
