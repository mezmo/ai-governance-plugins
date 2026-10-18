# Security Policy

## Safety boundary

These plugins decide whether an agent may run a tool, so a bug that lets a
denied call run is a vulnerability. So is a crash, a timeout or an unreadable
reply that lets a call run in an agent the README lists as failing closed.

The plugins do not sandbox tools or undo what a tool changed. Mezmo decides
from the tool name and its arguments; what the tool then does is up to the
agent and the machine it runs on.

Known limits, not vulnerabilities:

- Codex, Gemini CLI, Droid and GitHub Copilot run the tool when the hook
  times out (see their READMEs). In Codex, Gemini CLI, Droid, Windsurf and
  VS Code Local, a hook that crashes blocks the call only with the
  `|| exit 2` guard that the example configs carry.
- The Claude Code plugin lets every call run until it has a key. Set
  `MEZMO_GOVERNANCE_REQUIRED=1` to make it deny instead.
- Windsurf has no hook for grep and find, `write_to_file`, web search, URL
  reads, git commit, deploy or subagents, and loads no hook in Restricted Mode.
- Goose runs the tool calls inside Code Mode scripts and `delegate` subagents
  without a hook, so the Goose hook denies every Code Mode script and every
  `delegate` call.
- The OpenAI Agents SDK does not gate hosted tools.
- The token sits in the agent's environment or settings, so a governed agent
  that can read them can call Mezmo with it. Give the agent's service account
  no write access to policies or agents, and keep `MEZMO_GOVERNANCE_ADMIN_KEY`,
  which `./mezmo-governance policy` uses, out of the agents' environment.

## Data handling

Each tool call sends Mezmo the tool name and its full arguments, plus the
session and tool call ids. [What leaves your machine](README.md#what-leaves-your-machine)
has the details, including the LangChain middleware's OpenTelemetry span.

## Reporting a vulnerability

Report suspected vulnerabilities privately to
[security@mezmo.com](mailto:security@mezmo.com). Do not open a public issue or
pull request for one.

Include the plugin, the agent and its version, the smallest hook input or steps
that let a call run that should have been blocked, and the impact you saw.
Never include live tokens or customer data.

Use a public issue only for a hardening request that discloses nothing
sensitive.

## Supported versions

Security fixes target the latest commit on `main`. Update before reporting a
problem that may already be fixed.
