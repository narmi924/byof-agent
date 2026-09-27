import fixtureDocument from './test/businessSourceFixture.json?raw'
import { describe, expect, it } from 'vitest'
import { parseWorkspace } from './contracts'

// Actual Python source/solver serialization, with identical profile/calendars stored once.
// It includes all three business_scenarios(), a source-confirmed all-cancelled snapshot,
// its checked empty candidate, and the unchanged small SKF snapshot/ordinary candidate.
const fixture = JSON.parse(fixtureDocument) as {
  common: { profile: Record<string, unknown>; resources: unknown[]; workers: unknown[] }
  snapshots: Record<string, unknown>[]; empty_candidate: Record<string, unknown>
  legacy_snapshot: Record<string, unknown>; legacy_candidate: Record<string, unknown>
}
function source(document: Record<string, unknown>): Record<string, unknown> {
  return structuredClone({ ...fixture.common, ...document, profile: { ...fixture.common.profile, factory_id: document.factory_id } })
}
function workspace(snapshot: Record<string, unknown>, candidate?: Record<string, unknown>) {
  return { snapshot, last_synced_at: snapshot.snapshot_clock, jobs: [], candidates: candidate ? [{ candidate, snapshot_id: snapshot.snapshot_id, run_id: snapshot.run_id, state: 'CANDIDATE', approvals: [] }] : [], publications: [] }
}

describe('frontend contract of real source serialization', () => {
  it('reads qualified surplus goods at the workspace top level without writing them into the source snapshot or changing its hash', () => {
    const snapshot = source(fixture.snapshots[3]!), before = JSON.stringify(snapshot)
    const goods = [{ batch_id: 'SO-001-R001-B001', product_id: 'BRG-6202', quantity: 50, completed_at: String(snapshot.snapshot_clock) }]
    const parsed = parseWorkspace({ ...workspace(snapshot), finished_goods: goods }, String(snapshot.factory_id))
    expect(parsed.finished_goods).toEqual(goods)
    expect(JSON.stringify(parsed.snapshot)).toBe(before)
    expect(parsed.snapshot).not.toHaveProperty('finished_goods')
  })

  it('distinguishes no qualified stock, not read yet, and old services without finished goods data', () => {
    const snapshot = source(fixture.legacy_snapshot)
    expect(parseWorkspace({ ...workspace(snapshot), finished_goods: [] }, String(snapshot.factory_id)).finished_goods).toEqual([])
    expect(parseWorkspace({ ...workspace(snapshot), finished_goods: null }, String(snapshot.factory_id)).finished_goods).toBeNull()
    expect(parseWorkspace(workspace(snapshot), String(snapshot.factory_id)).finished_goods).toBeUndefined()
  })

  it.each([
    { quantity: 0 }, { quantity: -1 }, { quantity: 0.5 }, { quantity: '50' },
    { completed_at: 'not finished' }, { completed_at: null }, { batch_id: '' }, { product_id: '' },
  ])('damaged finished goods quantity, time or identity is not shown as stock %#', change => {
    const snapshot = source(fixture.legacy_snapshot)
    const goods = { batch_id: 'SO-001-R001-B001', product_id: 'BRG-6202', quantity: 50, completed_at: String(snapshot.snapshot_clock) }
    expect(() => parseWorkspace({ ...workspace(snapshot), finished_goods: [{ ...goods, ...change }] }, String(snapshot.factory_id))).toThrow('The production data format is incomplete')
  })

  it.each(fixture.snapshots.slice(0, 3))('Snapshot/3 of $factory_id can be read without rewriting source content or hash', document => {
    const snapshot = source(document), before = JSON.stringify(snapshot)
    const parsed = parseWorkspace(workspace(snapshot), String(snapshot.factory_id))
    expect(parsed.snapshot?.schema_version).toBe('byof.snapshot/3')
    expect(parsed.snapshot?.business_terms?.evidence_mode).toBe('synthetic')
    expect(parsed.snapshot?.content_hash).toBe(snapshot.content_hash)
    expect(JSON.stringify(parsed.snapshot)).toBe(before)
  })

  it('reads the feasible empty plan of the real solver when everything is cancelled and there is no WIP', () => {
    const snapshot = source(fixture.snapshots[3]!), candidate = structuredClone(fixture.empty_candidate)
    const parsed = parseWorkspace(workspace(snapshot, candidate), String(snapshot.factory_id))
    expect(parsed.snapshot?.orders.every(o => o.quantity === 0)).toBe(true)
    expect(parsed.candidates[0]?.candidate).toMatchObject({ schema_version: 'byof.candidate/3', empty_demand: true, has_solution: true, assignments: [], checker: { status: 'PASS' } })
    expect(parsed.candidates[0]?.candidate.content_hash).toBe(candidate.content_hash)
  })

  it('old sources and ordinary plans stay compatible without adding empty_demand or recomputing existing hashes', () => {
    const snapshot = source(fixture.legacy_snapshot), candidate = structuredClone(fixture.legacy_candidate)
    const parsed = parseWorkspace(workspace(snapshot, candidate), String(snapshot.factory_id))
    expect(parsed.snapshot).toEqual(snapshot)
    expect(parsed.candidates[0]?.candidate).toEqual(candidate)
    expect(parsed.candidates[0]?.candidate).not.toHaveProperty('empty_demand')
    expect(parsed.candidates[0]?.candidate.content_hash).toBe(candidate.content_hash)
  })

  it.each([
    { empty_demand: 'true' }, { schema_version: 'byof.candidate/2' }, { has_solution: false },
    { assignments: [{ operation_id: 'invalid-extra-op', resource_id: 'M1', worker_id: 'W1', changeover_start: '2026-09-14T00:30:00Z', start_at: '2026-09-14T00:30:00Z', end_at: '2026-09-14T00:40:00Z' }] },
  ])('contradictory empty-demand type, version or operations cannot enter the workspace %#', change => {
    const snapshot = source(fixture.snapshots[3]!)
    expect(() => parseWorkspace(workspace(snapshot, { ...fixture.empty_candidate, ...change }), String(snapshot.factory_id))).toThrow()
  })
})
