# Examples: Mastra, LangChain and the OpenAI Agents SDK with a real model

Three small agent projects that use the plugins the way an application would.
`check.sh` starts `tools/mock_governance.py` with `--deny 'delete_*'`, runs the
agent against a real model, and exits 1 if the gate let the wrong tool run.

```bash
export OPENAI_API_KEY=...
examples/check.sh mastra      # needs Node 22+; runs npm install on first use
examples/check.sh langchain   # needs Python 3.10+; makes .venv on first use
examples/check.sh openai-agents   # needs Python 3.10+; makes .venv on first use
```

Each example asks the model to call `read_note` and then `delete_note`, three
times:

| Run | Mastra | LangChain | OpenAI Agents SDK | Expected |
| --- | --- | --- | --- | --- |
| 1 | `agent.generate` | `agent.invoke` | `Runner.run_sync` | `read_note` runs, `delete_note` is denied |
| 2 | `agent.stream` | `agent.ainvoke` | `Runner.run_streamed` | `read_note` runs, `delete_note` is denied |
| 3 | `agent.generate`, Mezmo unreachable | `agent.ainvoke`, Mezmo unreachable | `Runner.run`, Mezmo unreachable | neither runs |

A run passes only if the model asked for both tools, so a model that skips a
tool cannot make it pass. Each run makes one or two model calls with
`gpt-5-mini` by default.

To use another model, set `MODEL`. Mastra takes a `provider/model` id, such
as `openrouter/openai/gpt-5-mini` with `OPENROUTER_API_KEY`. LangChain uses
`ChatOpenAI` and the OpenAI Agents SDK example uses the Responses API, so
set `OPENAI_BASE_URL` and `OPENAI_API_KEY` for any compatible provider. The
OpenAI Agents SDK example turns tracing off, so tool arguments do not reach
OpenAI's traces dashboard.

Tested on 2026-10-15 with `@mastra/core` 1.77.0, `langchain` 1.4.6 and
`langchain-openai` 1.7.2, with `gpt-5-mini`: every run passed. With a mock
that denies nothing, runs 1 and 2 fail, which shows the check can fail.
`openai-agents` 0.24.0 was checked the same way on 2026-10-15, with the
same results.
