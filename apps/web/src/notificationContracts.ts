import type { TaskRole } from './caseContracts'
import { taskRoles } from './caseContracts'

export interface NotificationContact { role: TaskRole; user_id: string; username: string; email: string; version: number; enabled: boolean }
export interface ContactSettings { contacts: NotificationContact[]; eligible_users: { user_id: string; username: string; roles: string[] }[]; channel_state: 'NOT_ENABLED' | 'CAPTURE' | 'TLS' }
export interface ContactInput { request_id: string; role: TaskRole; user_id: string; email: string; enabled: boolean; expected_version: number }
export interface NotificationRecord { notification_id: string; task_id: string; task_version: number; kind: string; send_state: string; delivery_state: 'UNAVAILABLE'; created_at: string; updated_at: string; error_code: string | null; message_id: string; attempts: number }
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const timestamp = (value: unknown) => nonempty(value) && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
export function normalizeContactEmail(value: string): string | null {
  const email = value.trim()
  if (email.length > 254 || !/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)+$/.test(email)) return null
  const [local, domain] = email.split('@')
  if (!local || !domain || local.length > 64 || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return null
  return `${local}@${domain.toLowerCase()}`
}
export function parseContact(value: unknown): NotificationContact {
  if (!object(value) || !taskRoles.some((role) => role === value.role) || !['user_id', 'email'].every((key) => nonempty(value[key])) || typeof value.username !== 'string' || !integer(value.version) || value.version < 1 || typeof value.enabled !== 'boolean') throw new Error('The notification contact data is incomplete; refresh to check.')
  return value as unknown as NotificationContact
}
export function parseContacts(value: unknown): ContactSettings {
  if (!object(value) || !Array.isArray(value.contacts) || !Array.isArray(value.eligible_users) || !['NOT_ENABLED', 'CAPTURE', 'TLS'].includes(String(value.channel_state)) || !value.eligible_users.every((user) => object(user) && nonempty(user.user_id) && nonempty(user.username) && Array.isArray(user.roles) && user.roles.every(nonempty))) throw new Error('The notification settings are not recognized; refresh to check.')
  value.contacts.forEach(parseContact)
  return value as unknown as ContactSettings
}
export function parseNotifications(value: unknown, taskId: string): NotificationRecord[] {
  if (!object(value) || !Array.isArray(value.notifications) || !value.notifications.every((item) => object(item) && item.task_id === taskId && ['notification_id', 'kind', 'send_state', 'message_id'].every((key) => nonempty(item[key])) && integer(item.task_version) && item.task_version >= 1 && integer(item.attempts) && timestamp(item.created_at) && timestamp(item.updated_at) && item.delivery_state === 'UNAVAILABLE' && (item.error_code === null || nonempty(item.error_code)))) throw new Error('The notification log does not match the current task or is incomplete; refresh to check.')
  return value.notifications as NotificationRecord[]
}
const sendLabels: Record<string, string> = { NOT_ENABLED: 'Email is not enabled; handle it in the workbench.', NOT_CONFIGURED: 'No notification contact is configured; handle it in the workbench.', QUEUED: 'Notification waiting to be sent.', SENDING: 'Submitting the email; waiting for the result.', PROVIDER_ACCEPTED: 'Accepted by the mail service; delivery is checked separately.', FAILED: 'Sending the notification failed; handle it in the workbench.', UNKNOWN: 'The sending result is not confirmed; handle it in the workbench.', CANCELLED: 'This notification was cancelled.' }
const errorLabels: Record<string, string> = {
  RECIPIENT_NOT_ALLOWED: 'Email to this contact is not authorized; ask the administrator to check the recipient scope.',
  REAL_EMAIL_NOT_AUTHORIZED: 'Real email is not authorized; handle it in the workbench.',
  INVALID_REAL_EMAIL_ALLOWLIST: 'The allowed recipient configuration is wrong; ask the administrator to check.',
  SMTP_AUTHENTICATION_FAILED: 'The sender account failed authentication; ask the administrator to check the mail credentials.',
  SMTP_TLS_PORT_MISMATCH: 'The mail encryption does not match the port; ask the administrator to check.',
  SMTP_CREDENTIALS_REQUIRED: 'The sender account is not fully configured; ask the administrator to check.',
}
export const notificationStateText = (state: string, errorCode?: string | null) => state === 'FAILED' && errorCode && Object.hasOwn(errorLabels, errorCode) ? errorLabels[errorCode]! : Object.hasOwn(sendLabels, state) ? sendLabels[state]! : 'The notification result is not confirmed; handle it in the workbench.'
const kindLabels: Record<string, string> = { INITIAL: 'New task notification', REMINDER: 'Task reminder', TRANSFER: 'Task transfer notification', ESCALATION: 'Overdue escalation notification' }
export const notificationKindText = (kind: string) => Object.hasOwn(kindLabels, kind) ? kindLabels[kind]! : 'Task notification'
