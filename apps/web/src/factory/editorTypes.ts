import type { SimulatorCommand, SimulatorStatus, Snapshot } from '../contracts'
import type { FactoryEditor } from '../factoryEditor'

export type FactoryCommand = (kind: SimulatorCommand['kind'], payload: SimulatorCommand['payload'], label: string) => void

export interface FactoryEditorProps {
  snapshot: Snapshot
  selection: FactoryEditor
  disabled: boolean
  zone: string
  status: SimulatorStatus | null
  command: FactoryCommand
  onError: (message: string) => void
}
