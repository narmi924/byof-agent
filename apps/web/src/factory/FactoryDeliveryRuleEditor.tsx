import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import type { FactoryEditor } from '../factoryEditor'
import type { FactoryEditorProps } from './editorTypes'
import { FactoryControlForm } from './FactoryControlForm'
import { whole } from './termsValue'

type Props = FactoryEditorProps & { selection: Extract<FactoryEditor, { kind: 'delivery-rule' }> }

export function FactoryDeliveryRuleEditor({ snapshot, disabled, command, onError, selection }: Props) {
  const productId = selection.productId
  const product = snapshot.profile.products.find(item => item.product_id === productId)
  const terms = snapshot.business_terms
  const rule = terms?.delivery_rules.find(item => item.product_id === productId)
  const enterprise = terms?.evidence_mode === 'enterprise'
  return <FactoryControlForm key={`${productId}:${terms?.version ?? 'none'}`} id="factory-delivery-rules" title="Customer split-delivery rule" disabled={disabled || enterprise || !product} onError={onError} onSubmit={data => {
    if (!product) throw new Error('Choose a product of this factory.')
    const minimum = whole(data, 'minimum'), partial = data.get('partial') === 'on'
    if (minimum % product.batch_size) throw new Error(`The minimum delivery quantity must be a whole multiple of ${product.batch_size} pcs per batch.`)
    const deliveries = whole(data, 'deliveries')
    if (partial && deliveries !== 2) throw new Error('Allowing split delivery requires two deliveries.')
    command('delivery_rule.set', { expected_terms_version: terms?.version ?? null, product_id: productId, partial_delivery_allowed: partial, minimum_partial_quantity: minimum, max_deliveries: deliveries }, `Save split-delivery rule for ${product.name}`)
  }}>
    <p className="muted">Records a verified customer rule. It does not change order quantities or due dates and does not accept any split-delivery option.</p>
    <p>Product: {product?.name ?? productId} · {product?.batch_size ?? 'unknown'} pcs per batch</p>
    <p>{rule ? `Current rule: ${rule.partial_delivery_allowed ? 'split delivery allowed' : 'split delivery not allowed'}, at least ${rule.minimum_partial_quantity} pcs, at most ${rule.max_deliveries} deliveries.` : 'No split-delivery rule is configured for this product; the Agent never assumes the customer allows split delivery.'}</p>
    {enterprise ? <p className="notice">This rule comes from the enterprise source; maintain it in the enterprise system. It cannot be overwritten here.</p> : null}
    <label className="checkbox-label"><Input name="partial" type="checkbox" defaultChecked={rule?.partial_delivery_allowed ?? false} />The customer confirmed split delivery is allowed</label>
    <label>Minimum delivery quantity (pcs)<Input name="minimum" type="number" min={product?.batch_size ?? 1} step={product?.batch_size ?? 1} defaultValue={rule?.minimum_partial_quantity ?? product?.batch_size} required /></label>
    <label>Maximum deliveries<select name="deliveries" defaultValue={rule?.max_deliveries ?? 2}><option value="1">1</option><option value="2">2</option></select></label>
    <Button variant="outline" type="submit">Save split-delivery rule</Button>
  </FactoryControlForm>
}
