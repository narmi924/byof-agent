/** The single mapping from schedule states to display meaning. Legend, blocks and the detail drawer all read it, so color and text never disagree.
 *  Color alone carries no information: every state has a text label and a texture, so it stays readable for color-blind users and in grayscale print.
 *  The design rules are in docs/DESIGN.md. */

export type Tone = 'positive' | 'critical' | 'negative' | 'info' | 'neutral'

/** The display state of one operation at a moment. */
export type OperationState =
  | 'PLANNED'
  | 'SETUP'
  | 'RUNNING'
  | 'DONE'
  | 'UNJUDGED'
  | 'BLOCKED'
  | 'UNCONFIRMED'
  | 'REPLAN'

export interface StateStyle {
  /** Label on blocks and in the legend. */
  label: string
  /** One sentence on which fact decides this state. */
  basis: string
  /** Semantic tone for badges and message strips. */
  tone: Tone
  /** CSS modifier class of the block, defined in board.css. */
  modifier: string
  /** Texture description for the legend and accessible descriptions. */
  texture: 'solid' | 'stripe' | 'dashed'
  /** Light or dark text on the block, for contrast. */
  ink: 'light' | 'dark'
}

export const operationStates: Record<OperationState, StateStyle> = {
  PLANNED: {
    label: 'Planned',
    basis: 'Scheduled; the factory has no execution record yet',
    tone: 'positive',
    modifier: 'is-planned',
    texture: 'solid',
    ink: 'dark',
  },
  SETUP: {
    label: 'Changeover',
    basis: 'The factory started preparing; production has not started',
    tone: 'critical',
    modifier: 'is-setup',
    texture: 'stripe',
    ink: 'dark',
  },
  RUNNING: {
    label: 'In production',
    basis: 'The factory started and is producing',
    tone: 'critical',
    modifier: 'is-running',
    texture: 'solid',
    ink: 'dark',
  },
  DONE: {
    label: 'Done, passed',
    basis: 'Completed with a passed quality record',
    tone: 'neutral',
    modifier: 'is-done',
    texture: 'solid',
    ink: 'dark',
  },
  UNJUDGED: {
    label: 'Done, awaiting QC',
    basis: 'Completed; the quality result is not recorded yet',
    tone: 'neutral',
    modifier: 'is-unjudged',
    texture: 'solid',
    ink: 'dark',
  },
  BLOCKED: {
    label: 'Interrupted',
    basis: 'Execution interrupted or quality failed',
    tone: 'negative',
    modifier: 'is-blocked',
    texture: 'solid',
    ink: 'light',
  },
  UNCONFIRMED: {
    label: 'Remaining work unconfirmed',
    basis: 'Interrupted and the remaining work has no confirmed source',
    tone: 'negative',
    modifier: 'is-unconfirmed',
    texture: 'stripe',
    ink: 'light',
  },
  REPLAN: {
    label: 'Needs rescheduling',
    basis: 'The planned slot has passed without an execution record',
    tone: 'critical',
    modifier: 'is-replan',
    texture: 'dashed',
    ink: 'dark',
  },
}

/** Legend order follows "not started → in progress → finished → problem", the same order the board is read. */
export const legendOrder: OperationState[] = [
  'PLANNED',
  'SETUP',
  'RUNNING',
  'DONE',
  'UNJUDGED',
  'BLOCKED',
  'UNCONFIRMED',
  'REPLAN',
]

/** The four main states always shown on the board; other states are added to the legend when they appear. */
export const primaryLegend: OperationState[] = ['PLANNED', 'RUNNING', 'DONE', 'BLOCKED']

export type ShiftBand = 'NORMAL' | 'OVERTIME' | 'CLOSED' | 'UNAVAILABLE'

export const shiftBands: Record<ShiftBand, { label: string; basis: string; modifier: string }> = {
  NORMAL: { label: 'Regular shift', basis: 'Regular working window in the calendar', modifier: 'band-normal' },
  OVERTIME: { label: 'Overtime window', basis: 'Overtime window in the calendar; needs manager approval', modifier: 'band-overtime' },
  CLOSED: { label: 'Non-working time', basis: 'Gaps between calendar windows, including breaks and nights', modifier: 'band-closed' },
  UNAVAILABLE: { label: 'Unavailable', basis: 'Down, absent or an explicit unavailable period', modifier: 'band-unavailable' },
}

/** Business state to semantic tone. Unlisted values are neutral and never guessed to be normal. */
const tones: Record<string, Tone> = {
  // Machines and staff
  AVAILABLE: 'positive',
  MAINTENANCE: 'critical',
  DOWN: 'negative',
  ABSENT: 'negative',
  // Orders
  CONFIRMED: 'info',
  IN_PROGRESS: 'critical',
  COMPLETED: 'positive',
  CANCELLED: 'neutral',
  // Receipts
  EXPECTED: 'neutral',
  RECEIVED: 'positive',
  // Quality
  PENDING: 'neutral',
  PASSED: 'positive',
  FAILED: 'negative',
  // Execution
  SETUP: 'critical',
  BLOCKED: 'negative',
  NOT_STARTED: 'neutral',
  // Data freshness
  CURRENT: 'positive',
  STALE: 'critical',
  UNKNOWN: 'neutral',
  // Plans
  CANDIDATE: 'info',
  APPROVED: 'positive',
  NO_SOLUTION: 'negative',
  CHECK_FAILED: 'negative',
  // Factory acceptance
  PENDING_SOURCE: 'critical',
  ACCEPTED_PENDING_EFFECTIVE: 'critical',
  ACTIVE: 'positive',
  REJECTED: 'negative',
  // Tasks
  OPEN: 'critical',
  ESCALATED: 'negative',
  RESPONDED: 'info',
  ACCEPTED: 'positive',
  REVIEWED: 'positive',
  // Solve jobs
  QUEUED: 'info',
  RUNNING: 'info',
  SUCCEEDED: 'positive',
  FAILED_JOB: 'negative',
  // Checks
  PASS: 'positive',
  NOT_RUN: 'neutral',
}

export const toneOf = (value: string | null | undefined): Tone =>
  value && Object.hasOwn(tones, value) ? tones[value]! : 'neutral'
