// Tests for the Mezmo mod (hooks/). Run with `claude plugin test` from the repo root.

import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import {
  bandLines,
  decisionToast,
  formatDuration,
  governanceLabel,
  governanceStatePath,
  livePending,
  parseGovernanceState,
  summarizeCall,
} from '../hooks/governance'
import type { GovernanceState, LastDecision } from '../hooks/governance'
import {
  CHECK_URL,
  describe as describeStatus,
  footerLabel,
  factsOf,
  isMezmoEndpoint,
  mask,
  otelEnvOf,
  outcomeOf,
  parseOtlpHeaders,
  parseSettings,
  reasonOf,
  settingsPath,
} from '../hooks/status'

const KEY = 'abcd1234efgh5678'
const OTHER_KEY = 'wxyz0000zzzz9999'
const START_PART = 'Telemetry: start a new session to begin exporting'
const START_FOOTER = `Mezmo · ${START_PART}`

function mezmoEnv(key: string): Record<string, string> {
  return {
    CLAUDE_CODE_ENABLE_TELEMETRY: '1',
    OTEL_EXPORTER_OTLP_ENDPOINT: 'https://logs.mezmo.com/otel',
    OTEL_EXPORTER_OTLP_HEADERS: `apikey=${key}`,
  }
}

type Request = { url: string; method?: string; headers?: Record<string, string>; body?: string }

const SETTINGS_FILE = '/home/me/.claude/settings.json'

type World = {
  // This process's environment and the files on disk; a test may change either mid-session.
  env: Record<string, string>
  files: Record<string, string>
  requests: Request[]
}

// Stubs for everything the mod asks Claude Code for. `status` answers the connection check;
// a string makes the request fail with that reason.
function stubs(on: On, world: World, status: number | string = 207, wait?: (ms: number) => Promise<void>) {
  on('session.start', () => ({ cwd: '/work' }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('env.get', ($, e) => ({ value: world.env[e.name] }))
  on('fs.read', ($, e) => {
    const text = world.files[e.path]
    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })
  on('http.fetch', async ($, e) => {
    world.requests.push({ url: e.url, method: e.init?.method, headers: e.init?.headers, body: e.init?.body })
    if (wait) await wait(1000)
    if (typeof status === 'string') return { deny: status }
    return { value: { status, ok: status < 300, headers: {}, text: status === 207 ? 'Connection confirmed' : '' } }
  })
  on('classic.ConfigChange', () => ({}))
  // Claude Code's footer, drawn from the labels the mod passes on.
  on('ui.render', { component: 'SessionMode' }, ($, e) => ({
    type: 'Text',
    props: {},
    children: [(e.props as { modes: readonly string[] }).modes.join(' & ')],
  }))
  // Nothing else draws in the band above the prompt.
  on('ui.render', { component: 'AbovePrompt' }, () => ({ type: 'Box', props: {}, children: [] }))
}

function settingsText(env: Record<string, string>): string {
  return JSON.stringify({ env }, null, 2)
}

function world(env: Record<string, string>, settingsEnv: Record<string, string>): World {
  return { env: { HOME: '/home/me', ...env }, files: { [SETTINGS_FILE]: settingsText(settingsEnv) }, requests: [] }
}

type Surface = 'terminal' | 'desktop'

async function footer($: Engine, surface: Surface = 'terminal') {
  const ui = await $.ui.mount({ plugin: 'mezmo', surface, component: 'SessionMode', props: { modes: [] } })
  const found = await ui.find({ type: 'Text' })
  await ui.unmount()
  return found?.text ?? ''
}

const START = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const

// /mezmo typed at the prompt.
function mezmo($: Engine) {
  return $.command.run({
    command: 'mezmo',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  })
}

// The pure status logic.

test('recognizes the Mezmo endpoint the way the scripts do', () => {
  expect(isMezmoEndpoint('https://logs.mezmo.com/otel')).toBe(true)
  expect(isMezmoEndpoint(' https://LOGS.mezmo.com/otel/ ')).toBe(true)
  expect(isMezmoEndpoint('https://logs.mezmo.com/otel/v1/logs')).toBe(false)
  expect(isMezmoEndpoint('https://collector.example.com')).toBe(false)
  expect(isMezmoEndpoint(undefined)).toBe(false)
})

test('parses OTLP headers and percent-decodes values', () => {
  expect(parseOtlpHeaders('apikey=ab%2Bc,x-team = eng ')).toEqual({ apikey: 'ab+c', 'x-team': 'eng' })
  expect(parseOtlpHeaders('novalue,=x,apikey=%zz')).toEqual({ apikey: '%zz' })
  expect(parseOtlpHeaders(undefined)).toEqual({})
})

test('masks keys as first4…last4', () => {
  expect(mask(KEY)).toBe('abcd…5678')
  expect(mask('short')).toBe('****')
  expect(mask(undefined)).toBe('(not set)')
})

test('reads facts from a settings env block, ignoring non-strings', () => {
  const env = otelEnvOf({ env: { ...mezmoEnv(KEY), CLAUDE_CODE_ENABLE_TELEMETRY: 1 } })
  expect(env.enable).toBeUndefined()
  expect(factsOf(env)).toEqual({ isMezmo: true, isEnabled: false, hasKey: true, key: 'abcd…5678' })
  expect(otelEnvOf({ env: 'nope' })).toEqual({})
  expect(otelEnvOf(null)).toEqual({})
})

test('finds the user settings file the way the scripts do', () => {
  expect(settingsPath(undefined, '/home/me')).toBe('/home/me/.claude/settings.json')
  expect(settingsPath('/etc/claude/', '/home/me')).toBe('/etc/claude/settings.json')
  expect(settingsPath('~/work-claude', '/home/me')).toBe('/home/me/work-claude/settings.json')
  expect(settingsPath(undefined, undefined)).toBeUndefined()
})

test('reads a settings file only when it holds a JSON object', () => {
  expect(parseSettings('  ')).toEqual({})
  expect(parseSettings('{"env": {}}')).toEqual({ env: {} })
  expect(parseSettings('{"env": ')).toBeUndefined()
  expect(parseSettings('[]')).toBeUndefined()
})

test('classifies the connection check', () => {
  expect(outcomeOf(207)).toBe('ok')
  expect(outcomeOf(200)).toBe('unexpected')
  expect(outcomeOf(401)).toBe('rejected')
  expect(outcomeOf(403)).toBe('rejected')
  expect(outcomeOf(503)).toBe('unexpected')
})

test('an error reason never carries the key', () => {
  expect(reasonOf(new Error(`refused apikey=${KEY}`), KEY)).toBe('refused apikey=abcd…5678')
})

test('describes each state', () => {
  const on = factsOf({ enable: '1', endpoint: 'https://logs.mezmo.com/otel', headers: `apikey=${KEY}` })
  const off = factsOf({})
  const incomplete = factsOf({ endpoint: 'https://logs.mezmo.com/otel' })
  expect(describeStatus(off, off, null).label).toBeUndefined()
  expect(describeStatus(on, on, null).label).toBe('Telemetry ✓')
  expect(describeStatus(off, on, null).label).toBe(START_PART)
  expect(describeStatus(off, incomplete, null).label).toBe('Telemetry ✗ setup incomplete')
  expect(describeStatus(on, on, { key: 'abcd…5678', outcome: 'rejected', status: 401 }).label).toBe('Telemetry ✗ key rejected')
  expect(describeStatus(off, on, { key: 'abcd…5678', outcome: 'unreachable' }).label).toBe('Telemetry ✗ unreachable')
  // A check of some other key says nothing about this one.
  expect(describeStatus(on, on, { key: 'wxyz…9999', outcome: 'rejected', status: 401 }).label).toBe('Telemetry ✓')
  expect(describeStatus(on, off, null).text).toContain('no longer point at Mezmo')
})

// The hooks, as a session drives them.

test('a session that exports shows Mezmo · Telemetry ✓ after one connection check', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w)
  await $.session.start(START)
  const answer = await mezmo($)
  await mezmo($)

  for (const surface of ['terminal', 'desktop'] as const) {
    expect(await footer($, surface)).toBe('Mezmo · Telemetry ✓')
  }
  expect(w.requests).toEqual([
    { url: CHECK_URL, method: 'POST', headers: { apikey: KEY, 'content-type': 'application/json' }, body: '{}' },
  ])
  expect(answer.text).toContain('This session exports telemetry to https://logs.mezmo.com/otel (ingestion key abcd…5678).')
  expect(answer.text).toContain('Mezmo accepted the key (HTTP 207). Nothing was published.')
  expect(answer.text).not.toContain(KEY)
})

test('settings that point at Mezmo before a restart ask for a new session', async ($, on) => {
  const w = world({}, mezmoEnv(KEY))
  stubs(on, w)
  await $.session.start(START)
  const answer = await mezmo($)

  expect(await footer($)).toBe(START_FOOTER)
  expect(w.requests.length).toBe(1)
  expect(w.requests[0]?.headers?.apikey).toBe(KEY)
  expect(answer.text).toContain('Start a new session to begin exporting.')
})

test('nothing shows and nothing is sent when Mezmo is not configured', async ($, on) => {
  const w = world({}, { OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector.example.com' })
  stubs(on, w)
  await $.session.start(START)
  const answer = await mezmo($)

  expect(await footer($)).toBe('')
  expect(w.requests).toEqual([])
  expect(answer.text).toBe("Mezmo isn't set up. Run /mezmo:setup to send Claude Code telemetry to Mezmo.")
})

test('a rejected key shows in the footer and in /mezmo', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w, 401)
  await $.session.start(START)
  const answer = await mezmo($)

  expect(await footer($)).toBe('Mezmo · Telemetry ✗ key rejected')
  expect(answer.text).toContain('Mezmo rejected the ingestion key (HTTP 401)')
  expect(answer.text).not.toContain(KEY)
})

test('a network failure shows Mezmo as unreachable', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w, 'offline')
  await $.session.start(START)
  const answer = await mezmo($)

  expect(await footer($)).toBe('Mezmo · Telemetry ✗ unreachable')
  expect(answer.text).toContain("couldn't reach Mezmo")
})

test('/mezmo:setup mid-session asks for a new session even though the environment changed', async ($, on) => {
  const w = world({}, {})
  stubs(on, w)
  await $.session.start(START)
  expect(await footer($)).toBe('')

  // What /mezmo:setup does: write the settings file, which Claude Code copies into its environment.
  Object.assign(w.env, mezmoEnv(KEY))
  w.files[SETTINGS_FILE] = settingsText(mezmoEnv(KEY))
  await $.classic.ConfigChange({ source: 'user_settings', file_path: SETTINGS_FILE })
  const answer = await mezmo($)

  expect(await footer($)).toBe(START_FOOTER)
  expect(answer.text).not.toContain('This session exports')
  expect(w.requests.length).toBe(1)
})

test('a new key in settings is checked once, and this session keeps its own', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w)
  await $.session.start(START)
  w.files[SETTINGS_FILE] = settingsText(mezmoEnv(OTHER_KEY))
  await $.classic.ConfigChange({ source: 'user_settings' })
  const answer = await mezmo($)

  expect(await footer($)).toBe('Mezmo · Telemetry ✓')
  expect(answer.text).toContain('Your user settings now hold ingestion key wxyz…9999; a new session will use it.')
  // Only this session's key matters while it exports, so the new key isn't sent.
  expect(w.requests.map(r => r.headers?.apikey)).toEqual([KEY])
})

test('the check does not hold up the session or the footer', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w, 401, ms => clock.sleep(ms))
  await $.session.start(START)

  expect(await footer($)).toBe('Mezmo · Telemetry ✓')
  await clock.advance(1000)
  const answer = await mezmo($)
  expect(await footer($)).toBe('Mezmo · Telemetry ✗ key rejected')
  expect(answer.text).toContain('HTTP 401')
  expect(w.requests.length).toBe(1)
})

test('/mezmo answers as text where nothing draws', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w)
  await $.session.start({ surface: null, isInteractive: false, cwd: '/work' })
  const answer = await mezmo($)

  expect(answer.text).toContain('Mezmo accepted the key')
})

test('reads the settings file under CLAUDE_CONFIG_DIR', async ($, on) => {
  const w = world({ CLAUDE_CONFIG_DIR: '/work/claude-config' }, {})
  w.files['/work/claude-config/settings.json'] = settingsText(mezmoEnv(KEY))
  stubs(on, w)
  await $.session.start(START)
  await mezmo($)

  expect(await footer($)).toBe(START_FOOTER)
})

test('a missing settings file means Mezmo is not configured', async ($, on) => {
  const w = world({}, {})
  delete w.files[SETTINGS_FILE]
  stubs(on, w)
  await $.session.start(START)
  const answer = await mezmo($)

  expect(await footer($)).toBe('')
  expect(answer.text).toContain("Mezmo isn't set up.")
})

test('a half-written settings file keeps the last reading', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  stubs(on, w)
  await $.session.start(START)
  w.files[SETTINGS_FILE] = '{"env": {"OTEL_EXP'
  await $.classic.ConfigChange({ source: 'user_settings', file_path: SETTINGS_FILE })
  const answer = await mezmo($)

  expect(await footer($)).toBe('Mezmo · Telemetry ✓')
  expect(answer.text).not.toContain('no longer point at Mezmo')
})

// Mezmo Governance: the status file governance_hook.py writes, and what the mod makes of it.

const GOV_FILE = '/home/me/.mezmo/governance/sessions/session-1.json'
const REASON = 'No rule in this policy covers this request.'

function govState(over: Partial<GovernanceState> = {}): GovernanceState {
  return {
    counts: { allowed: 0, denied: 0, expired: 0, errors: 0 },
    pending: {},
    host: 'api.use.dev.mezmo.it',
    ...over,
  }
}

function lastDecision(over: Partial<LastDecision> = {}): LastDecision {
  return {
    ts: '2026-10-15T22:00:00+00:00',
    decision_id: 'd-1',
    tool: 'claude_code/Bash',
    tool_name: 'Bash',
    outcome: 'allowed',
    detail: null,
    by_person: false,
    ...over,
  }
}

function pendingEntry(sinceMs: number, reason: string | null = REASON) {
  return { tool: 'claude_code/Bash', tool_name: 'Bash', reason, since_ms: sinceMs, deadline_ms: sinceMs + 590_000 }
}

test('finds the status file the way governance_hook.py writes it', () => {
  expect(governanceStatePath('/home/me', 'session-1')).toBe(GOV_FILE)
  expect(governanceStatePath('/home/me/', 'a/b c')).toBe('/home/me/.mezmo/governance/sessions/a_b_c.json')
  expect(governanceStatePath(undefined, 'session-1')).toBeUndefined()
  expect(governanceStatePath('/home/me', undefined)).toBeUndefined()
})

test('reads a status file, and nothing that is not one', () => {
  const state = parseGovernanceState(JSON.stringify({
    counts: { allowed: 2, denied: 'x' },
    pending: { 'd-1': pendingEntry(5), bad: 7 },
    last: lastDecision(),
    host: 'api.mezmo.com',
  }))
  expect(state?.counts).toEqual({ allowed: 2, denied: 0, expired: 0, errors: 0 })
  expect(Object.keys(state?.pending ?? {})).toEqual(['d-1'])
  expect(state?.last?.decision_id).toBe('d-1')
  expect(parseGovernanceState('{"counts": ')).toBeUndefined()
  expect(parseGovernanceState('[]')).toBeUndefined()
})

test('summarizes a call from its arguments', () => {
  expect(summarizeCall('Bash', { command: 'git push origin main\necho done' })).toBe('git push origin main')
  expect(summarizeCall('Read', { file_path: '/work/README.md' })).toBe('/work/README.md')
  expect(summarizeCall('WebFetch', { url: 'https://example.com', prompt: 'x' })).toBe('https://example.com')
  expect(summarizeCall('Bash', { command: 'x'.repeat(80) })).toBe('x'.repeat(59) + '…')
  expect(summarizeCall('mcp__k8s__pods_list', {})).toBe('')
})

test('formats durations as minutes and seconds', () => {
  expect(formatDuration(72_400)).toBe('1:12')
  expect(formatDuration(5_000)).toBe('0:05')
  expect(formatDuration(-1)).toBe('0:00')
})

test('the band names the waiting call, how long it waited and its reason', () => {
  const state = govState({ pending: { 'd-1': pendingEntry(1_000) } })
  const lines = bandLines(state, [{ tool: 'Bash', summary: 'git push origin main' }], 73_000)
  expect(lines).toEqual([
    { text: '⏳ Mezmo Governance · waiting for approval · Bash: git push origin main · 1:12, blocked in 8:38', tone: 'waiting' },
    { text: `   ${REASON}`, tone: 'note' },
  ])
  const mcp = govState({ pending: { 'd-2': { ...pendingEntry(0, null), tool: 'k8s/pods_delete', tool_name: 'mcp__k8s__pods_delete' } } })
  expect(bandLines(mcp, [], 0).map(l => l.text)).toEqual(['⏳ Mezmo Governance · waiting for approval · k8s/pods_delete · 0:00, blocked in 9:50'])
  expect(bandLines(undefined, [], 0)).toEqual([])
})

test('a pending entry only counts while a call is in flight and before its deadline', () => {
  const state = govState({ pending: { 'd-1': pendingEntry(0) } })
  expect(Object.keys(livePending(state, true, 1_000)!.pending)).toEqual(['d-1'])
  expect(livePending(state, false, 1_000)!.pending).toEqual({})
  expect(livePending(state, true, 600_000)!.pending).toEqual({})
})

test('the footer label says what is waiting, or counts the session', () => {
  expect(governanceLabel(undefined)).toBeUndefined()
  expect(governanceLabel(govState())).toBeUndefined()
  expect(governanceLabel(govState({ counts: { allowed: 14, denied: 0, expired: 0, errors: 0 } }))).toBe('Governance 14 ✓')
  expect(governanceLabel(govState({ counts: { allowed: 14, denied: 1, expired: 1, errors: 0 } }))).toBe('Governance 14 ✓ 2 ✗')
  expect(governanceLabel(govState({ pending: { 'd-1': pendingEntry(0) } }))).toBe('Governance ⏳ 1 waiting')
})

test('the footer names Mezmo once, governance first, and leaves out what is off', () => {
  expect(footerLabel([undefined, undefined])).toBeUndefined()
  expect(footerLabel(['Governance 14 ✓', undefined])).toBe('Mezmo · Governance 14 ✓')
  expect(footerLabel([undefined, 'Telemetry ✓'])).toBe('Mezmo · Telemetry ✓')
  expect(footerLabel(['Governance 14 ✓ 1 ✗', 'Telemetry ✗ key rejected'])).toBe('Mezmo · Governance 14 ✓ 1 ✗ · Telemetry ✗ key rejected')
})

test('only a person decides with a toast', () => {
  expect(decisionToast(lastDecision())).toBeUndefined()
  expect(decisionToast(lastDecision({ by_person: true, detail: 'looks fine' }), 'git push origin main'))
    .toBe('✓ Approved in Mezmo Governance: Bash: git push origin main (looks fine)')
  expect(decisionToast(lastDecision({ by_person: true, outcome: 'denied', detail: 'not on main' })))
    .toBe('✗ Denied in Mezmo Governance: Bash (not on main)')
  expect(decisionToast(lastDecision({ by_person: true, outcome: 'expired' })))
    .toBe('✗ Nobody decided in time, so Bash was blocked')
})

// The hooks, with a stand-in for the governance hook writing the status file.

function govStubs(on: On, w: World, toasts: string[]) {
  stubs(on, w)
  on('session.id', () => ({ value: 'session-1' }))
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
}

const BAND_PROPS = {
  hasSurvey: false,
  isWorking: true,
  maxRows: 10,
  bodyColumns: 200,
  scroll: { offset: 0, bodyRows: 10 },
  view: {},
}

async function band($: Engine): Promise<string[]> {
  const ui = await $.ui.mount({ plugin: 'mezmo', surface: 'terminal', component: 'AbovePrompt', props: BAND_PROPS })
  const found = await ui.findAll({ type: 'Text' })
  await ui.unmount()
  return found.map(t => t.text)
}

function writeState(w: World, state: GovernanceState) {
  w.files[GOV_FILE] = JSON.stringify(state)
}

test('a call waiting for a person shows in the band, ticks, and toasts the decision', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  const toasts: string[] = []
  govStubs(on, w, toasts)
  // The tool: the governance hook records the pending decision, a person approves 10 s later.
  on('tool.call', async () => {
    writeState(w, govState({ pending: { 'd-1': pendingEntry(clock.now()) } }))
    await clock.sleep(10_000)
    writeState(w, govState({
      counts: { allowed: 1, denied: 0, expired: 0, errors: 0 },
      last: lastDecision({ by_person: true, detail: 'looks fine' }),
    }))
    return { result: 'pushed' }
  })
  await $.session.start(START)
  const call = $.tool.call({ tool: 'Bash', command: 'git push origin main' })

  await clock.advance(2_000)
  expect(await band($)).toEqual([
    '⏳ Mezmo Governance · waiting for approval · Bash: git push origin main · 0:02, blocked in 9:48',
    `   ${REASON}`,
  ])
  expect(await footer($)).toBe('Mezmo · Governance ⏳ 1 waiting · Telemetry ✓')
  await clock.advance(3_000)
  expect((await band($))[0]).toContain('· 0:05, blocked in 9:45')

  await clock.advance(5_000)
  const result = await call
  expect(result.result).toBe('pushed')
  expect(toasts).toEqual(['✓ Approved in Mezmo Governance: Bash: git push origin main (looks fine)'])
  expect(await band($)).toEqual([])
  expect(await footer($)).toBe('Mezmo · Governance 1 ✓ · Telemetry ✓')
  const answer = await mezmo($)
  expect(answer.text).toContain('Mezmo Governance (api.use.dev.mezmo.it) in this session: 1 allowed, 0 denied, 0 not decided in time, 0 errors.')
  expect(answer.text).toContain('Last check: Bash allowed by a person: looks fine')
  // Governance comes first, then telemetry.
  const text = answer.text ?? ''
  expect(text.indexOf('Mezmo Governance')).toBeLessThan(text.indexOf('This session exports telemetry'))
})

test('decisions the policy makes count in the footer without a toast', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  const toasts: string[] = []
  mock.clock(on)
  govStubs(on, w, toasts)
  on('tool.call', async () => {
    writeState(w, govState({
      counts: { allowed: 3, denied: 1, expired: 0, errors: 0 },
      last: lastDecision({ outcome: 'denied', detail: 'shell is not allowed' }),
    }))
    return { result: 'ok' }
  })
  await $.session.start(START)
  await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(toasts).toEqual([])
  expect(await footer($)).toBe('Mezmo · Governance 3 ✓ 1 ✗ · Telemetry ✓')
  expect(await band($)).toEqual([])
})

test('with governance off nothing about it shows, and calls run as usual', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  const toasts: string[] = []
  govStubs(on, w, toasts)
  on('tool.call', async () => ({ result: 'ok' }))
  await $.session.start(START)
  const result = await $.tool.call({ tool: 'Read', file_path: '/work/README.md' })

  expect(result.result).toBe('ok')
  expect(await footer($)).toBe('Mezmo · Telemetry ✓')
  expect(await band($)).toEqual([])
  expect((await mezmo($)).text).not.toContain('Governance')
  expect(toasts).toEqual([])
})

test('a pending entry left by a stopped hook does not show', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  const toasts: string[] = []
  mock.clock(on)
  govStubs(on, w, toasts)
  writeState(w, govState({ counts: { allowed: 2, denied: 0, expired: 0, errors: 0 }, pending: { 'd-9': pendingEntry(0) } }))
  await $.session.start(START)

  expect(await band($)).toEqual([])
  expect(await footer($)).toBe('Mezmo · Governance 2 ✓ · Telemetry ✓')
})

test('a resumed session does not toast a decision made before it started', async ($, on) => {
  const w = world(mezmoEnv(KEY), mezmoEnv(KEY))
  const toasts: string[] = []
  govStubs(on, w, toasts)
  writeState(w, govState({ counts: { allowed: 1, denied: 0, expired: 0, errors: 0 }, last: lastDecision({ by_person: true }) }))
  mock.clock(on)
  on('tool.call', async () => ({ result: 'ok' }))
  await $.session.start(START)
  await $.tool.call({ tool: 'Read', file_path: '/work/README.md' })

  expect(toasts).toEqual([])
})
