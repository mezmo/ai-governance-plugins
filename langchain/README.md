# LangChain + Mezmo Governance

`MezmoGovernanceMiddleware` wraps every tool call from a LangChain v1
`create_agent` graph, through `invoke`/`stream` and `ainvoke`/`astream`. It
POSTs an authorize request, waits for `{ "approved": true|false }`, and
returns a `ToolMessage` with `status="error"` when the call is denied. Any
failure before a decision (Mezmo unreachable, an HTTP error, bad JSON, missing
configuration) also returns an error `ToolMessage`: the tool does not run, and
the agent keeps going.

Tested against `langchain` 1.4.6 and `langgraph` 1.2.15: the tests run real
`create_agent` graphs with a fake chat model through `invoke`.

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
    access_key=os.environ["MEZMO_GOVERNANCE_TOKEN"],
)
agent = create_agent(model="openai:gpt-4.1-mini", tools=[echo], middleware=[middleware])

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Echo hello"}]},
    config={"configurable": {"thread_id": "my-session"}},
)
```

The endpoint and token you pass win over `MEZMO_GOVERNANCE_URL` and
`MEZMO_GOVERNANCE_TOKEN`; pass `None` to read those per call. Pass a
LangGraph `thread_id` so `session_id` is stable across a conversation.
`ainvoke` takes the same arguments.
The URL must use `https://`; plain `http://` works only to a loopback
address such as `127.0.0.1`, for a local mock. A URL with a password is refused.

Without a `thread_id`, the middleware uses `set_current_turn_id(...)` when you
set one around the call (see [example_agent.py](example_agent.py)). Otherwise
it uses one random session id per middleware instance, so every un-threaded
run through that instance shows as one session in Mezmo. A tool call without
an id gets a random one, so it never reuses an earlier decision.

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
export OPENAI_API_KEY="…"
python3 example_agent.py
```

## Verify it works

With the package installed and the two variables set, ask Mezmo about one
call. This prints the decision, or raises `GovernanceError` with the reason
there is none:

```bash
python -c 'import asyncio, uuid
from mezmo_governance import approval_payload, authorize
payload = approval_payload(session_id="verify", tool_call_id=str(uuid.uuid4()),
                           tool_name="echo", arguments={"text": "hi"})
print(asyncio.run(authorize(payload)))'
```

To check fail-closed in your agent, set
`MEZMO_GOVERNANCE_URL=http://127.0.0.1:9/authorize` and run a prompt that
calls a tool: the tool message must say `could not authorize`, and the tool
must not run.

## What it sends

`request_id` is `langchain:<session_id>:<tool_call_id>#<digest>`.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). Tool names
`mcp__<server>__<tool>` use that MCP server as `tool_namespace`; everything else
uses `langchain`.

### Tool names for policy

`tool_name` is the LangChain tool's `name` (for a `@tool` function, the
function name), in namespace `langchain`. Write policy for
`langchain:<tool name>`.

`intent_provider` is opt-in and off by default. When you pass one, its return
value goes in `items[0].tool_call_intent`, cut at Mezmo's limit of 2048
characters. See [docs/protocol.md](../docs/protocol.md).

OpenTelemetry is optional. If `opentelemetry` is installed, approved calls are
traced as `mcp.tool_call` spans. Each span carries `tool.name`,
`tool.parameters` (the full arguments as JSON) and `tool.result` (up to 8,000
characters of the result). With only `opentelemetry-api` installed the spans
are no-ops. Once the app sets up an OpenTelemetry SDK with an exporter, that
exporter sends them, results included, wherever it points. The middleware traces
whenever `opentelemetry` can be imported and has no switch to leave these
attributes out, so treat the trace backend as seeing every argument and result.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

`tests/test_client.py` has no LangChain dependency. Middleware tests skip unless
LangChain is installed, so run them with the venv.
