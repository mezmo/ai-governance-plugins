# LangChain + Mezmo Governance

`MezmoGovernanceMiddleware` wraps every tool call from a LangChain v1
`create_agent` graph, through `invoke`/`stream` and `ainvoke`/`astream`. It
POSTs an authorize request, waits for `{ "approved": true|false }`, and
returns a `ToolMessage` with `status="error"` when the call is denied. Any
failure before a decision (Mezmo unreachable, an
HTTP error, bad JSON, missing configuration) also returns an error
`ToolMessage`: the tool does not run, and the agent keeps going.

## Install

Python 3.10+. Install the package into your project's environment:

```bash
pip install ./langchain            # from a checkout of this repository
pip install './langchain[otel]'    # also install OpenTelemetry
```

To run the example and the tests from this directory instead:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```python
import os

from langchain.agents import create_agent
from langchain.tools import tool
from mezmo_governance import MezmoGovernanceMiddleware


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


middleware = MezmoGovernanceMiddleware(
    os.environ["MEZMO_GOVERNANCE_URL"],
    access_key=os.environ["MEZMO_GOVERNANCE_ACCESS_KEY"],
)
agent = create_agent(model="openai:gpt-4.1-mini", tools=[echo], middleware=[middleware])

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Echo hello"}]},
    config={"configurable": {"thread_id": "my-session"}},
)
```

The endpoint and access key you pass win over `MEZMO_GOVERNANCE_URL` and
`MEZMO_GOVERNANCE_ACCESS_KEY`; pass `None` to read those per call. Pass a
LangGraph `thread_id` so `session_id` is stable across a conversation.
`ainvoke` takes the same arguments.

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
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

`tests/test_client.py` has no LangChain dependency. Middleware tests skip unless
LangChain is installed, so run them with the venv.
