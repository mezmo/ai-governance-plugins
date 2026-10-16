.PHONY: test test-codex test-claude-code test-claude-code-mod test-cursor test-pi test-opencode test-langchain test-mastra

PYTHON ?= python3
NODE ?= node

test: test-codex test-claude-code test-cursor test-pi test-opencode test-langchain test-mastra

test-codex:
	cd codex && $(PYTHON) -m unittest test_hook.py -v

test-claude-code:
	cd claude-code && $(PYTHON) -m unittest discover -s tests -v

# The status display's tests need the claude CLI, so they aren't part of `make test`.
test-claude-code-mod:
	cd claude-code && claude plugin test

test-cursor:
	cd cursor && $(PYTHON) -m unittest test_hook.py -v

test-pi:
	cd pi && $(NODE) --test test/client.test.ts

test-opencode:
	cd opencode && $(NODE) --test test/client.test.ts

test-langchain:
	cd langchain && PYTHONPATH=. $(if $(wildcard langchain/.venv/bin/python),$(CURDIR)/langchain/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v

test-mastra:
	cd mastra && $(NODE) --test test/*.test.ts
