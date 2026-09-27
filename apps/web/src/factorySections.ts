/** Business groups on the single factory workspace, not simulator commands. */
export const factorySections = [
  { id: 'orders', title: 'Orders and delivery', description: 'Customer orders, demand changes and delivery rules' },
  { id: 'supply', title: 'Inventory and supply', description: 'Raw materials, receipts and finished goods' },
  { id: 'capacity', title: 'Machines and staff', description: 'Machines, staff and available shifts' },
  { id: 'execution', title: 'Execution and quality', description: 'WIP progress, blocked operations and quality checks' },
] as const

export type FactorySection = typeof factorySections[number]['id']
