import type { ReactNode } from 'react'

/** Persistent frame: the sidebar and top bar stay mounted while only the main view changes. */
export function Shell({ surface, sidebar, topbar, children }: {
  surface: 'agent' | 'factory'; sidebar?: ReactNode; topbar: ReactNode; children: ReactNode
}) {
  return <div className={`app-shell ${surface}-shell${sidebar ? '' : ' sidebar-hidden'}`}>
    <a className="skip-link" href="#main">Skip to main content</a>
    {sidebar}
    <div className="shell-main">
      {topbar}
      <main id="main">{children}</main>
    </div>
  </div>
}
