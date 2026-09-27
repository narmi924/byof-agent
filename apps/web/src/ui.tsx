import { Button } from './components/ui/button'
/** Shared workbench display parts. Presentation only; no business decisions.
 *  Colors always go through Tone to the design tokens; components never hard-code colors. */

import { Badge as StatusBadge } from './components/ui/badge'
import type { ReactNode } from 'react'
import { useEffect, useId, useRef, useState } from 'react'
import type { Tone } from './palette'

export function Badge({ tone = 'neutral', children, dot = true }: { tone?: Tone; children: ReactNode; dot?: boolean }) {
  return <StatusBadge className={`badge tone-${tone}${dot ? '' : ' no-dot'}`}>{children}</StatusBadge>
}

export function MessageStrip({ tone = 'critical', children, alert = false }: { tone?: Tone; children: ReactNode; alert?: boolean }) {
  return <div className={`message-strip tone-${tone}`} role={alert ? 'alert' : undefined}>{children}</div>
}

export function SummaryChip({ label, value, unit, tone = 'neutral', onClick }: {
  label: string; value: ReactNode; unit?: string; tone?: Tone; onClick?: () => void
}) {
  const body = <><span>{label}</span><b>{value}</b>{unit ? <span>{unit}</span> : null}</>
  if (!onClick) return <span className={`summary-chip tone-${tone}`}>{body}</span>
  return <Button variant="outline" type="button" className={`summary-chip tone-${tone}`} onClick={onClick}>{body}</Button>
}

/** Key/value list. Business wording on the left, facts on the right. */
export function KeyValues({ items }: { items: { label: string; value: ReactNode }[] }) {
  return <dl className="kv">{items.map((item) => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>
}

/** Right-hand detail drawer. Esc or a click on the scrim closes it; opening moves focus into the drawer. */
export function Drawer({ label, children, footer, onClose }: {
  label: string; children: ReactNode; footer?: ReactNode; onClose: () => void
}) {
  const panel = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    panel.current?.focus()
    function onKey(event: KeyboardEvent) { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return <>
    <Button variant="outline" type="button" className="scrim" aria-label="Close details" onClick={onClose} />
    <aside className="drawer" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} ref={panel}>
      {children}
      {footer ? <div className="drawer-footer">{footer}</div> : null}
    </aside>
  </>
}

/** Light popover. Moves secondary notes out of the main line of sight while keeping them one click away. */
export function Popover({ label, children, tone }: { label: ReactNode; children: ReactNode; tone?: Tone }) {
  const [open, setOpen] = useState(false)
  const anchor = useRef<HTMLDivElement | null>(null)
  const id = useId()
  useEffect(() => {
    if (!open) return
    function onDown(event: MouseEvent) {
      if (anchor.current && !anchor.current.contains(event.target as Node)) setOpen(false)
    }
    function onKey(event: KeyboardEvent) { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); window.removeEventListener('keydown', onKey) }
  }, [open])
  return <div className="popover-anchor" ref={anchor}>
    <Button variant="outline" type="button" className={tone ? `badge tone-${tone}` : 'secondary'} aria-expanded={open} aria-controls={id} onClick={() => setOpen((value) => !value)}>{label}</Button>
    {open ? <div className="popover" id={id}>{children}</div> : null}
  </div>
}

/** Product wordmark; decoration is controlled by the global theme. */
export function BrandMark({ product }: { product?: string }) {
  return <>
    <span className="shell-brand"><i aria-hidden="true" />BYOF</span>
    {product ? <span className="shell-product">{product}</span> : null}
  </>
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty-state">
    <div className="empty-mark" aria-hidden="true"><span /><span /><span /></div>
    <h3>{title}</h3>
    {children}
  </div>
}
