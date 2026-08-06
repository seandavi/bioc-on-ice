# 0002 — Land sources verbatim, derive downstream

**Status**: Accepted

## Context

The first ingest parsed a GTF and wrote annotation tables in one pass, never
persisting the source. Reinterpreting anything meant re-downloading, and there
was no record of what we had been given as distinct from what we made of it.

## Decision

Two phases. **Land** writes the source verbatim into the `raw` namespace — for
a GTF, all nine columns including the unparsed attribute blob. **Transform**
derives tables by reading `raw` back out.

The layers do not share write semantics:

| | `raw` | derived |
| --- | --- | --- |
| identity | none — a source line has no natural key | declared identifier fields |
| write | replace per source version | merge |
| history | accumulated source versions | `first_seen` / `retired_in` |

## Why not transform in flight

Transform-in-flight couples interpretation to fetching: a parsing fix or a
newly-needed attribute costs a re-download, and for sources that mutate in
place (NCBI regenerates nightly) the bytes you would re-fetch are not the bytes
you parsed. Landing raw also separates the audit trail — what we were given —
from the derivation.

## Why raw needs no merge

An immutable source release makes replace-per-version both correct and
idempotent, so the merge machinery would buy nothing. Validity intervals belong
where there are keys and a maintained current state, which is the derived side.

## Consequences

Raw grows with the number of source versions retained; how many is a retention
policy, not a correctness question. Transform can be re-run offline, which the
test suite relies on.
