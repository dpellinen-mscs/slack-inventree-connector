# The local gate. `make check` runs everything CI runs, except the Docker integration tests.
PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install check format lint types test eval clean

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip

install: $(BIN)/python
	$(BIN)/pip install --quiet -e ".[dev]"

check: lint types test eval

format:
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

lint:
	$(BIN)/ruff format --check src tests
	$(BIN)/ruff check src tests

types:
	$(BIN)/mypy

test:
	$(BIN)/pytest

eval:
	$(BIN)/python eval/tools/validate_corpus.py
	$(BIN)/python -m unittest discover -s eval/tools -p "test_*.py"

clean:
	rm -rf $(VENV) .mypy_cache .pytest_cache .ruff_cache
