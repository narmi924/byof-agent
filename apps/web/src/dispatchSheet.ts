import type { Assignment } from './contracts'
import type { OperationIdentity } from './scheduleModel'
import { clockOf } from './dayWindow'

const cell = (value: string | number) => {
  const text = String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/** Shop-floor dispatch list for one factory day, ordered by machine and start time. */
export function dispatchCsv(assignments: Assignment[], index: Map<string, OperationIdentity>, startMs: number, endMs: number, zone: string): string {
  const rows = assignments
    .map(item => ({ item, start: Date.parse(item.resume_at ?? item.start_at), end: Date.parse(item.end_at) }))
    .filter(row => row.start < endMs && row.end > startMs)
    .sort((a, b) => a.item.resource_id.localeCompare(b.item.resource_id) || a.start - b.start)
  const lines = [['Machine', 'Start', 'End', 'Worker', 'Order', 'Batch', 'Operation', 'Quantity'].join(',')]
  for (const { item, start, end } of rows) {
    const identity = index.get(item.operation_id)
    lines.push([
      item.resource_id, clockOf(start, zone), clockOf(end, zone), item.worker_id,
      identity?.orderId ?? '', identity?.batchId ?? '', identity ? `${identity.operationCode} ${identity.stepName}` : item.operation_id, identity?.quantity ?? '',
    ].map(cell).join(','))
  }
  return '﻿' + lines.join('\r\n') + '\r\n'
}

export function downloadText(content: string, filename: string, type = 'text/csv;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement('a')
  link.href = url; link.download = filename
  document.body.appendChild(link)
  try { link.click() } finally { link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 10_000) }
}
