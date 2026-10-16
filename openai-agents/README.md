# OpenAI Agents SDK + Mezmo Governance

`MezmoGovernance` asks Mezmo before each function tool and MCP tool of an
OpenAI Agents SDK agent runs, through `Runner.run`, `Runner.run_sync` and
`Runner.run_streamed`. It is a tool input guardrail: the SDK runs it after the
model asks for a call and before the tool runs. When Mezmo denies the call,
the tool does not run, and the model gets
`Mezmo Governance denied this tool call: <reason>` in place of the tool
result. The run keeps going.

Tested against `openai-agents` 0.24.0: the tests run real `Runner` loops with
the SDK's `ScriptedModel`, and [examples/check.sh](../examples/) runs one with
`gpt-5-mini`.

## Install

Python 3.10+. Install the package into your project's environment:

```bash
pip install ./openai-agents        # from a checkout of this repository
```

To run the tests from this directory instead:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```python
from agents import Agent, RunConfig, Runner, function_tool
from mezmo_governance_openai_agents import MezmoGovernance


@function_tool
def delete_note(name: str) -> bool:
    """Delete a note by name."""
    ...


agent = Agent(name="notes", model="gpt-5-mini", tools=[delete_note])
MezmoGovernance().govern(agent)

result = Runner.run_sync(agent, "Delete the note todo", run_config=RunConfig(group_id="my-session"))
```

`MezmoGovernance()` reads `MEZMO_GOVERNANCE_URL` and
`MEZMO_GOVERNANCE_TOKEN` per call. Pass an endpoint and
`access_key=` to use those instead.

`govern(agent)` adds the guardrail, first in line, to:

- every `FunctionTool` on the agent, including agents wrapped with `as_tool`;
- every MCP server in `mcp_servers`, so each tool the server lists is gated;
- the same, in turn, for each handoff target and each agent behind `as_tool`.

To reach the agent behind `as_tool` or `handoff()`, it reads private SDK
fields. If they are missing, as in a newer SDK, `govern` raises
`GovernanceError` rather than leave that agent's tools ungated.

Call it once the agent's tools are final: a tool or server added later is not
gated. It adds its guardrail to a tool once, so a second `govern` call (or a
second `MezmoGovernance`) leaves the first in place.

To gate one tool by hand, add `MezmoGovernance().guardrail()` to its
`tool_input_guardrails`, or `guardrail(namespace="<server>")` to an MCP
server's.

## What is not gated

The SDK runs tool input guardrails only for function tools, so these kinds
are not gated:

| Tool kind | Where it runs | What `govern` does |
| --- | --- | --- |
| `WebSearchTool`, `FileSearchTool`, `CodeInterpreterTool`, `ImageGenerationTool`, `ToolSearchTool` | on OpenAI's servers; no call reaches your process before it runs | leaves them in place, not gated |
| `HostedMCPTool` | on OpenAI's servers, which call the remote MCP server's tools, side effects included | raises `GovernanceError`; connect the server through `Agent(mcp_servers=[...])`, which is gated |
| `ShellTool`, `LocalShellTool`, `ComputerTool`, `ApplyPatchTool`, `CustomTool` | on your machine, with no guardrail | raises `GovernanceError`, so the agent never runs them ungated |

`ProgrammaticToolCallingTool` stays. Its hosted program calls your function
tools through the runner, and openai-agents 0.24.0 runs their input
guardrails as for a direct call: a test sends a program-owned call to a
denied tool and the tool does not run. Other hosted tools the program
calls are not gated, as above.

For the local kinds, the SDK's `needs_approval` and `on_approval` decide per
call; this package does not wire those to Mezmo.

A handoff itself (the `transfer_to_<agent>` call) is not sent to Mezmo; the
tools of the agent it hands to are. Realtime and voice agents are not tested.

## Fails closed

The tool does not run, and the model gets
`Mezmo Governance could not authorize this tool call, so the OpenAI Agents SDK blocked it: <error>`,
when:

- the URL or key is missing, or a seconds setting is not a finite number above 0;
- Mezmo cannot be reached, returns an HTTP error, or a reply that is not JSON,
  not an object, or has no boolean `approved`;
- a decision is still pending at the deadline, or arrives after it;
- the tool arguments are not a JSON object;
- anything else in the guardrail raises.

If the guardrail crashes outside its own error handling, the SDK raises
`UserError` and stops the run; the tool still does not run.

## Settings

The variables in the [root README](../README.md#settings), except
`MEZMO_GOVERNANCE_AUDIT_LOG`: this package writes no audit file. The
guardrail waits for a decision inside the run for up to
`MEZMO_GOVERNANCE_TIMEOUT_SECONDS` (default and maximum 590).

## What it sends

`request_id` is `openai_agents:<session_id>:<call_id>#<digest>`, and `origin.kind` is
`openai_agents_guardrail`. `session_id` is `RunConfig.group_id`, else the
run's trace id, else one random id per `MezmoGovernance` instance (with
tracing off, every run through that instance shares it). The SDK refuses a
tool call without a call id before the guardrail runs; if one gets through,
it gets a random id, so it never reuses an earlier decision.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)).

### Tool names for policy

| Tool | `tool_namespace` | `tool_name` | Policy |
| --- | --- | --- | --- |
| `@function_tool`, `as_tool` | `openai_agents` | the tool name (for `@function_tool`, the function name) | `openai_agents:<tool>` |
| MCP server tool | the server's `name` | the MCP tool name | `<server>:<tool>` |
| tool in an SDK tool namespace | that namespace | the tool name | `<namespace>:<tool>` |

`arguments` is the JSON object the model sent for the call.

## Verify it works

With the package installed and the two variables set, ask Mezmo about one
call. This prints the decision, or raises `GovernanceError` with the reason
there is none:

```bash
python -c 'import asyncio, uuid
from mezmo_governance_openai_agents import approval_payload, authorize
payload = approval_payload(session_id="verify", tool_call_id=str(uuid.uuid4()),
                           tool_name="read_note", arguments={"name": "todo"})
print(asyncio.run(authorize(payload)))'
```

For a full smoke test with a real model, run
`examples/check.sh openai-agents` from the repository root with
`OPENAI_API_KEY` set. It starts the mock with `--deny 'delete_*'` and fails
unless `read_note` runs and `delete_note` does not, and neither runs when
Mezmo is unreachable.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

`tests/test_client.py` has no SDK dependency. It also checks that
`mezmo_governance_openai_agents/client.py` matches `langchain/`'s copy except
for the agent name, `origin.kind` and `origin.reason`: the copy keeps this
package installable without LangChain. The guardrail tests skip unless the SDK
is installed, so run them with the venv.
