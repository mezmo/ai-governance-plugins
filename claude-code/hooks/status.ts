// The mod's status logic as plain functions, shared by the hooks and the tests.
// It mirrors scripts/mezmo_common.py and `claude_code_otel.py status`.

import type { MezmoCheck, MezmoFacts } from './state'

export const MEZMO_OTEL_ENDPOINT = 'https://logs.mezmo.com/otel'
// Mezmo answers an empty JSON object here with 207 and publishes nothing.
export const CHECK_URL = `${MEZMO_OTEL_ENDPOINT}/v1/logs`

/** The variables that decide where Claude Code exports, from the process or a settings file's `env`. */
export type OtelEnv = {
  enable?: string
  endpoint?: string
  headers?: string
}

export type View = {
  /** The telemetry part of the footer label, or undefined to show nothing. */
  label?: string
  /** What /mezmo prints. */
  text: string
}

export function isMezmoEndpoint(value?: string | null): boolean {
  return !!value && value.trim().replace(/\/+$/, '').toLowerCase() === MEZMO_OTEL_ENDPOINT
}

function decode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

/** Parse OTEL_EXPORTER_OTLP_HEADERS ("k1=v1,k2=v2", values percent-encoded). */
export function parseOtlpHeaders(value?: string | null): Record<string, string> {
  const headers: Record<string, string> = {}
  for (const item of (value ?? '').split(',')) {
    const at = item.indexOf('=')
    const name = item.slice(0, at).trim()
    if (at < 0 || !name) continue
    headers[name] = decode(item.slice(at + 1).trim())
  }
  return headers
}

/** Show only the first and last four characters of a secret. */
export function mask(secret?: string | null): string {
  if (!secret) return '(not set)'
  if (secret.length < 12) return '****'
  return `${secret.slice(0, 4)}…${secret.slice(-4)}`
}

/** The ingestion key the env sends, when it exports to Mezmo. */
export function keyOf(env: OtelEnv): string | undefined {
  if (!isMezmoEndpoint(env.endpoint)) return undefined
  return parseOtlpHeaders(env.headers).apikey || undefined
}

export function factsOf(env: OtelEnv): MezmoFacts {
  const key = keyOf(env)
  return {
    isMezmo: isMezmoEndpoint(env.endpoint),
    isEnabled: env.enable === '1' || env.enable === 'true',
    hasKey: !!key,
    key: mask(key),
  }
}

/** The user settings file the scripts write: $CLAUDE_CONFIG_DIR/settings.json, else ~/.claude/settings.json. */
export function settingsPath(configDir?: string, home?: string): string | undefined {
  if (configDir) {
    const dir = home && (configDir === '~' || configDir.startsWith('~/')) ? home + configDir.slice(1) : configDir
    return `${dir.replace(/\/+$/, '')}/settings.json`
  }
  return home ? `${home.replace(/\/+$/, '')}/.claude/settings.json` : undefined
}

/** A settings file's text as an object: {} when blank, undefined when it isn't a JSON object. */
export function parseSettings(text: string): Record<string, unknown> | undefined {
  if (!text.trim()) return {}
  try {
    const value: unknown = JSON.parse(text)
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

/** The OTel variables in the `env` block of a settings object. */
export function otelEnvOf(settings: unknown): OtelEnv {
  const env = (settings as { env?: unknown } | null)?.env
  if (!env || typeof env !== 'object') return {}
  const text = (name: string) => {
    const value = (env as Record<string, unknown>)[name]
    return typeof value === 'string' ? value : undefined
  }
  return {
    enable: text('CLAUDE_CODE_ENABLE_TELEMETRY'),
    endpoint: text('OTEL_EXPORTER_OTLP_ENDPOINT'),
    headers: text('OTEL_EXPORTER_OTLP_HEADERS'),
  }
}

export function isExporting(facts: MezmoFacts): boolean {
  return facts.isMezmo && facts.isEnabled
}

export function outcomeOf(status: number): MezmoCheck['outcome'] {
  if (status === 207) return 'ok'
  if (status === 401 || status === 403) return 'rejected'
  return 'unexpected'
}

function checkLine(check: MezmoCheck): string | undefined {
  switch (check.outcome) {
    case 'ok':
      return `Connection check: Mezmo accepted the key (HTTP ${check.status}). Nothing was published.`
    case 'rejected':
      return `Connection check: Mezmo rejected the ingestion key (HTTP ${check.status}). Check it under Settings > API Keys in Mezmo, then run /mezmo:setup.`
    case 'unexpected':
      return (check.status ?? 0) >= 500
        ? `Connection check: Mezmo returned a server error (HTTP ${check.status}). Try again in a few minutes.`
        : `Connection check: unexpected response from Mezmo (HTTP ${check.status}).`
    case 'unreachable':
      return `Connection check: couldn't reach Mezmo${check.reason ? ` (${check.reason})` : ''}.`
    case 'running':
      return 'Connection check: running…'
  }
}

function failureLabel(check: MezmoCheck): string | undefined {
  switch (check.outcome) {
    case 'rejected':
      return 'Telemetry ✗ key rejected'
    case 'unreachable':
      return 'Telemetry ✗ unreachable'
    case 'unexpected':
      return `Telemetry ✗ HTTP ${check.status}`
  }
  return undefined
}

/**
 * Compare what this session started with against what the settings file says
 * now, together with the connection check for the key that matters: the one
 * this session exports with, else the one the next session will use.
 */
export function describe(session: MezmoFacts, settings: MezmoFacts, check: MezmoCheck | null): View {
  const exporting = isExporting(session)
  if (!exporting && !settings.isMezmo) {
    return {
      text: "Mezmo telemetry is off. Run /mezmo:setup telemetry to send Claude Code's metrics, events and traces to Mezmo.",
    }
  }

  const lines: string[] = []
  let label: string
  if (exporting) {
    if (session.hasKey) {
      label = 'Telemetry ✓'
      lines.push(`This session exports telemetry to ${MEZMO_OTEL_ENDPOINT} (ingestion key ${session.key}).`)
    } else {
      label = 'Telemetry ✗ no ingestion key'
      lines.push(
        `This session exports to ${MEZMO_OTEL_ENDPOINT}, but OTEL_EXPORTER_OTLP_HEADERS has no apikey header, so Mezmo rejects the data. Run /mezmo:setup, then start a new session.`,
      )
    }
    if (!settings.isMezmo) {
      lines.push("Your user settings no longer point at Mezmo, so the next session won't export.")
    } else if (settings.key !== session.key) {
      lines.push(`Your user settings now hold ingestion key ${settings.key}; a new session will use it.`)
    }
  } else if (settings.isEnabled && settings.hasKey) {
    label = 'Telemetry: start a new session to begin exporting'
    lines.push(
      `Your user settings send telemetry to Mezmo (ingestion key ${settings.key}), but this session started without them. Start a new session to begin exporting.`,
    )
  } else {
    label = 'Telemetry ✗ setup incomplete'
    const missing = [
      settings.isEnabled ? undefined : 'CLAUDE_CODE_ENABLE_TELEMETRY is not 1',
      settings.hasKey ? undefined : 'OTEL_EXPORTER_OTLP_HEADERS has no apikey header',
    ].filter(Boolean)
    lines.push(`Your user settings point at Mezmo, but ${missing.join(' and ')}. Run /mezmo:setup again.`)
  }

  const key = exporting ? session.key : settings.key
  const result = check?.key === key ? check : null
  if (result) {
    label = failureLabel(result) ?? label
    lines.push(checkLine(result) ?? '')
  }
  return { label, text: lines.filter(Boolean).join('\n') }
}

/** An error's message, short, with any copy of the key masked. */
export function reasonOf(error: unknown, key: string): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.split(key).join(mask(key)).slice(0, 160)
}

/** The one footer label: Mezmo, then each part that has something to say. */
export function footerLabel(parts: readonly (string | undefined)[]): string | undefined {
  const shown = parts.filter((part): part is string => !!part)
  return shown.length ? ['Mezmo', ...shown].join(' · ') : undefined
}
