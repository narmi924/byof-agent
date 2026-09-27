import type { ReactNode } from 'react'
import { Settings, X } from 'lucide-react'
import { Button } from './components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogTitle, DialogTrigger } from './components/ui/dialog'
import { ModelSwitch } from './ModelSwitch'

export function WorkbenchSettings({ children }: { children?: ReactNode }) {
  return <Dialog>
    <DialogTrigger asChild><Button variant="ghost" className="settings-trigger"><Settings size={16} aria-hidden="true" />Settings</Button></DialogTrigger>
    <DialogContent aria-describedby={undefined}>
      <div className="section-heading"><DialogTitle>Settings</DialogTitle><DialogClose asChild><Button variant="ghost" aria-label="Close settings"><X size={16} /></Button></DialogClose></div>
      <ModelSwitch />
      {children}
    </DialogContent>
  </Dialog>
}
