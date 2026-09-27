import type { Snapshot } from '../contracts'
import type { FactoryEditor } from '../factoryEditor'

export interface FactorySectionProps {
  snapshot: Snapshot
  onEdit: (editor: FactoryEditor) => void
  disabled: boolean
}
