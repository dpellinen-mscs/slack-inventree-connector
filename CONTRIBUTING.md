# Contributing to stockthread

stockthread is in early development. The design is in
[`docs/design.md`](docs/design.md), and the plan is in
[`ROADMAP.md`](ROADMAP.md).

## Set up and run the checks

You need Python 3.11 or newer and `make`. Everything installs into a
project-local `.venv`.

```sh
make install   # create .venv and install stockthread with its dev tools
make check     # format check, lint, type check, unit tests, eval corpus tests
```

`make check` is the gate that CI runs. Run it before you push.
`make format` fixes formatting and the lint findings that ruff can fix.

## Ground rules

- A wrong stock write is worse than no write. Code that can write to
  InvenTree needs tests for the failure paths, not only the happy path.
- Never weaken or skip a test to get green. Fix the code, or explain why
  the test is wrong.
- Use only synthetic data in examples, tests and fixtures: the people,
  places and parts in [`eval/catalog/`](eval/catalog/). Never commit real
  inventory data, names or credentials.
- Significant choices get a decision record in
  [`docs/decisions/`](docs/decisions/).
- By contributing, you agree that your contribution is licensed under the
  project's [MIT license](LICENSE).
