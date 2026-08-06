# Architecture decisions

One file per decision, `NNNN-kebab-title.md`, numbered in the order taken and
never renumbered. A superseded ADR is not deleted or edited into agreement with
the present — it gets a `Superseded by` line and stays, because the reasoning
that turned out wrong is the useful part.

## What belongs here, and what does not

These three carry different things, and duplicating between them is how they rot:

| | holds |
| --- | --- |
| **SPEC.md** | what the system MUST do — normative behaviour, current and complete |
| **ADRs** | why a decision went the way it did, and what was rejected |
| **[wayfinder issues](https://github.com/seandavi/bioc-on-ice/issues/1)** | how the work is being found and sequenced |

An ADR earns its place when the obvious alternative was rejected for a reason
that is not obvious from the result. If the decision is self-evident from
reading SPEC.md, it does not need one. The test: would somebody six months from
now, or an architecture review, propose the rejected option again? If yes,
write the ADR.

ADRs are read by `/improve-codebase-architecture` precisely so that settled
decisions are not re-suggested. Contradicting one is allowed — reopening it
deliberately is the point — but it should be argued, not drifted into.

## Status values

`Accepted` · `Superseded by NNNN` · `Reopened` (argued against and being
revisited; say by whom and where).
