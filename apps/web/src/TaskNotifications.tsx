import { Button } from './components/ui/button'
import { Card } from './components/ui/card'
import { useCallback, useState } from 'react'
import { readNotifications } from './api'
import { notificationKindText, notificationStateText } from './notificationContracts'
import { dateTime } from './presentation'
import { usePolling } from './usePolling'

export function TaskNotifications(props: { factoryId: string; taskId: string; admin: boolean; onSessionEnded: () => void }) {
  const [open, setOpen] = useState(false)
  return <details className="detail-block" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>Notification log</summary>{open ? <NotificationHistory {...props} /> : null}</details>
}
function NotificationHistory({ factoryId, taskId, admin, onSessionEnded }: { factoryId: string; taskId: string; admin: boolean; onSessionEnded: () => void }) {
  const load = useCallback((signal: AbortSignal) => readNotifications(factoryId, taskId, signal), [factoryId, taskId])
  const { data, error, refresh } = usePolling(load, onSessionEnded)
  return <div className="notification-history">
    <Button variant="outline" type="button" className="secondary" onClick={() => { void refresh() }}>Refresh log</Button>
    {error ? <p className="notice" role="alert">{error}</p> : null}
    {!data && !error ? <p role="status">Loading the notification log.</p> : null}
    {data?.length === 0 ? <p className="muted">No notifications sent yet. Tasks can be handled directly in the workbench.</p> : null}
    {data?.map((item) => <Card asChild><article key={item.notification_id} className="notification-record"><strong>{notificationKindText(item.kind)}</strong><p>{notificationStateText(item.send_state, item.error_code)}</p><p className="muted">Delivery status not reported · {item.attempts} attempt(s) · updated {dateTime(item.updated_at)} UTC</p>{admin ? <details><summary>Notification details</summary><dl className="evidence-list"><div><dt>Message ID</dt><dd>{item.message_id}</dd></div><div><dt>Task version</dt><dd>{item.task_version}</dd></div><div><dt>Created</dt><dd>{dateTime(item.created_at)} UTC</dd></div>{item.error_code ? <div><dt>Error code</dt><dd>{item.error_code}</dd></div> : null}</dl></details> : null}</article></Card>)}
  </div>
}
