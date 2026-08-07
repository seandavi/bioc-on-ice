# 0009 — biocOnIce moves accounts, not the logs

**Status**: Accepted. Revisits the *direction* chosen in
[ADR-0005](0005-cloudflare-account-is-the-trust-domain.md), not its principle.

## Context

ADR-0005 established that the exposure boundary for an anonymous-read Iceberg
catalog is the **Cloudflare account**, because R2 Data Catalog access is
Admin-only, Admin tokens cannot be bucket-scoped, and vended SigV4 credentials
inherit the backend token's storage permissions. That reasoning is sound and is
not in question here. Re-verified on the live gateway while deploying it: a
`read`-only principal that asks for delegated access receives
`s3.access-key-id` / `s3.secret-access-key` with no expiry and no session token.

ADR-0005 then chose which side of the boundary should move: the IP-bearing log
buckets leave the account, and biocOnIce stays. It justified that on the grounds
that "the public catalog is the thing that grows," and issue #13 became the gate
on anonymous read.

This ADR reverses that choice. The principle stands; the direction was decided
on the wrong axis.

## Decision

**biocOnIce moves to its own Cloudflare account. The logs stay where they are.**

The new account holds the `bioconice` R2 bucket and its Data Catalog, and
nothing else. Its Admin token's account-wide reach therefore covers only data
that is meant to be world-readable, which is exactly the state ADR-0005 requires
of an anonymous-read account.

The icegate Worker stays deployed in the existing account.

## Why the original axis was wrong

ADR-0005 weighed *growth* — biocOnIce accumulates releases, the logs do not
grow as fast — and concluded the smaller thing should move. But growth is a
steady-state property, and migration is a one-time cost. The two are only
comparable if you expect to migrate repeatedly, and you do not.

Priced as a one-time cost, the asymmetry inverts:

- **biocOnIce is fully re-derivable from public upstream files.** Moving it is
  not a data migration at all; it is re-running ingest against a new catalog.
  Measured: NCBI Gene is 116,410,423 raw rows in 2m56s, Ensembl is under a
  minute per species. Nothing has to be copied, and nothing can be lost, because
  the source of truth is Ensembl's and NCBI's FTP servers.
- **The logs are not re-derivable.** They are the only copy. Moving them means
  copying historical objects, repointing a live Logpush pipeline, and recreating
  the Data Catalog that `bioc-cloudfront-logs` already has.

## Why this needs no code, and no cross-account bindings

The obvious objection is that a Worker cannot bind a bucket in another account.
It cannot — an R2 binding resolves a bucket by name within the Worker's own
account, and there is no account parameter.

It does not matter, because **icegate uses no bindings.** Verified three ways:
`wrangler.jsonc` declares none, the source never calls the R2 binding API, and
`wrangler deploy` reports `No bindings found.` Every account reference is a
config value resolved from a Workers secret:

| | |
| --- | --- |
| catalog endpoint | `catalog.cloudflarestorage.com/${CF_ACCOUNT_ID}/bioconice` |
| `backend_warehouse` | `${CF_ACCOUNT_ID}_bioconice` |
| `backend_prefix` | `${R2_CATALOG_PREFIX}` |
| catalog auth | `${CF_API_TOKEN}` |
| data path | `s3.endpoint`, returned *by the backend* — follows automatically |

So the move is three Workers secrets and zero code changes: create the bucket
and Data Catalog in the new account, mint an Admin token there, repoint
`CF_ACCOUNT_ID`, `R2_CATALOG_PREFIX` and `CF_API_TOKEN`, re-run ingest.

## Why the logs must not move: log collection is account-bound

This is the decisive practical point, and it is what makes the two directions
genuinely different rather than mirror images.

A Logpush job's *destination* can live in another account — the documented
destination string takes an explicit `account-id` plus R2 `access-key-id` and
`secret-access-key`, so the destination account is a parameter. But a job's
*collection* is account-bound: `workers_trace_events` are emitted by the Workers
that produce them, so the job must exist in the account owning those Workers.
You cannot stand up an account whose purpose is to gather another account's
Workers logs.

Because the icegate Worker does not move, its trace events keep being emitted in
the existing account and keep flowing to the two jobs already running there —
`1826554` → `bioc-access-logs` and `1360530` → `cloudflare-managed-d9dc0363`.
No job is edited, no destination is repointed, there is no cutover gap, and no
historical object is copied. Under ADR-0005's direction every one of those is
required.

## Alternatives considered

**Stop logging raw client IPs, and keep one account.** Neither Logpush job
selects the `ClientIP` field; both are `workers_trace_events` with the same 13
fields. IPs reach those buckets instead through icegate's own request log, which
reads `cf-connecting-ip` at `src/logging/index.ts:21` and emits it as
`clientIp`, landing in the `Logs` field. Hashing it with the existing
`bioc-logs-ip-salt`, truncating it, or dropping it would reduce future exposure
and is worth doing on its own merits.

It is not sufficient as *the* fix, for two reasons. It does nothing about
historical objects already written. And it makes the safety of a public account
depend on every future log line in every future service staying disciplined
about PII — an invariant that must hold forever across code nobody has written
yet. "This account contains only public data" is a far more robust invariant
than "no code in this account ever logs anything sensitive." Do both; rely on
the account boundary.

**A scoped signer in icegate instead of credential vending.** If icegate
rewrote the table config to set `s3.remote-signing-enabled: true`, point
`s3.signer.uri` at itself, and strip the credentials, it could validate that
each request targets the `bioconice` bucket before signing, and the backend
credential would never leave the Worker. Bytes would still go direct to R2, so
it would stay cheap.

Not chosen now: Cloudflare currently returns `s3.remote-signing-enabled: false`,
so this is not available off the shelf, and it additionally requires every
client to honour remote signing, which duckdb-iceberg and PyIceberg support
unevenly. This is the principled long-term fix and belongs in icegate, not here.
It would make anonymous read safe in *any* account, at which point this ADR
becomes unnecessary rather than wrong.

**Proxying all object bytes through the Worker.** Rejected. A 5.35M-row exon
scan through Workers CPU and egress limits is the wrong shape.

## Consequences

Anonymous read stays disabled until the new account exists — the gate moves from
#13 to account creation. #13 stops being that gate and becomes hygiene: worth
doing for the abandoned `cloudflare-managed-c4208861`, and worth doing for the
IP logging, but it no longer blocks C4.

The existing account keeps an Admin-level R2 token and a Data Catalog on
`bioc-cloudfront-logs`, so **it must never host an anonymous-read catalog.**
That is ADR-0005's rule, unchanged and now permanent for that account rather
than temporary.

The new account needs a payment method for R2. If icegate later moves to a
custom domain, the zone must be in the Worker's account, which staying put keeps
simple. Any ingest path that writes directly to R2 rather than through the
gateway needs the new account's credentials.

## Note on the inventory

ADR-0005 made the decision rest on an inventory claim and required it be
re-verified rather than assumed. That requirement now applies to the *new*
account, where it is trivially satisfiable because the account is created empty
and holds one bucket.

It also caught something in the old one: Logpush job `1826554` writes to
**`bioc-access-logs`**, a bucket absent from #13's inventory entirely. A bucket
carrying Workers logs that nobody enumerated is precisely the failure mode
ADR-0005 warned about, and it is an argument for preferring an account whose
contents are known by construction over one whose contents must be audited.

The claim that IPs reach the log buckets via `Logs` is inferred from icegate's
source and the jobs' field lists; it has not been confirmed by reading a log
object, which would mean reading the IP data itself.
