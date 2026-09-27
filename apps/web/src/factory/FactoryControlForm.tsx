import { Button } from '../components/ui/button'
import type { ReactNode } from 'react'
import { errorMessage } from '../api'

export function FactoryControlForm({ id, title, disabled, children, button, onSubmit, onError }: { id?: string; title: string; disabled: boolean; children: ReactNode; button?: string; onSubmit: (data: FormData) => void; onError: (message: string) => void }) {
  return <form id={id} tabIndex={-1} className="control-form" aria-label={title} onSubmit={event => {
    event.preventDefault()
    if (disabled) return
    try { onSubmit(new FormData(event.currentTarget)) } catch (reason) { onError(errorMessage(reason, 'Check the input.')) }
  }}><fieldset disabled={disabled}><legend>{title}</legend>{children}{button ? <Button type="submit">{button}</Button> : null}</fieldset></form>
}
