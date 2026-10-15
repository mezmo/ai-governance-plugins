.PHONY: test test-codex test-claude-code test-cursor test-pi test-opencode test-langchain test-mastra test-tools

PYTHON ?= python3
NODE ?= node

test: test-codex test-claude-code test-cursor test-pi test-opencode test-langchain test-mastra test-tools

test-codex:
	cd codex && $(PYTHON) -m unittest test_hook.py -v

test-claude-code:
	cd claude-code && $(PYTHON) -m unittest test_hook.py -v

test-cursor:
	cd cursor && $(PYTHON) -m unittest test_hook.py -v

test-pi:
	cd pi && $(NODE) --test test/*.test.ts

test-opencode:
	cd opencode && $(NODE) --test test/*.test.ts

test-langchain:
	cd langchain && PYTHONPATH=. $(if $(wildcard langchain/.venv/bin/python),$(CURDIR)/langchain/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v

test-mastra:
	cd mastra && $(NODE) --test test/*.test.ts

test-tools:
	$(PYTHON) -m unittest discover -s tools -p 'test_*.py' -v
