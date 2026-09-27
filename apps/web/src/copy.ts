/** Product wording shared by several components.
 *  Sentences used by a single component stay next to that component. */

export const appNames = { agent: 'Production Agent', simulator: 'Disruption simulator' } as const

export const measureNames: Record<string, string> = {
  supply: 'Resupply', repair: 'Expedited repair', staff: 'Qualified cover',
  order_due: 'Extend due date', order_quantity: 'Reduce quantity',
}

export const metricNames: Record<string, string> = {
  weighted_tardiness: 'Weighted tardiness', incremental_overtime_metric: 'Added overtime',
  changed_operations: 'Changed operations', total_start_shift: 'Total start shift', makespan: 'Makespan',
}

export const unitNames: Record<string, string> = { operations: 'ops', minutes: 'min' }

/** "1 order", "2 orders": counts in running text read naturally in English. */
export const count = (value: number, noun: string, plural = `${noun}s`) => `${value} ${value === 1 ? noun : plural}`

/** English display names of the routing steps in the protected SKF seed, which keeps its original
 *  Chinese names. Display only: unknown names are shown as the source gives them. */
const seedStepNames: Record<string, string> = {
  '物料配套与装配前准备': 'Kitting and pre-assembly prep',
  '套圈与滚动体装配': 'Ring and rolling element assembly',
  '保持架装配': 'Cage assembly',
  '装脂密封前检查': 'Pre-lubrication and sealing check',
  '注脂': 'Grease filling',
  '密封圈安装': 'Seal installation',
  '最终检验与放行': 'Final inspection and release',
  '打标与包装': 'Marking and packaging',
}
export const stepName = (name: string) => seedStepNames[name] ?? name
