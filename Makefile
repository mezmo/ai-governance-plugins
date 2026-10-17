.PHONY: test test-codex test-claude-code test-claude-code-mod test-cursor test-gemini-cli test-copilot test-goose test-pi test-opencode test-langchain test-openai-agents test-mastra test-claude-agent-sdk test-tools

PYTHON ?= python3
NODE ?= node

test: test-codex test-claude-code test-cursor test-gemini-cli test-copilot test-goose test-pi test-opencode test-langchain test-openai-agents test-mastra test-claude-agent-sdk test-tools

test-codex:
	cd codex && $(PYTHON) -m unittest test_hook.py -v

test-claude-code:
	cd claude-code && $(PYTHON) -m unittest discover -s tests -v

# The status display's tests need the claude CLI, so they aren't part of `make test`.
test-claude-code-mod:
	cd claude-code && claude plugin test

test-cursor:
	cd cursor && $(PYTHON) -m unittest test_hook.py -v

test-gemini-cli:
	cd gemini-cli && $(PYTHON) -m unittest test_hook.py -v

test-copilot:
	cd copilot && $(PYTHON) -m unittest test_hook.py -v

test-goose:
	cd goose && $(PYTHON) -m unittest test_hook.py -v

test-pi:
	cd pi && $(NODE) --test test/*.test.ts

test-opencode:
	cd opencode && $(NODE) --test test/*.test.ts

test-langchain:
	cd langchain && PYTHONPATH=. $(if $(wildcard langchain/.venv/bin/python),$(CURDIR)/langchain/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v

test-openai-agents:
	cd openai-agents && PYTHONPATH=. $(if $(wildcard openai-agents/.venv/bin/python),$(CURDIR)/openai-agents/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v

test-mastra:
	cd mastra && $(NODE) --test test/*.test.ts

test-claude-agent-sdk:
	cd claude-agent-sdk/python && PYTHONPATH=. $(if $(wildcard claude-agent-sdk/python/.venv/bin/python),$(CURDIR)/claude-agent-sdk/python/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v
	cd claude-agent-sdk/typescript && $(NODE) --test test/*.test.ts

test-tools:
	$(PYTHON) -m unittest discover -s tools -p 'test_*.py' -v
