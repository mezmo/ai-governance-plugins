// What the mod shows about Mezmo Governance, as plain functions shared by the hooks and the tests.
//
// The governance check itself is scripts/governance_hook.py, a PreToolUse command hook. It
// writes each session's counts, pending decisions and last result to
// ~/.mezmo/governance/sessions/<session id>.json, never the tool's arguments. The mod reads
// that file and adds what only it sees: which call is waiting, from its own tool.call hook.

export type GovernanceCounts = {
  allowed: number
  denied: number
  expired: number
  errors: number
  /** Calls auto mode's classifier blocked after Mezmo Governance allowed them. */
  auto_mode_blocked?: number
}

export type PendingDecision = {
  tool: string
  tool_name: string
  reason: string | null
  since_ms: number
  deadline_ms: number
}

export type LastDecision = {
  ts: string
  decision_id: string | null
  tool: string
  tool_name: string
  outcome: 'allowed' | 'denied' | 'expired' | 'error'
  detail: string | null
  by_person: boolean
}

export type GovernanceState = {
  counts: GovernanceCounts
  pending: Record<string, PendingDecision>
  last?: LastDecision
  host?: string | null
}

/** A tool call the mod has seen start and not finish. */
export type InFlightCall = {
  tool: string
  summary: string
}

export type BandLine = {
  text: string
  tone: 'waiting' | 'note'
}

/** Where governance_hook.py writes this session's status. Keep in step with its session_state_path. */
export function governanceStatePath(home: string | undefined, sessionId: string | undefined): string | undefined {
  if (!home || !sessionId) return undefined
  const safe = sessionId.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 128)
  return `${home.replace(/\/+$/, '')}/.mezmo/governance/sessions/${safe}.json`
}

const count = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0)

/** The status file's text as a state, or undefined when it isn't one. */
export function parseGovernanceState(text: string): GovernanceState | undefined {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return undefined
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const raw = value as Record<string, unknown>
  const counts = (raw.counts && typeof raw.counts === 'object' ? raw.counts : {}) as Record<string, unknown>
  const pending: Record<string, PendingDecision> = {}
  if (raw.pending && typeof raw.pending === 'object') {
    for (const [id, entry] of Object.entries(raw.pending as Record<string, unknown>)) {
      if (!entry || typeof entry !== 'object') continue
      const p = entry as Record<string, unknown>
      pending[id] = {
        tool: String(p.tool ?? ''),
        tool_name: String(p.tool_name ?? ''),
        reason: typeof p.reason === 'string' ? p.reason : null,
        since_ms: count(p.since_ms),
        deadline_ms: count(p.deadline_ms),
      }
    }
  }
  const last = raw.last && typeof raw.last === 'object' ? (raw.last as LastDecision) : undefined
  return {
    counts: {
      allowed: count(counts.allowed),
      denied: count(counts.denied),
      expired: count(counts.expired),
      errors: count(counts.errors),
      auto_mode_blocked: count(counts.auto_mode_blocked),
    },
    pending,
    last,
    host: typeof raw.host === 'string' ? raw.host : null,
  }
}

function clip(text: string, max: number): string {
  const line = text.split('\n')[0]!.trim()
  return line.length > max ? `${line.slice(0, max - 1)}…` : line
}

/** A few words that say what a tool call does, from its arguments. Shown only on screen. */
export function summarizeCall(tool: string, input: Record<string, unknown>): string {
  const field = (...names: string[]) => {
    for (const name of names) {
      const value = input[name]
      if (typeof value === 'string' && value.trim()) return value
    }
    return undefined
  }
  const text = field('command', 'file_path', 'notebook_path', 'url', 'pattern', 'query', 'description', 'path')
  return text ? clip(text, 60) : ''
}

/** A tool name as people read it: MCP tools without their mcp__ prefix. */
export function toolLabel(toolName: string): string {
  const parts = toolName.split('__')
  return parts.length === 3 && parts[0] === 'mcp' ? `${parts[1]}/${parts[2]}` : toolName
}

/** 72 seconds as 1:12. */
export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`
}

function describeCall(entry: PendingDecision, inFlight: readonly InFlightCall[]): string {
  const call = inFlight.find(c => c.tool === entry.tool_name && c.summary)
  const name = toolLabel(entry.tool_name || entry.tool)
  return call ? `${name}: ${call.summary}` : name
}

/**
 * The state as it stands while calls are in flight: a decision can only be pending while the
 * hook that waits for it runs inside a tool call, so without one, or past its deadline, an
 * entry is left over from a hook that was stopped.
 */
export function livePending(state: GovernanceState | undefined, hasInFlight: boolean, nowMs: number): GovernanceState | undefined {
  if (!state) return undefined
  const pending: Record<string, PendingDecision> = {}
  if (hasInFlight) {
    for (const [id, entry] of Object.entries(state.pending)) {
      if (entry.deadline_ms > nowMs) pending[id] = entry
    }
  }
  return { ...state, pending }
}

/** The band above the prompt: one line per call waiting for a person, then its reason. */
export function bandLines(state: GovernanceState | undefined, inFlight: readonly InFlightCall[], nowMs: number): BandLine[] {
  if (!state) return []
  const lines: BandLine[] = []
  const entries = Object.values(state.pending).sort((a, b) => a.since_ms - b.since_ms)
  for (const entry of entries) {
    const waited = formatDuration(nowMs - entry.since_ms)
    const left = formatDuration(entry.deadline_ms - nowMs)
    lines.push({
      text: `⏳ Mezmo Governance · waiting for approval · ${describeCall(entry, inFlight)} · ${waited}, blocked in ${left}`,
      tone: 'waiting',
    })
    if (entry.reason) lines.push({ text: `   ${entry.reason}`, tone: 'note' })
  }
  return lines
}

export function blockedCount(counts: GovernanceCounts): number {
  return counts.denied + counts.expired + counts.errors
}

/** The footer label: calls waiting, or this session's counts. Undefined before the first check. */
export function governanceLabel(state: GovernanceState | undefined): string | undefined {
  if (!state) return undefined
  const waiting = Object.keys(state.pending).length
  if (waiting > 0) return `Governance ⏳ ${waiting} waiting`
  const allowed = state.counts.allowed
  const blocked = blockedCount(state.counts)
  if (allowed + blocked === 0) return undefined
  return blocked > 0 ? `Governance ${allowed} ✓ ${blocked} ✗` : `Governance ${allowed} ✓`
}

/** The toast for a decision a person made, or for one nobody made in time. */
export function decisionToast(last: LastDecision, summary?: string): string | undefined {
  if (!last.by_person) return undefined
  const call = `${toolLabel(last.tool_name || last.tool)}${summary ? `: ${summary}` : ''}`
  const reason = last.detail ? ` (${clip(last.detail, 80)})` : ''
  if (last.outcome === 'allowed') return `✓ Approved in Mezmo Governance: ${call}${reason}`
  if (last.outcome === 'denied') return `✗ Denied in Mezmo Governance: ${call}${reason}`
  if (last.outcome === 'expired') return `✗ Nobody decided in time, so ${call} was blocked`
  return undefined
}

/**
 * What /mezmo says before governance has checked a call in this session. The access key is in
 * the keychain, out of the mod's reach, so it can't tell "off" from "on but not used yet".
 */
export const NO_GOVERNANCE_TEXT =
  "Mezmo Governance hasn't checked a tool call in this session. If it isn't set up, run /mezmo:setup governance."

/** What /mezmo says about governance in this session. */
export function governanceText(state: GovernanceState | undefined, nowMs: number): string | undefined {
  if (!state) return undefined
  const c = state.counts
  const lines = [
    `Mezmo Governance${state.host ? ` (${state.host})` : ''} in this session: ${c.allowed} allowed, ${c.denied} denied, ` +
      `${c.expired} not decided in time, ${c.errors} errors.`,
  ]
  const blockedByAutoMode = c.auto_mode_blocked ?? 0
  if (blockedByAutoMode > 0) {
    lines.push(`Auto mode blocked ${blockedByAutoMode} call${blockedByAutoMode === 1 ? '' : 's'} that Mezmo Governance allowed.`)
  }
  for (const entry of Object.values(state.pending)) {
    lines.push(
      `Waiting for a person: ${toolLabel(entry.tool_name || entry.tool)} for ${formatDuration(nowMs - entry.since_ms)}, ` +
        `blocked in ${formatDuration(entry.deadline_ms - nowMs)}.`,
    )
  }
  const last = state.last
  if (last) {
    const who = last.by_person ? ' by a person' : ''
    const detail = last.detail ? `: ${clip(last.detail, 200)}` : ''
    lines.push(`Last check: ${toolLabel(last.tool_name || last.tool)} ${last.outcome}${who}${detail}`)
  }
  return lines.join('\n')
}
