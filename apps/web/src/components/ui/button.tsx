import type { ComponentProps } from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../lib/utils'

const variants = cva('ui-button', {
  variants: { variant: { default: 'ui-button-primary', outline: 'ui-button-outline', ghost: 'ui-button-ghost' } },
  defaultVariants: { variant: 'default' },
})

export function Button({ className, variant, asChild = false, ...props }: ComponentProps<'button'> & VariantProps<typeof variants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'button'
  return <Comp data-slot="button" className={cn(variants({ variant }), className)} {...props} />
}
