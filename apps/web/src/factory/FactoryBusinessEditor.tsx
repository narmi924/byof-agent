import type { FactoryEditorProps } from './editorTypes'
import { FactoryOrdersEditor } from './FactoryOrdersEditor'
import { FactorySupplyEditor } from './FactorySupplyEditor'
import { FactoryCapacityEditor } from './FactoryCapacityEditor'
import { FactoryExecutionEditor } from './FactoryExecutionEditor'

export function FactoryBusinessEditor(props: FactoryEditorProps) {
  switch (props.selection.kind) {
    case 'order':
    case 'new-order':
    case 'delivery-rule': return <FactoryOrdersEditor {...props} />
    case 'inventory':
    case 'receipt':
    case 'new-receipt':
    case 'quote': return <FactorySupplyEditor {...props} />
    case 'resource':
    case 'worker':
    case 'overtime': return <FactoryCapacityEditor {...props} />
    case 'remaining':
    case 'quality': return <FactoryExecutionEditor {...props} />
  }
}
