# LangChain + Mezmo Governance

`MezmoGovernanceMiddleware` wraps every tool call from a LangChain v1
`create_agent` graph, through `invoke`/`stream` and `ainvoke`/`astream`. It POSTs an authorize request, waits for
`{ "approved": true|false }`, and returns a `ToolMessage` with `status="error"`
when the call is denied. Any failure before a decision (Mezmo unreachable, an
HTTP error, bad JSON, missing configuration) also returns an error
`ToolMessage`: the tool does not run, and the agent keeps going.

## Install

```bash
pip install -r requirements.txt
```

```python
from mezmo_governance.middleware import MezmoGovernanceMiddleware
from langchain.agents import create_agent

middleware = MezmoGovernanceMiddleware(
    os.environ["MEZMO_GOVERNANCE_URL"],
    access_key=os.environ["MEZMO_GOVERNANCE_ACCESS_KEY"],
)
agent = create_agent(model=model, tools=tools, middleware=[middleware])
```

Pass a LangGraph `thread_id` so `session_id` is stable across a conversation:

```python
await agent.ainvoke(
    {"messages": [{"role": "user", "content": prompt}]},
    config={"configurable": {"thread_id": "my-session"}},
)
```

Without a `thread_id`, the middleware uses `set_current_turn_id(...)` when you
set one around the call (see [example_agent.py](example_agent.py)). Otherwise
it uses one random session id per middleware instance, so every un-threaded
run through that instance shows as one session in Mezmo. A tool call without
an id gets a random one, so it never reuses an earlier decision.

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
export OPENAI_API_KEY="…"
python3 example_agent.py
```

`request_id` is `langchain:<session_id>:<tool_call_id>`. Tool names
`mcp__<server>__<tool>` use that MCP server as `tool_namespace`; everything else
uses `langchain`.

`intent_provider` is opt-in and off by default. When you pass one, its return
value goes in `items[0].tool_call_intent`. That field is not in
[docs/protocol.md](../docs/protocol.md); use it only with a Mezmo endpoint
that accepts it.

OpenTelemetry is optional. If `opentelemetry` is installed, approved calls are
traced as `mcp.tool_call`.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

`tests/test_client.py` has no LangChain dependency. Middleware tests skip unless
LangChain is installed.
