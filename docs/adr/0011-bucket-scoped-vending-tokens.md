# 0011 — Anonymous read is contained by token composition, not account hygiene

**Status**: Accepted — verified live against both the backend catalog and the
deployed gateway on 2026-08-07. Retires the account-inventory gate of
[ADR-0010](0010-logs-leave-cloudflare.md) and the capability claim in
[ADR-0005](0005-cloudflare-account-is-the-trust-domain.md)'s note, which was
true when written and is no longer.

## Context

ADR-0005 established that vended SigV4 credentials inherit the storage
permissions of the API token the catalog was authenticated with, that catalog
access required account-wide Admin tokens, and therefore that the *account* was
the exposure boundary for anonymous read. Its closing note said bucket-scoped
storage tokens "do not exist in combination with catalog access. Do not
re-propose them without checking Cloudflare's permission groups first."

We checked. On 2026-07-09 Cloudflare shipped read-only Data Catalog tokens
([changelog](https://developers.cloudflare.com/changelog/post/2026-07-09-r2-data-catalog-read-only-tokens/)),
and API-created tokens compose an **account-scoped catalog** permission group
with a **bucket-scoped storage** group. The vended credential inherits only the
storage half. The full prior-art survey is
[docs/research/rest-catalog-auth-prior-art.md](../research/rest-catalog-auth-prior-art.md);
the client finding that decided the mechanism is that DuckDB speaks only vended
credentials — a remote-signing gateway would exclude our headline client.

## Decision

icegate's backend credentials become **two tokens selected by the
authenticated principal's capability** (icegate#34, `bearer_token` /
`bearer_token_write`):

- **read-only vending token** — `Workers R2 Data Catalog Read` (account) +
  `Workers R2 Storage Bucket Item Read` scoped to the `bioconice` bucket only.
  The default every request falls back to; what anonymous will use.
- **write token** — used only for principals holding `write`. Currently the
  legacy Admin token; to be replaced by a bucket-scoped
  Catalog-Write + Storage-Item-Write pair, after which nothing Admin-level
  remains in the Worker.

Anonymous read is therefore safe **in the shared 23-bucket account**: the
credential an anonymous reader receives cannot touch any other bucket, and
cannot write the public one.

## Evidence (2026-08-07)

Backend (token direct against R2 Data Catalog) and gateway (RO key through the
deployed `icegate-bioconice` Worker) verifications, all four axes each:

| check | result |
| --- | --- |
| vended creds list/read `bioconice` | allowed |
| vended creds write `bioconice` | 403 |
| vended creds read `bioc-site`, `cmgd-data` | 403 |
| catalog mutation (create namespace) via RO principal | 403 |

The write path was verified by running the gene2go ingest through the deployed
two-token Worker with the write key.

## Consequences

- The ADR-0010 gate item "every remaining bucket must be world-readable-safe"
  is retired: containment no longer depends on what else the account holds.
  The #13 inventory remains good hygiene, not a blocker.
- `anonymous.enabled: true` in `icegate.yaml` is the remaining flip for SPEC
  C4, at the maintainer's word.
- Vended credentials are static, with no observed expiry (Cloudflare documents
  none). For a fully public read-only bucket that is acceptable by
  construction — the credential grants nothing the anonymous endpoint doesn't.
  The upgrade path if that ever changes is R2's `temp-access-credentials` API
  (per-table prefixes, TTL), as Lakekeeper's `cloudflare-r2` profile does.
- The federation registry (#59) inherits this as its admission criterion:
  anonymous vended read, verified live — this ADR's evidence table is the
  conformance record format.
