PY := .venv/bin/python

.PHONY: install dev test lint demo demo-live

install:          # Python 3.12 venv with dependencies (uv downloads Python if needed)
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[dev]"

dev:              # run the Telegram bot locally (needs BOT_TOKEN and PORTKEY_API_KEY)
	$(PY) -m kurator.bot.main

test:             # full test suite, no network
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff format kurator tests
	$(PY) -m ruff check --fix kurator tests

demo:             # one full homework cycle in the terminal, recorded model answers, no key needed
	$(PY) -m kurator.demo

demo-live:        # the same cycle with real gpt-6-luna calls via Portkey, prints token usage
	$(PY) -m kurator.demo --live
