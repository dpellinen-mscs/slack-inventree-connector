# Releasing

Nothing is released yet. This is how a release will be made. Each step that
publishes anything (a tag on the public repository, a GitHub release, a
package or an image) is the maintainer's, and none of them happens until the
maintainer decides where stockthread is published.

## Versions

stockthread follows [Semantic Versioning](https://semver.org/). Before 1.0.0
a minor version may break things; from 1.0.0 on, these count as breaking and
need a major version:

- a configuration key removed or renamed, or a default that writes more than
  it did ([configuration](configuration.md));
- a CLI command or option removed or changed in meaning;
- a Slack manifest that needs a new scope or event, so every install has to
  update its app;
- the oldest supported InvenTree or Python version raised
  ([versions](versions.md));
- an audit store that an older version can no longer open after the
  upgrade. Migrations run forward only, so every release that adds one says
  so in the changelog ([operations](operations.md) has the backup and
  downgrade notes).

The version lives in two places, `version` in `pyproject.toml` and
`__version__` in `src/stockthread/__init__.py`. `tests/test_release.py`
checks that they agree with each other and with the newest version in
`CHANGELOG.md`, and that the changelog's headings are in order.

## Before a release

1. `main` is green, and the integration matrix passed on the commit to
   release. The release pull request below changes `pyproject.toml`, so the
   matrix runs on it whatever else changed.
2. The README quickstart, followed from scratch in a temporary directory
   exactly as written, reaches a dry run. Fix the docs, not the steps you
   followed.
3. For the first public release: the prepublication audit (a leak scan of
   the full history, license and docs checks, private vulnerability
   reporting turned on for the repository so that [SECURITY.md](../SECURITY.md)
   holds).
4. The wheel holds the package data. Build it and list it:

   ```sh
   .venv/bin/pip wheel --no-deps -w dist .
   .venv/bin/python -m zipfile -l dist/stockthread-*.whl
   ```

   Every file in `src/stockthread/store/migrations/`, `py.typed` and
   `LICENSE` must be in it. `dist/` is ignored by git; delete it afterwards.

## The release pull request

One pull request, with nothing else in it:

1. Set the new version in `pyproject.toml` and in
   `src/stockthread/__init__.py`.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   (the day it merges), and add an empty `## [Unreleased]` above it. Read
   the section through as a user who upgrades: each breaking change and each
   new migration is named, with what to do.
3. `make check`.

## After it merges

The maintainer tags the merge commit (`git tag -a vX.Y.Z -m "stockthread
X.Y.Z"`) and pushes the tag, then publishes it wherever has been decided.
