# Examples: Mastra, LangChain, the OpenAI Agents SDK and the Claude Agent SDK with a real model

Four small agent projects that use the plugins the way an application would.
`check.sh` starts `tools/mock_governance.py` with `--deny 'delete_*'`, runs the
agent against a real model, and exits 1 if the gate let the wrong tool run.

```bash
export OPENAI_API_KEY=...            # Mastra, LangChain, OpenAI Agents SDK
examples/check.sh mastra             # needs Node 22.18+; runs npm install on first use
examples/check.sh langchain          # needs Python 3.10+; makes .venv on first use
examples/check.sh openai-agents      # needs Python 3.10+; makes .venv on first use
examples/check.sh claude-agent-sdk   # needs Python 3.10+ and Claude Code credentials, such as ANTHROPIC_API_KEY
```

`check.sh` makes the venv with `python3`, which must be 3.10 or later. If a
first run failed, delete `examples/<name>/.venv` (or
`examples/mastra/node_modules`) and run again.

A passing run prints three `pass` lines, then the mock's decisions, and exits
0. A `FAIL` line and exit 1 mean the gate let the wrong tool run, or the model
skipped a tool.

Each example asks the model to call `read_note` and then `delete_note`, three
times:

| Run | Mastra | LangChain | OpenAI Agents SDK | Claude Agent SDK | Expected |
| --- | --- | --- | --- | --- | --- |
| 1 | `agent.generate` | `agent.invoke` | `Runner.run_sync` | `query` | `read_note` runs, `delete_note` is denied |
| 2 | `agent.stream` | `agent.ainvoke` | `Runner.run_streamed` | `ClaudeSDKClient` | `read_note` runs, `delete_note` is denied |
| 3 | `agent.generate`, Mezmo unreachable | `agent.ainvoke`, Mezmo unreachable | `Runner.run`, Mezmo unreachable | `query`, Mezmo unreachable | neither runs |

A run passes only if the model asked for both tools, so a model that skips a
tool cannot make it pass. Each run makes one or two model calls with
`gpt-5-mini` by default, or `claude-haiku-5-5` for the Claude Agent SDK.

The Claude Agent SDK example runs in `bypassPermissions` mode with no built-in
tools and `setting_sources=[]`, so only the hook can stop a tool and your own
Claude Code settings do not load. Its prompt also says to call `delete_note`
when `read_note` fails: without that, in run 3 the model retried `read_note`
and never asked for `delete_note`.

To use another model, set `MODEL`. Mastra takes a `provider/model` id, such
as `openrouter/openai/gpt-5-mini` with `OPENROUTER_API_KEY`. LangChain uses
`ChatOpenAI` and the OpenAI Agents SDK example uses the Responses API, so
set `OPENAI_BASE_URL` and `OPENAI_API_KEY` for any compatible provider. The
OpenAI Agents SDK example turns tracing off, so tool arguments do not reach
OpenAI's traces dashboard. The Claude Agent SDK takes a Claude model id.

Tested on 2026-10-15 with `@mastra/core` 1.77.0, `langchain` 1.4.6 and
`langchain-openai` 1.7.2, `openai-agents` 0.24.0, all with `gpt-5-mini`, and
with `claude-agent-sdk` 0.2.168 and `claude-haiku-5-5`: every run passed.
With a mock that denies nothing, runs 1 and 2 fail, which shows the check can
fail.

Adapter docs: [mastra/](../mastra/), [langchain/](../langchain/),
[openai-agents/](../openai-agents/) and [claude-agent-sdk/](../claude-agent-sdk/).
Pi and OpenCode have no example here: use `./mezmo-governance smoke`.
