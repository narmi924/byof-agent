import { Button } from './components/ui/button'
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

/** Edit one source record in a focused dialog; unsaved input is confirmed before closing. */
export function FactoryEditorDialog({ label, busy = false, dirty = false, onClose, children }: {
  label: string; busy?: boolean; dirty?: boolean; onClose: () => void; children: ReactNode
}) {
  const panel = useRef<HTMLElement | null>(null)
  const [confirmClose, setConfirmClose] = useState(false)
  useEffect(() => {
    const opener = document.activeElement
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    panel.current?.focus()
    return () => {
      document.body.style.overflow = previousOverflow
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])

  function close() {
    if (busy) return
    if (dirty) { setConfirmClose(true); panel.current?.focus() }
    else onClose()
  }
  function onKey(event: KeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
    if (event.key !== 'Tab') return
    const elements = [...(panel.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex="0"]') ?? [])]
      .filter(element => !element.matches(':disabled') && !element.closest('[hidden], [inert]') && getComputedStyle(element).display !== 'none' && getComputedStyle(element).visibility !== 'hidden' && (!element.closest('details:not([open])') || element.tagName === 'SUMMARY'))
    const first = elements[0], last = elements.at(-1)
    if (!first || !last) { event.preventDefault(); panel.current?.focus(); return }
    const outsideTabOrder = !elements.includes(document.activeElement as HTMLElement)
    if (event.shiftKey && (document.activeElement === first || outsideTabOrder)) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && (document.activeElement === last || outsideTabOrder)) { event.preventDefault(); first.focus() }
  }
  return <div className="factory-editor-layer">
    <Button variant="outline" type="button" className="factory-editor-scrim" aria-label="Back to the shop floor" tabIndex={-1} disabled={busy} onClick={close} />
    <section className="factory-editor" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} ref={panel} onKeyDown={onKey}>
      <header className="factory-editor-header"><div><p className="eyebrow">Shop floor record</p><h2>{label}</h2></div><Button variant="outline" type="button" className="ghost" aria-label="Close editor" disabled={busy} onClick={close}>Close</Button></header>
      <div className="factory-editor-body">{children}</div>
      {confirmClose && dirty ? <div className="factory-editor-confirm" role="alert"><p>You have unsaved changes. Closing discards them.</p><div className="factory-editor-actions"><Button variant="outline" type="button" disabled={busy} onClick={() => { setConfirmClose(false); panel.current?.focus() }}>Keep editing</Button><Button variant="outline" type="button" className="secondary" disabled={busy} onClick={onClose}>Discard and close</Button></div></div> : null}
      {busy ? <p className="factory-editor-pending" role="status">Checking the submission result, please wait.</p> : null}
    </section>
  </div>
}
