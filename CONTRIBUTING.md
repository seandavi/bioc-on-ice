# Contributing

Read [SPEC.md](SPEC.md) and [AGENTS.md](AGENTS.md) first. SPEC.md is the
design source of truth; decisions with a non-obvious alternative are ADRs in
[docs/adr/](docs/adr/).

## Proposing a data source

Open a **Data source** issue. The licence is a hard gate: quote the terms that
allow redistribution, or nothing is published. New sources land in
[cdsci-lake](https://github.com/seandavi/cdsci-lake) first (ADR-0015); this
repo derives and publishes them.

## Reporting a data problem

Open a **Bug** issue with the query that shows it, the numbers, and the
release (`manifest.json` `release`).

## Code

- One issue, one branch, one pull request (`Closes #N`).
- `uv run pytest` passes offline before you push.
- A new source is SQL, not a framework.

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
