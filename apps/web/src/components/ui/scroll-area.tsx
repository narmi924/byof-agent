import type { ComponentProps } from 'react'
import * as ScrollPrimitive from '@radix-ui/react-scroll-area'
import { cn } from '../../lib/utils'

export function ScrollArea({ className, children, ...props }: ComponentProps<typeof ScrollPrimitive.Root>) {
  return <ScrollPrimitive.Root className={cn('ui-scroll-area', className)} {...props}>
    <ScrollPrimitive.Viewport className="ui-scroll-viewport">{children}</ScrollPrimitive.Viewport>
    <ScrollPrimitive.Scrollbar orientation="vertical" className="ui-scrollbar"><ScrollPrimitive.Thumb className="ui-scroll-thumb" /></ScrollPrimitive.Scrollbar>
    <ScrollPrimitive.Corner />
  </ScrollPrimitive.Root>
}
