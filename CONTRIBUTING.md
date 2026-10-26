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

### Integration tests

`make check` skips the tests in `tests/integration/`. They need a
disposable InvenTree server, which CI starts for every pinned version.
To run them locally you need Docker:

```sh
tests/integration/start-inventree.sh inventree/inventree:1.5.6 > .it.env
set -a; . ./.it.env; set +a
.venv/bin/pytest tests/integration
tests/integration/start-inventree.sh --stop
```

The script starts InvenTree on port 8000 with a throwaway admin account,
and writes an API token to a file. It never prints the token. Set
`STOCKTHREAD_IT_DB=postgres` before starting it to run InvenTree on
PostgreSQL instead of SQLite; the `.it.env` file passes the setting on to
the tests. The tests
create and move stock, so never point them at a real server.

### Soak run

`tests/soak/soak.py` runs the connector for as long as you ask, with writes
on, against the same disposable server and a local fake of Slack whose
connection it keeps breaking. It also breaks InvenTree reads. At the end it
checks that every post was answered, no post wrote twice, and the stock adds
up. It also checks that threads, open files and memory stopped growing. With
the server started as above:

```sh
.venv/bin/python tests/soak/soak.py --minutes 1440 --report soak-report.json
```

`--minutes 10` is a quick check. The script's docstring lists every check
and option.

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
