.PHONY: test test-codex test-claude-code test-cursor test-pi test-opencode test-langchain

PYTHON ?= python3
NODE ?= node

test: test-codex test-claude-code test-cursor test-pi test-opencode test-langchain

test-codex:
	cd codex && $(PYTHON) -m unittest test_hook.py -v

test-claude-code:
	cd claude-code && $(PYTHON) -m unittest test_hook.py -v

test-cursor:
	cd cursor && $(PYTHON) -m unittest test_hook.py -v

test-pi:
	cd pi && $(NODE) --test test/client.test.ts

test-opencode:
	cd opencode && $(NODE) --test test/client.test.ts

test-langchain:
	cd langchain && PYTHONPATH=. $(if $(wildcard langchain/.venv/bin/python),$(CURDIR)/langchain/.venv/bin/python,$(PYTHON)) -m unittest discover -s tests -p 'test_*.py' -v
