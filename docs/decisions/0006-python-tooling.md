# 0006: Python tooling: hatchling, ruff, mypy strict, pytest, make

- Status: Accepted
- Date: 2026-09-29
- Decided by: agent (FYI to Dan)

## Context

ADR 0003 picked Python. A contributor should be able to install and run the
full gate with one documented command, on a plain Python install, without
global tools.

## Decision

- Layout: `src/stockthread/` and `tests/`. Python 3.11 or newer. CI tests
  the oldest and the newest supported version (3.11 and 3.13).
- Build backend: hatchling, from `pyproject.toml`.
- Format and lint: ruff. Type check: mypy in strict mode over `src` and
  `tests`. Tests: pytest.
- Development tools are pinned to exact versions in the `dev` extra.
  Runtime dependencies get lower bounds, added with the code that needs
  them.
- The gate is a `Makefile` with a project-local `.venv`: `make install`,
  then `make check`. No uv, Poetry or tox, so a contributor needs only
  Python and make.
- The stdlib scripts in `eval/tools/` keep their own style and are not
  linted by ruff. `make check` runs their tests.

## Consequences

- There is no lock file for the dev tools beyond the exact pins, so a
  transitive dependency can change under CI. Revisit (a hash-pinned
  requirements file or uv) if that ever breaks the gate.
- The Docker image (M1) installs from the same `pyproject.toml`.
