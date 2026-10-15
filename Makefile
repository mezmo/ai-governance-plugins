.PHONY: test test-codex test-claude-code test-cursor test-gemini-cli test-copilot test-pi test-opencode test-langchain test-mastra test-claude-agent-sdk test-tools

PYTHON ?= python3
NODE ?= node

test: test-codex test-claude-code test-cursor test-gemini-cli test-copilot test-pi test-opencode test-langchain test-mastra test-claude-agent-sdk test-tools

test-codex:
	cd codex && $(PYTHON) -m unittest test_hook.py -v

test-claude-code:
	cd claude-code && $(PYTHON) -m unittest test_hook.py -v

test-cursor:
	cd cursor && $(PYTHON) -m unittest test_hook.py -v

test-gemini-cli:
	cd gemini-cli && $(PYTHON) -m unittest test_hook.py -v

test-copilot:
	cd copilot && $(PYTHON) -m unittest test_hook.py -v

test-pi:
	cd pi && $(NODE) --test test/*.test.ts

test-opencode:
	cd opencode && $(NODE) --test test/*.test.ts

test-langchain:
	cd langchain && PYTHONPATH=. $(if $(wildcard langchain/.venv/bin/python),$(CURDIR)/langchain/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v

test-mastra:
	cd mastra && $(NODE) --test test/*.test.ts

test-claude-agent-sdk:
	cd claude-agent-sdk/python && PYTHONPATH=. $(if $(wildcard claude-agent-sdk/python/.venv/bin/python),$(CURDIR)/claude-agent-sdk/python/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v
	cd claude-agent-sdk/typescript && $(NODE) --test test/*.test.ts

test-tools:
	$(PYTHON) -m unittest discover -s tools -p 'test_*.py' -v
