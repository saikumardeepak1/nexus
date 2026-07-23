# Contributing to Nexus

This repo follows a standard feature-branch workflow, even for a single-maintainer project — it keeps `main` always deployable and gives every feature a reviewable, revertable unit.

## Branch naming

- `feature/<name>` — new functionality (e.g. `feature/hybrid-search`)
- `bugfix/<name>` — bug fixes
- `refactor/<name>` — internal restructuring with no behavior change
- `docs/<name>` — documentation-only changes

## Workflow

1. Pick up an open issue (see the [issue tracker](https://github.com/saikumardeepak1/nexus/issues) and [roadmap](docs/ROADMAP.md)).
2. Branch off `main` using the naming convention above.
3. Implement the change with tests.
4. Update relevant docs (README, TDD, API docs) in the same PR if the change affects them.
5. Open a PR describing Problem / Solution / Technical Decisions / Testing.
6. CI (lint, typecheck, test, docker build) must pass before merge.
7. Squash-merge into `main`.

## Local development

See [README.md](README.md#getting-started) for running the stack locally.

## Code style

- Python: `ruff` for lint/format, `mypy` for typing. Enforced in CI.
- TypeScript: `eslint` + `tsc --noEmit`. Enforced in CI.
