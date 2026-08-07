# 0005 — The Cloudflare account is the trust domain

**Status**: Accepted — the trust-domain principle below stands and is unchanged.
The *choice of which side moves* (logs out, biocOnIce stays) is superseded by
[ADR-0009](0009-biocOnIce-moves-accounts-not-the-logs.md), which argues the
growth-versus-migration axis was the wrong one. Read both. The Note's claim
that bucket-scoped storage tokens cannot combine with catalog access was true
when written and stopped being true on 2026-07-09 — see
[ADR-0011](0011-bucket-scoped-vending-tokens.md), which followed the Note's
own instruction to re-check the permission groups.

## Context

biocOnIce is specified to serve anonymous public read (SPEC.md acceptance C4).
The warehouse lives in an R2 bucket, fronted by icegate, which holds a backend
credential server-side and issues its own scoped keys in its place. The natural
assumption is that a per-bucket credential can contain the exposure.

## Decision

The exposure boundary is the **Cloudflare account**, not the bucket, the
catalog, or the gateway. An account either holds only data that is safe to be
world-readable, or it does not host an anonymous-read catalog.

Consequently the existing account becomes the public trust domain, and
IP-bearing log data moves to a separate account reserved for sensitive data.
Anonymous read stays disabled until that move completes.

## Why a per-bucket credential cannot work

R2 Data Catalog access is only granted by `Admin Read only` or
`Admin Read & Write`, and both apply to every bucket in the account —
`Object Read only`, which *can* be bucket-scoped, does not grant catalog
access. Cloudflare states that vended SigV4 credentials "inherit the R2 storage
permissions of the API token used to authenticate", and clients use those
credentials against R2 directly, with the gateway out of the loop.

So an anonymous reader of any catalog in an account receives credentials able
to read every bucket in that account. icegate cannot mitigate this: it governs
which catalogs it routes to, not what a credential can reach once vended, and
`capabilities.write: false` does not touch the path at all.

## Consequences

A second Cloudflare account has to be created and Logpush repointed, which
across accounts requires the S3-compatible destination type rather than the
native R2 one, with a cutover gap to plan for. Until then biocOnIce is
key-only.

Inverting the boundary — moving the *sensitive* data out rather than moving
biocOnIce — was chosen because the public catalog is the thing that grows.
That inversion makes the decision rest on an inventory claim, so the inventory
must be re-verified before anonymous read is enabled, not assumed.

## Note

Bucket-scoped storage tokens were proposed as a mitigation and do not exist in
combination with catalog access. Do not re-propose them without checking
Cloudflare's permission groups first.
