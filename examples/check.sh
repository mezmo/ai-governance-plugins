#!/bin/sh
# Run an example agent with a real model against the local mock.
# Usage: examples/check.sh mastra|langchain|openai-agents|claude-agent-sdk
# (mastra, langchain and openai-agents need OPENAI_API_KEY, or MODEL plus that
# provider's key; claude-agent-sdk needs Claude Code credentials, such as
# ANTHROPIC_API_KEY)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
example=${1:?usage: check.sh mastra|langchain|openai-agents|claude-agent-sdk}
work=$(mktemp -d)
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
port=$(free_port)

python3 -u "$here/../tools/mock_governance.py" --port "$port" --deny 'delete_*' > "$work/mock.log" 2>&1 &
mock=$!
# wait reports the killed mock as 143; under set -e that would replace the exit status.
trap 'kill $mock 2>/dev/null; wait $mock 2>/dev/null || true; rm -rf "$work"' EXIT
until grep -q "Mock Mezmo Governance" "$work/mock.log" 2>/dev/null; do
  kill -0 $mock 2>/dev/null || { cat "$work/mock.log"; exit 1; }
  sleep 0.2
done

export MEZMO_GOVERNANCE_URL="http://127.0.0.1:$port/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY=local-test
export MEZMO_GOVERNANCE_AUDIT_LOG="$work/audit.jsonl"
# Nothing listens on this port, so every call there must be blocked.
export DOWN_URL="http://127.0.0.1:$(free_port)/governance/workflow/authorize"

cd "$here/$example" 2>/dev/null || { echo "unknown example: $example" >&2; exit 2; }
case $example in
  mastra)
    [ -d node_modules ] || npm install --no-audit --no-fund --loglevel=error
    status=0; node check.ts || status=$? ;;
  langchain|openai-agents|claude-agent-sdk)
    [ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
    status=0; .venv/bin/python check.py || status=$? ;;
  *) echo "unknown example: $example" >&2; exit 2 ;;
esac

echo "--- mock decisions"; tail -n +5 "$work/mock.log"
[ -f "$work/audit.jsonl" ] && echo "--- audit lines: $(wc -l < "$work/audit.jsonl" | tr -d " ")"
exit $status
