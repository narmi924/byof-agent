import type { ComponentProps } from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cn } from '../../lib/utils'

export function Card({ className, asChild = false, ...props }: ComponentProps<'div'> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'div'
  return <Comp data-slot="card" className={cn('ui-card', className)} {...props} />
}
