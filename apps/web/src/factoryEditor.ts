/** A selected source record, never an approval or business strategy. */
export type FactoryEditor =
  | { kind: 'order'; orderId: string }
  | { kind: 'new-order' }
  | { kind: 'inventory'; materialId: string }
  | { kind: 'receipt'; receiptId: string }
  | { kind: 'new-receipt'; materialId?: string }
  | { kind: 'resource'; resourceId: string }
  | { kind: 'worker'; workerId: string }
  | { kind: 'remaining'; operationId: string }
  | { kind: 'quality'; operationId: string }
  | { kind: 'delivery-rule'; productId: string }
  | { kind: 'quote'; receiptId: string; quoteId?: string }
  | { kind: 'overtime'; targetType: 'worker' | 'resource'; targetId: string }
