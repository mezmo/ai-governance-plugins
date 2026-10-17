// The values the status mod keeps in $.state. The manifest names this file as the mod's
// types contract, which `claude plugin validate` checks the mod's state against.

/**
 * What one source says about the Mezmo export: this Claude Code process's
 * environment as it started, or the user settings file as it is now. The
 * ingestion key appears only masked (first4…last4).
 */
export type MezmoFacts = {
  /** OTEL_EXPORTER_OTLP_ENDPOINT is https://logs.mezmo.com/otel. */
  isMezmo: boolean
  /** CLAUDE_CODE_ENABLE_TELEMETRY is 1 or true. */
  isEnabled: boolean
  /** OTEL_EXPORTER_OTLP_HEADERS carries an apikey header. */
  hasKey: boolean
  /** The apikey, masked. */
  key: string
}

/** The connection check for one ingestion key (by its masked form). */
export type MezmoCheck = {
  key: string
  outcome: 'running' | 'ok' | 'rejected' | 'unexpected' | 'unreachable'
  status?: number
  reason?: string
}

declare module 'claude-code' {
  interface PluginState {
    mezmo: {
      session: MezmoFacts | null
      settings: MezmoFacts | null
      check: MezmoCheck | null
    }
  }
}
