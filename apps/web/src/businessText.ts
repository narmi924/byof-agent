/** Presentation-only cleanup of model and tool text: internal field names, solver codes and raw
 *  timestamps become business wording in factory time. The facts themselves are not changed. */

const terms: [RegExp, string][] = [
  [/\([^()]*\bUTC\b[^()]*\)/g, ''],
  [/release\s*[0-9a-f]{8}-[0-9a-f-]{27},?\s*/gi, ''],
  [/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g, ''],
  [/\bcandidate\s*[0-9a-f]{6,}/gi, 'plan'],
  [/\bsource state\s*ACTIVE/gi, 'accepted by factory'],
  [/\bexecution state (is )?(still )?\s*NOT_STARTED/gi, 'not started'],
  [/\bNOT_STARTED\b/g, 'not started'],
  [/\bIN_PROGRESS\b/g, 'in production'],
  [/\bACTIVE\b/g, 'effective'],
  [/\bfinish\s*(was rejected with|rejected with)\s*EXECUTION_NOT_COMPLETED,?\s*/g, ''],
  [/\bEXECUTION_NOT_COMPLETED\b/g, 'production not finished'],
  [/\bexecution source/gi, 'factory system'],
  [/native_status\s*=\s*UNKNOWN/g, 'no feasible schedule found'],
  [/native_status\s*=\s*INFEASIBLE/g, 'cannot be scheduled under current conditions'],
  [/native_status\s*=\s*(FEASIBLE|OPTIMAL)/g, 'feasible schedule found'],
  [/has_solution\s*=\s*(true|false)[,;]?\s*/g, ''],
  [/checker\s*=\s*PASS/g, 'checked'],
  [/checker\s*=\s*FAIL/g, 'check failed'],
  [/\bTIME_LIMIT\b/g, 'calculation time limit'],
  [/\bUNKNOWN\b/g, 'no feasible schedule found'],
  [/\bINFEASIBLE\b/g, 'infeasible'],
  [/\b(FEASIBLE|OPTIMAL)\b/g, 'feasible'],
  [/\bindependent check\s*PASS|\bPASS\b/g, 'checked'],
  [/\bsolve_scenario\b/g, 'schedule calculation'],
  [/\bevaluate_business_options\b/g, 'option comparison'],
  [/\bproduction_exception\b/g, 'disruption handling'],
  [/\breport_production\b/g, 'production report'],
  [/\bnew_actions_not_before\b/g, 'earliest start for new work'],
  [/\breview_minutes\b/g, 'review buffer'],
  [/\bbusiness_clock\b/g, 'factory time'],
  [/\bplan_covered_quantity\b/g, 'quantity covered by the plan'],
  [/\bqualified_completed_quantity\b/g, 'qualified completed quantity'],
  [/\bmaterial_shortfalls\b/g, 'material shortfalls'],
  [/\border_facts\b/g, 'order figures'],
  [/\bsolver_outcomes\b/g, 'calculation results'],
  [/\bUNCHANGED_SEARCH\b/g, 'already calculated under the same conditions'],
  [/\bPROBLEM_SEARCH_LIMIT\b/g, 'calculation limit reached'],
  [/\bMATERIAL_SHORTFALL\b/g, 'material shortage'],
  [/\bLATE_PLAN_NEEDS_OPTIONS\b/g, 'would delay delivery'],
  [/\bSEARCH_NEEDS_OPTIONS\b/g, 'response options needed'],
  [/\bWIP_CONFIRMATION_REQUIRED\b/g, 'remaining work to confirm'],
  [/\bCASE_FACTS_CHANGED\b/g, 'shop floor changed'],
  [/\bSUBJECT_NOT_FOUND\b/g, 'not on the current shop floor'],
]

// Model wording such as "2026-09-27 09:00 UTC" or "09-27 09:00Z".
const looseUtc = /(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})\s*(?:Z|UTC)(?![A-Za-z])/g
const isoTime = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})/g

export function shortDateTime(value: string | null | undefined, timezone?: string): string {
  if (!value) return 'Not confirmed'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return 'Not confirmed'
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone ?? 'UTC', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(date).map(part => [part.type, part.value]))
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

export function plainBusinessText(value: string, timezone?: string): string {
  let text = value.replace(isoTime, match => shortDateTime(match, timezone))
    .replace(looseUtc, (match, year: string | undefined, month: string, day: string, hour: string, minute: string) => {
      const at = Date.UTC(Number(year ?? new Date().getUTCFullYear()), Number(month) - 1, Number(day), Number(hour), Number(minute))
      return Number.isFinite(at) ? shortDateTime(new Date(at).toISOString(), timezone) : match
    })
  for (const [pattern, replacement] of terms) text = text.replace(pattern, replacement)
  return text.replace(/\(\s*\)/g, '').replace(/,\s*([,.])/g, '$1').replace(/ {2,}/g, ' ')
}
