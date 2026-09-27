export function readText(data: FormData, name: string): string { return String(data.get(name) ?? '').trim() }

export function identifier(data: FormData, name: string): string {
  const value = readText(data, name)
  if (!/^[^\s]{1,160}$/.test(value)) throw new Error('IDs and source references cannot be empty or contain spaces, and have at most 160 characters.')
  return value
}

export function whole(data: FormData, name: string): number {
  const raw = readText(data, name), value = Number(raw)
  if (!raw || !Number.isSafeInteger(value) || value < 1) throw new Error('Enter a positive whole number.')
  return value
}

export function localTime(value: string | undefined, zone: string): string {
  return value ? new Intl.DateTimeFormat('sv-SE', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value)).replace(' ', 'T') : ''
}
