/** Calendar days in the factory time zone. The backend always uses UTC timestamps and the board splits days
 *  in the factory time zone, so these conversions handle offsets and DST themselves instead of using the browser zone. */

export interface Civil { year: number; month: number; day: number; hour: number; minute: number; second: number }
export interface DayBounds { key: string; startMs: number; endMs: number }

const parts = new Map<string, Intl.DateTimeFormat>()
const labels = new Map<string, Intl.DateTimeFormat>()
const clocks = new Map<string, Intl.DateTimeFormat>()

function cached(store: Map<string, Intl.DateTimeFormat>, timeZone: string, build: () => Intl.DateTimeFormat) {
  const found = store.get(timeZone)
  if (found) return found
  const created = build()
  store.set(timeZone, created)
  return created
}

/** An unrecognized time zone falls back to UTC and the caller shows "time zone not provided"; never the browser zone. */
export function safeZone(timeZone: string | undefined | null): string {
  if (!timeZone) return 'UTC'
  try {
    new Intl.DateTimeFormat('en-CA', { timeZone })
    return timeZone
  } catch {
    return 'UTC'
  }
}

export function civilOf(ms: number, timeZone: string): Civil {
  const format = cached(parts, timeZone, () => new Intl.DateTimeFormat('en-CA', {
    timeZone, hourCycle: 'h23',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }))
  const found: Record<string, number> = {}
  for (const part of format.formatToParts(ms)) if (part.type !== 'literal') found[part.type] = Number(part.value)
  return {
    year: found.year ?? 1970, month: found.month ?? 1, day: found.day ?? 1,
    hour: (found.hour ?? 0) % 24, minute: found.minute ?? 0, second: found.second ?? 0,
  }
}

/** Offset of this instant in this time zone, in milliseconds (east is positive). */
export function offsetOf(ms: number, timeZone: string): number {
  const civil = civilOf(ms, timeZone)
  const asUtc = Date.UTC(civil.year, civil.month - 1, civil.day, civil.hour, civil.minute, civil.second)
  return asUtc - Math.floor(ms / 1000) * 1000
}

export const dayKeyOf = (ms: number, timeZone: string): string => {
  const civil = civilOf(ms, timeZone)
  return `${String(civil.year).padStart(4, '0')}-${String(civil.month).padStart(2, '0')}-${String(civil.day).padStart(2, '0')}`
}

const KEY = /^(\d{4})-(\d{2})-(\d{2})$/
export const isDayKey = (value: string): boolean => {
  const match = KEY.exec(value)
  if (!match) return false
  const [, year, month, day] = match
  const stamp = Date.UTC(Number(year), Number(month) - 1, Number(day))
  return Number.isFinite(stamp) && dayKeyOf(stamp, 'UTC') === value
}

/** Converts a factory-zone day and time to UTC milliseconds. The offset is taken twice to cover DST change days. */
export function zonedMs(key: string, timeZone: string, hour = 0, minute = 0): number {
  const match = KEY.exec(key)
  if (!match) return Number.NaN
  const [, year, month, day] = match
  const naive = Date.UTC(Number(year), Number(month) - 1, Number(day), hour, minute)
  const first = naive - offsetOf(naive, timeZone)
  return naive - offsetOf(first, timeZone)
}

/** Converts factory wall time to UTC; times that DST makes non-existent or ambiguous must be chosen again by the administrator. */
export function factoryLocalToUtc(value: string, timeZone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) throw new Error('Enter a valid factory local date and time.')
  const [, year, month, day, hour, minute] = match
  const naive = Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute))
  if (!Number.isFinite(naive)) throw new Error('Enter a valid factory local date and time.')
  const offsets = new Set([offsetOf(naive - 86_400_000, timeZone), offsetOf(naive, timeZone), offsetOf(naive + 86_400_000, timeZone)])
  const candidates = [...offsets].map(offset => naive - offset).filter(ms => {
    const civil = civilOf(ms, timeZone)
    return civil.year === Number(year) && civil.month === Number(month) && civil.day === Number(day)
      && civil.hour === Number(hour) && civil.minute === Number(minute)
  })
  if (candidates.length !== 1) throw new Error('That factory local time does not exist or is ambiguous; choose another time.')
  return new Date(candidates[0]!).toISOString()
}

export function shiftDayKey(key: string, days: number): string {
  const match = KEY.exec(key)
  if (!match) return key
  const [, year, month, day] = match
  return dayKeyOf(Date.UTC(Number(year), Number(month) - 1, Number(day) + days), 'UTC')
}

export function dayBounds(key: string, timeZone: string): DayBounds {
  return { key, startMs: zonedMs(key, timeZone), endMs: zonedMs(shiftDayKey(key, 1), timeZone) }
}

/** Calendar days covering [startMs, endMs]. A cap keeps a bad range from freezing the page. */
export function dayKeysIn(startMs: number, endMs: number, timeZone: string, limit = 120): string[] {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return []
  const last = dayKeyOf(endMs, timeZone)
  const keys: string[] = []
  let key = dayKeyOf(startMs, timeZone)
  while (keys.length < limit) {
    keys.push(key)
    if (key === last) break
    key = shiftDayKey(key, 1)
  }
  return keys
}

const weekdays = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function dayLabel(key: string): string {
  const match = KEY.exec(key)
  if (!match) return 'Date not confirmed'
  const [, , month, day] = match
  return `${months[Number(month) - 1] ?? month} ${Number(day)}`
}

export function weekdayLabel(key: string, timeZone: string): string {
  const noon = zonedMs(key, timeZone, 12)
  if (!Number.isFinite(noon)) return ''
  return weekdays[new Date(noon + offsetOf(noon, timeZone)).getUTCDay()] ?? ''
}

export function clockOf(ms: number, timeZone: string): string {
  const format = cached(clocks, timeZone, () => new Intl.DateTimeFormat('en-GB', {
    timeZone, hourCycle: 'h23', hour: '2-digit', minute: '2-digit',
  }))
  return format.format(ms)
}

export function dateClockOf(ms: number, timeZone: string): string {
  const format = cached(labels, timeZone, () => new Intl.DateTimeFormat('en-CA', {
    timeZone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }))
  return format.format(ms).replace(',', '')
}

/** Such as UTC+8 or UTC+05:30, so the factory time zone is labeled and nobody has to convert it. */
export function zoneOffsetLabel(ms: number, timeZone: string): string {
  const minutes = Math.round(offsetOf(ms, timeZone) / 60000)
  if (minutes === 0) return 'UTC'
  const sign = minutes < 0 ? '-' : '+'
  const hours = Math.floor(Math.abs(minutes) / 60)
  const rest = Math.abs(minutes) % 60
  return rest ? `UTC${sign}${String(hours).padStart(2, '0')}:${String(rest).padStart(2, '0')}` : `UTC${sign}${hours}`
}

export const parseMs = (value: string | null | undefined): number => {
  if (!value) return Number.NaN
  return Date.parse(value)
}
