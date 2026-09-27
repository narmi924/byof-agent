import { describe, expect, it } from 'vitest'
import {
  clockOf, dayBounds, dayKeyOf, dayKeysIn, dayLabel, factoryLocalToUtc, isDayKey, offsetOf,
  safeZone, shiftDayKey, weekdayLabel, zonedMs, zoneOffsetLabel,
} from './dayWindow'

const zone = 'Asia/Singapore'

describe('calendar days in the factory time zone', () => {
  it('splits days by the factory time zone, not the browser zone', () => {
    // Local 2026-09-15 00:30 is UTC 2026-09-14 16:30.
    expect(dayKeyOf(Date.parse('2026-09-14T16:30:00Z'), zone)).toBe('2026-09-15')
    expect(dayKeyOf(Date.parse('2026-09-14T15:30:00Z'), zone)).toBe('2026-09-14')
  })

  it('maps day boundaries back to the start of the same day and the next day', () => {
    const bounds = dayBounds('2026-09-14', zone)
    expect(new Date(bounds.startMs).toISOString()).toBe('2026-09-13T16:00:00.000Z')
    expect(new Date(bounds.endMs).toISOString()).toBe('2026-09-14T16:00:00.000Z')
    expect(bounds.endMs - bounds.startMs).toBe(86_400_000)
  })

  it('the length of the DST start day follows the real offset', () => {
    // US Eastern 2026-03-08 has only 23 hours.
    const bounds = dayBounds('2026-03-08', 'America/New_York')
    expect(bounds.endMs - bounds.startMs).toBe(23 * 3_600_000)
  })

  it('the DST end day keeps 25 hours', () => {
    const bounds = dayBounds('2026-11-01', 'America/New_York')
    expect(bounds.endMs - bounds.startMs).toBe(25 * 3_600_000)
  })

  it('converts factory time entered by the administrator to UTC and rejects ambiguous or missing times', () => {
    expect(factoryLocalToUtc('2026-09-14T11:15', zone)).toBe('2026-09-14T03:15:00.000Z')
    expect(() => factoryLocalToUtc('2026-02-30T11:15', zone)).toThrow()
    expect(() => factoryLocalToUtc('2026-03-08T02:30', 'America/New_York')).toThrow()
    expect(() => factoryLocalToUtc('2026-11-01T01:30', 'America/New_York')).toThrow()
  })

  it('computes offset and zone labels per instant', () => {
    expect(offsetOf(Date.parse('2026-09-14T01:00:00Z'), zone)).toBe(8 * 3_600_000)
    expect(zoneOffsetLabel(Date.parse('2026-09-14T01:00:00Z'), zone)).toBe('UTC+8')
    expect(zoneOffsetLabel(Date.parse('2026-09-14T01:00:00Z'), 'Asia/Kolkata')).toBe('UTC+05:30')
    expect(zoneOffsetLabel(Date.parse('2026-09-14T01:00:00Z'), 'UTC')).toBe('UTC')
  })

  it('covers the scheduling window with a sequence of days', () => {
    const keys = dayKeysIn(Date.parse('2026-09-14T00:30:00Z'), Date.parse('2026-09-16T09:30:00Z'), zone)
    expect(keys).toEqual(['2026-09-14', '2026-09-15', '2026-09-16'])
  })

  it('an invalid range does not produce an endless sequence', () => {
    expect(dayKeysIn(Date.parse('2026-09-16T00:00:00Z'), Date.parse('2026-09-14T00:00:00Z'), zone)).toEqual([])
    expect(dayKeysIn(Number.NaN, 0, zone)).toEqual([])
    expect(dayKeysIn(0, Date.parse('2030-01-01T00:00:00Z'), zone, 5)).toHaveLength(5)
  })

  it('validates and shifts day keys', () => {
    expect(isDayKey('2026-09-14')).toBe(true)
    expect(isDayKey('2026-02-30')).toBe(false)
    expect(isDayKey('2026-9-14')).toBe(false)
    expect(shiftDayKey('2026-09-30', 1)).toBe('2026-10-01')
    expect(shiftDayKey('2026-01-01', -1)).toBe('2025-12-31')
  })

  it('shows the date and weekday', () => {
    expect(dayLabel('2026-09-14')).toBe('Sep 14')
    expect(weekdayLabel('2026-09-14', zone)).toBe('Mon')
    expect(clockOf(Date.parse('2026-09-14T01:12:00Z'), zone)).toBe('09:12')
  })

  it('an unrecognized time zone falls back to UTC, not the browser zone', () => {
    expect(safeZone('Mars/Olympus')).toBe('UTC')
    expect(safeZone(undefined)).toBe('UTC')
    expect(safeZone(zone)).toBe(zone)
  })

  it('midnight conversion does not skip a day because of the 24:00 notation', () => {
    expect(clockOf(dayBounds('2026-09-14', zone).startMs, zone)).toBe('00:00')
    expect(zonedMs('2026-09-14', zone, 0, 0)).toBe(dayBounds('2026-09-14', zone).startMs)
  })
})
