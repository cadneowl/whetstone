import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useConsoleConfig, useGitStatus } from '@/api/client'
import { ErrorBoundary } from './ErrorBoundary'
import { ModelPicker } from './ModelPicker'
import { Badge } from './primitives'

/**
 * Routes that are workspaces rather than documents.
 *
 * `max-w-6xl` is right for reading a skill's guidance and wrong for triage, which is three panes —
 * queue, evidence, form — and on a wide monitor was rendering all of them inside 1152px with the
 * rest of the screen left empty. Prose still gets a measure; the workbench gets the desk.
 */
const WIDE_ROUTES = ['/triage', '/reviews/']

/**
 * The header does NOT follow the body's width.
 *
 * It used to, on the reasoning that the nav should line up with the content beneath it. But the two
 * widths are centred, so every navigation between a document route and a workspace route slid the
 * whole header sideways by a few hundred pixels — the tab you were aiming at moved while you were
 * moving towards it. Chrome that stays put is worth far more than chrome that lines up: the nav is
 * anchored to the viewport, and only the content column below it changes measure.
 */
const HEADER = 'mx-auto max-w-[120rem]'

export function AppShell() {
  const { data: config } = useConsoleConfig()
  const { data: git } = useGitStatus()
  const location = useLocation()

  const wide = WIDE_ROUTES.some((route) => location.pathname.startsWith(route))
  const container = wide ? 'mx-auto max-w-[120rem]' : 'mx-auto max-w-6xl'

  return (
    <div className="min-h-screen">
      <header className="border-b border-line">
        <div className={`${HEADER} flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-3`}>
          <NavLink to="/" className="text-[15px] font-semibold">
            Whetstone
          </NavLink>
          {/* Ordered as the loop runs: what needs doing, the signal behind it, the skills it
              changes, and the evidence it produced. */}
          <nav className="flex gap-4 text-sm">
            <Tab to="/" end>
              Inbox
            </Tab>
            <Tab to="/triage">Triage</Tab>
            <Tab to="/reviews">Reviews</Tab>
            <Tab to="/skills">Skills</Tab>
            <Tab to="/runs">Runs</Tab>
            <Tab to="/judge">Judge</Tab>
          </nav>

          <div className="ml-auto flex items-center gap-2 text-xs text-muted">
            {config && <ModelPicker readOnly={config.read_only} />}
            {config?.read_only && (
              <Badge tone="warn" title="Mutating routes are disabled server-side">
                read-only
              </Badge>
            )}
            {config?.practice_mode && (
              <Badge tone="warn" title="Runs use deterministic doubles — no model, no spend">
                practice mode
              </Badge>
            )}
            {git?.available && git.status && (
              <span title={`HEAD ${git.status.head.slice(0, 12)}`}>
                <span className="font-mono">{git.status.branch}</span>
                {!git.status.clean && <span className="ml-1 text-warn">•</span>}
              </span>
            )}
            {config && <span title={config.principal.email}>{config.principal.name}</span>}
          </div>
        </div>
      </header>

      <main className={`${container} px-5 py-6`}>
        {/* Keyed on the route so a crash on one page does not wedge the others. */}
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  )
}

function Tab({ to, end, children }: { to: string; end?: boolean; children: string }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        isActive ? 'text-ink' : 'text-muted transition-colors hover:text-ink'
      }
    >
      {children}
    </NavLink>
  )
}
