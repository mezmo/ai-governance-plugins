// The Mezmo mod: says in the prompt footer whether this session exports to
// Mezmo, checks the ingestion key once, and answers /mezmo without a turn.
// For Mezmo Governance it shows, above the prompt, each tool call waiting for a
// person to approve it, toasts that person's decision, and counts this
// session's checks in the footer.
//
// Claude Code reads its OTel settings once, at startup, but copies a changed
// settings `env` into its own environment mid-session. So the environment is
// read once, at session.start, and kept; the user settings file is read from
// disk each time, at the path scripts/claude_code_otel.py writes.
//
// The governance check is scripts/governance_hook.py, a PreToolUse command hook
// that runs inside this mod's tool.call `next()`. It writes the session's status
// file, which this mod reads; enforcement never depends on the mod.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { MezmoCheck, MezmoFacts } from './state'
import {
  bandLines,
  decisionToast,
  governanceLabel,
  governanceStatePath,
  governanceText,
  livePending,
  parseGovernanceState,
  summarizeCall,
} from './governance'
import type { GovernanceState, InFlightCall } from './governance'
import {
  CHECK_URL,
  describe,
  factsOf,
  footerLabel,
  isExporting,
  keyOf,
  mask,
  otelEnvOf,
  outcomeOf,
  parseSettings,
  reasonOf,
  settingsPath,
} from './status'

const sessionFacts = atom({ plugin: 'mezmo', key: 'session' } as const, null)
const settingsFacts = atom({ plugin: 'mezmo', key: 'settings' } as const, null)
const lastCheck = atom({ plugin: 'mezmo', key: 'check' } as const, null)

// $.state keeps the values above across a reload of this module; these copies
// keep them across /clear, /resume and /branch, which reset $.state.
let sessionCopy: MezmoFacts | null = null
let checkCopy: MezmoCheck | null = null
// The key this process exports with. It never leaves the module.
let sessionKey: string | undefined
const running = new Map<string, Promise<MezmoCheck>>()

// Governance: this session's status file as governance_hook.py last wrote it, and
// the tool calls in flight, which only this mod sees. Arguments stay in memory.
let governancePath: string | undefined
let governance: GovernanceState | undefined
const inFlight = new Map<number, InFlightCall>()
let nextCallId = 0
let ticker: { cancel(): void } | undefined
// Decisions already toasted, or already made when the session started.
const seenDecisions = new Set<string>()

type Refresh = 'toast' | 'silent' | 'baseline'

// Read the status file. `toast` announces a person's decision not seen before;
// `baseline` marks the current one as seen, for a session that resumes.
async function refreshGovernance($: EngineInterface, mode: Refresh): Promise<void> {
  if (!governancePath) return
  let text: unknown
  try {
    text = await $.fs.read(governancePath)
  } catch {
    return // no file yet: governance is off, or hasn't checked a call in this session
  }
  const state = typeof text === 'string' ? parseGovernanceState(text) : undefined
  if (!state) return // half-written: keep the last reading
  const last = state.last
  if (last?.decision_id && !seenDecisions.has(last.decision_id) && mode !== 'silent') {
    seenDecisions.add(last.decision_id)
    if (mode === 'toast') {
      const call = [...inFlight.values()].find(c => c.tool === last.tool_name)
      const message = decisionToast(last, call?.summary)
      if (message) $.ui.toast(message, { timeoutMs: 8000 })
    }
  }
  governance = state
  $.ui.invalidate('ui.render')
}

async function locateGovernance($: EngineInterface): Promise<void> {
  const home = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE'))
  let sessionId: string | undefined
  try {
    sessionId = await $.session.id()
  } catch {
    sessionId = undefined
  }
  governancePath = governanceStatePath(home, sessionId)
  governance = undefined
  seenDecisions.clear()
  await refreshGovernance($, 'baseline')
}

// While a call has run for over a second, read the status file and redraw once a second.
function startTicking($: EngineInterface): void {
  if (ticker) return
  ticker = $.clock.every(1000, async () => {
    await refreshGovernance($, 'toast')
    $.ui.invalidate('ui.render')
  })
}

function stopTicking(): void {
  ticker?.cancel()
  ticker = undefined
}

// The state with stale pending entries dropped, and the time it was taken at. The clock is
// read only once governance has checked a call in this session.
async function liveGovernance($: EngineInterface): Promise<[GovernanceState | undefined, number]> {
  if (!governance) return [undefined, 0]
  const now = await $.clock.now()
  return [livePending(governance, inFlight.size > 0, now), now]
}

async function readSessionEnv($: EngineInterface) {
  return {
    enable: await $.env.get('CLAUDE_CODE_ENABLE_TELEMETRY'),
    endpoint: await $.env.get('OTEL_EXPORTER_OTLP_ENDPOINT'),
    headers: await $.env.get('OTEL_EXPORTER_OTLP_HEADERS'),
  }
}

async function runCheck($: EngineInterface, apikey: string): Promise<MezmoCheck> {
  const key = mask(apikey)
  let result: MezmoCheck
  try {
    const response = await $.http.fetch(CHECK_URL, {
      method: 'POST',
      headers: { apikey, 'content-type': 'application/json' },
      body: '{}',
    })
    result = { key, outcome: outcomeOf(response.status), status: response.status }
  } catch (error) {
    result = { key, outcome: 'unreachable', reason: reasonOf(error, apikey) }
  }
  checkCopy = result
  running.delete(key)
  await update($, lastCheck, () => result).catch(() => undefined)
  return result
}

// One check per key per session: a key already checked, or being checked, isn't sent again.
function ensureCheck($: EngineInterface, apikey: string): Promise<MezmoCheck> | undefined {
  const key = mask(apikey)
  const pending = running.get(key)
  if (pending) return pending
  if (checkCopy?.key === key) return undefined
  checkCopy = { key, outcome: 'running' }
  const started = runCheck($, apikey)
  running.set(key, started)
  return started
}

// The user settings file as it is on disk now: {} when there is none, undefined when it
// isn't a JSON object. It is read from disk because ConfigChange arrives before Claude
// Code's own copy of the settings changes.
async function readUserSettings($: EngineInterface): Promise<Record<string, unknown> | undefined> {
  const home = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE'))
  const path = settingsPath(await $.env.get('CLAUDE_CONFIG_DIR'), home)
  if (path === undefined) return {}
  let text: string
  try {
    text = await $.fs.read(path)
  } catch {
    return {}
  }
  return parseSettings(text)
}

// Read the user settings file, note what it says, and start the check if one is due.
// The check comes back unawaited, so only /mezmo waits on the network.
async function refresh($: EngineInterface): Promise<{ pending?: Promise<MezmoCheck> }> {
  const settings = await readUserSettings($)
  // A file that isn't JSON (say, half-written by an editor) leaves the last reading in place.
  if (settings === undefined && (await read($, settingsFacts)) !== null) return {}
  const env = otelEnvOf(settings)
  const facts = factsOf(env)
  await update($, settingsFacts, () => facts)
  if (!sessionCopy) return {}
  const apikey = isExporting(sessionCopy) ? sessionKey : facts.isMezmo ? keyOf(env) : undefined
  const pending = apikey === undefined ? undefined : ensureCheck($, apikey)
  if (pending !== undefined) await update($, lastCheck, () => checkCopy)
  return { pending }
}

async function start($: EngineInterface) {
  const env = await readSessionEnv($)
  // After a reload, keep what the process started with: the environment may have changed since.
  const facts = (await read($, sessionFacts)) ?? sessionCopy ?? factsOf(env)
  const apikey = keyOf(env)
  sessionKey = apikey !== undefined && mask(apikey) === facts.key ? apikey : undefined
  sessionCopy = facts
  await update($, sessionFacts, () => facts)
  const kept = (await read($, lastCheck)) ?? checkCopy
  // A check the reload cut short runs again.
  checkCopy = kept?.outcome === 'running' ? null : kept
  await refresh($)
  await locateGovernance($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await start($)
    try {
      await $.command.register({
        name: 'mezmo',
        description: 'Show whether this session sends telemetry to Mezmo',
        immediate: true,
      })
    } catch {
      // A taken name leaves the footer indicator working.
    }
    return next(e)
  })

  // /mezmo:setup and /mezmo:remove write the user settings file: show the change now.
  on('classic.ConfigChange', { source: 'user_settings' }, async ($, e, next) => {
    await refresh($)
    return next(e)
  }).catch(($, e, next) => next(e))

  // /clear, /resume and /branch reset $.state: put back what this process started with.
  // They also start a new session id, so governance moves to that session's file.
  on('classic.SessionStart', { source: ['clear', 'resume', 'fork'] }, async ($, e, next) => {
    await update($, sessionFacts, () => sessionCopy)
    await update($, lastCheck, () => checkCopy)
    await refresh($)
    await locateGovernance($)
    return next(e)
  })

  // Each tool call runs the governance hook inside next(). Track it, so the band can say
  // which call is waiting, and read the status file once it's done.
  on('tool.call', async ($, e, next) => {
    if (!governancePath) return next(e)
    const id = ++nextCallId
    inFlight.set(id, { tool: e.tool, summary: summarizeCall(e.tool, e as unknown as Record<string, unknown>) })
    // Most calls finish within a second; only a slower one starts the once-a-second reads.
    const slow = $.clock.after(1000, () => startTicking($))
    try {
      return await next(e)
    } finally {
      slow.cancel()
      await refreshGovernance($, 'toast').catch(() => undefined)
      inFlight.delete(id)
      if (inFlight.size === 0) stopTicking()
      $.ui.invalidate('ui.render')
    }
  })

  on('command.run', { command: 'mezmo' }, async $ => {
    const { pending } = await refresh($)
    if (pending) await pending
    await refreshGovernance($, 'silent')
    const settings = await read($, settingsFacts)
    const telemetry =
      !sessionCopy || !settings ? "Mezmo: status isn't known yet." : describe(sessionCopy, settings, checkCopy).text
    const gov = governanceText(...(await liveGovernance($)))
    return { text: gov ? `${gov}\n\n${telemetry}` : telemetry }
  })

  on('ui.render', { component: 'SessionMode' }, async ($, e, next) => {
    // One label that names Mezmo: governance first, then telemetry.
    const [state] = await liveGovernance($)
    const session = await read($, sessionFacts)
    const settings = await read($, settingsFacts)
    const telemetry = session && settings ? describe(session, settings, await read($, lastCheck)).label : undefined
    const label = footerLabel([governanceLabel(state), telemetry])
    return label ? next({ ...e, props: { ...e.props, modes: [...e.props.modes, label] } }) : next(e)
  })

  // The band above the prompt, while a call waits for a person. Other mods share it.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const [state, now] = await liveGovernance($)
    const lines = bandLines(state, [...inFlight.values()], now)
    if (lines.length === 0) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const ours = Box({
      flexDirection: 'column',
      children: lines.map(line =>
        line.tone === 'waiting'
          ? Text({ children: [line.text], color: 'yellow', wrap: 'truncate-end' })
          : Text({ children: [line.text], dimColor: true, wrap: 'truncate-end' }),
      ),
    })
    const theirs = await next(e)
    return theirs ? Box({ flexDirection: 'column', children: [ours, theirs] }) : ours
  })
}
