# 0010 — The logs leave Cloudflare: GCS destination, BigQuery analytics

**Status**: Accepted — the `cloudflare-managed` Logpush job (`1360530`) was
confirmed editable via the standard Logpush API on 2026-08-07 (a no-op `PUT`
succeeded), so it can be repointed or disabled like any other job. The gate's
account-inventory step is retired by
[ADR-0011](0011-bucket-scoped-vending-tokens.md); the log migration stands on
its own. Supersedes
the *direction* of [ADR-0009](0009-biocOnIce-moves-accounts-not-the-logs.md);
the trust-domain principle of
[ADR-0005](0005-cloudflare-account-is-the-trust-domain.md) is unchanged and is
what this ADR finally satisfies with a single Cloudflare account.

## Context

ADR-0009 chose to move biocOnIce to a new Cloudflare account, priced as "three
Workers secrets and zero code changes." That price counted only technical cost.
It has since become clear that a new Cloudflare account is not self-service
here: it means contacting an administrator, which triggers a security review,
an accounting workflow, and a second stream of invoice emails that must be
routed indefinitely. ADR-0009's own framing — one-time costs are cheap,
steady-state costs dominate — applies, and a permanent second account is a
steady-state administrative cost, not a one-time technical one.

Two capabilities re-price the other direction:

1. **A Logpush destination is a parameter.** Established while writing
   ADR-0009: collection is account-bound, but delivery is not. Jobs can write
   to any S3-compatible destination — including Google Cloud Storage, for which
   Logpush also has a native destination type. GCP is already provisioned and
   operational, so the sensitive-data trust domain can be an existing GCP
   project rather than a new Cloudflare account.
2. **BigQuery reads Logpush output directly.** Logpush writes gzipped NDJSON;
   a BigQuery external table over the GCS bucket queries it with no load jobs
   and no ETL. That retires the log-side R2 Data Catalog on
   `bioc-cloudfront-logs` entirely — which was ADR-0009's strongest structural
   reason the existing account could "never host an anonymous-read catalog":
   it was assumed to keep an Admin-level token and a sensitive catalog forever.
   It no longer needs either.

## Decision

**The logs leave Cloudflare entirely. biocOnIce stays, and the existing
account becomes the public trust domain.**

- Logpush jobs repoint to a GCS bucket in the existing GCP project.
- Log analytics moves to BigQuery: an external table over the GCS bucket,
  hive-partitioned on the `{DATE}` path prefix, plus a view that unpacks the
  `Logs` field. Nothing heavier unless query volume demands it.
- Historical log objects are copied to GCS, then the log buckets and the
  Data Catalog on `bioc-cloudfront-logs` are deleted from the Cloudflare
  account.
- Anonymous read (SPEC C4) stays disabled until the gate below is fully
  cleared.

## Why this supersedes ADR-0009

ADR-0009's asymmetry — biocOnIce is re-derivable, the logs are the only copy —
was correct on the axis it measured, but its pricing assumed account creation
was free. It is not. With organizational cost included:

| | move biocOnIce (ADR-0009) | move the logs (this ADR) |
| --- | --- | --- |
| technical one-time | 3 secrets, re-ingest | repoint jobs, copy history, delete buckets |
| organizational one-time | admin request, security review, accounting workflow | none — GCP exists |
| steady state | second account: invoices, keys, token inventory, forever | one CF account + GCP already being paid for |

The log migration is real work, but it is bounded and happens once, against a
pre-launch service where a delivery gap is acceptable. The second account is
forever.

## What is traded away

ADR-0009 prized an account that is clean **by construction**. This ADR trades
that for an account that is clean **by audit**, and the audit has already
failed once: `bioc-access-logs` was absent from #13's inventory. That risk is
accepted, with two mitigations:

- **Standing rule for the existing account:** no Logpush job, log sink, or
  service in this account may have an in-account destination. Every log
  destination points at GCP. This is the invariant that replaces
  "clean by construction," and it must be checked whenever a job is added.
- IP hygiene from ADR-0009's alternatives section — hash `clientIp` with
  `bioc-logs-ip-salt`, or truncate it — is still worth doing on its own
  merits, and is easier to reason about when the only place IPs land is a GCS
  bucket behind GCP IAM. BigQuery views can expose analytics without exposing
  raw `clientIp`.

## The gate on anonymous read

ADR-0005's re-verification requirement, applied to this direction. In order:

1. **Verify job `1360530` → `cloudflare-managed-d9dc0363` can be repointed to
   GCS or disabled.** This is the accept/reject switch for the whole ADR: if
   any Cloudflare-managed path keeps writing IP-bearing trace events to an
   in-account bucket that cannot be redirected, this plan fails and ADR-0009
   stands.
2. Repoint job `1826554` (→ `bioc-access-logs`) to the GCS destination.
3. Copy historical objects out: `bioc-access-logs`, `bioc-cloudfront-logs`,
   `cloudflare-managed-d9dc0363`, and the abandoned
   `cloudflare-managed-c4208861`. Then delete those buckets and the
   `bioc-cloudfront-logs` Data Catalog.
4. Re-run the #13 inventory against the account. Every remaining bucket must
   be world-readable-safe; the inventory is verified, not assumed. Measured
   2026-08-07: the account holds **23 buckets**, most belonging to other
   projects (`cmgd-*`, `omicidx*`, `biodatalake*`, `cdsci-lake`,
   `u24-cancer-genomics`, `starrocks-warehouse`, …). Unless every one of them
   is world-readable-safe, this step fails regardless of the log migration,
   and anonymous read needs the icegate scoped signer or a dedicated account
   after all. The log isolation stands on its own either way.
5. Only then enable anonymous read.

## Consequences

One Cloudflare account, one GCP project, one trust domain each — which is the
shape ADR-0005 wanted all along, without a new account on either side. icegate,
the `bioconice` bucket, and its Data Catalog do not move; no Workers secrets
change; ingest does not re-run. The sensitive domain's access control becomes
GCP IAM, which the org already administers.

#13 remains hygiene, as ADR-0009 left it, but its cleanup steps are now on the
critical path as the gate above.

Lakekeeper (or an icegate scoped signer, ADR-0009's long-term option) remains
the eventual collapse to "anonymous read safe in any account"; nothing here
forecloses it.
