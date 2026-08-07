# Iceberg REST catalog auth and data-access delegation — prior art

**Status**: research note, not a decision. Written 2026-08-07 for the icegate
scoped-signer design.

> This is the first file under `docs/research/`. The convention it starts:
> `docs/` holds `adr/` for decisions we have made and `research/` for the
> evidence we gathered before making them. A research note is dated, cites a
> primary source for every claim, and is never edited to hide that it was wrong
> — if it goes stale, a newer note supersedes it. ADRs may cite research notes;
> research notes never bind anyone.

Every claim below is followed by the primary source that owns it. Where a fact
could not be confirmed in a primary source it is labelled **UNVERIFIED** rather
than asserted. Apache Iceberg citations are pinned to commit
[`0d60b2b`](https://github.com/apache/iceberg/tree/0d60b2bb8780d781f4ceb69032418d222b649ae5)
(main, 2026-08-07); the latest release at the time of writing is
`apache-iceberg-1.11.0` (2026-05-20)
([releases](https://github.com/apache/iceberg/releases)).

---

## Summary

The single most important finding for icegate: **remote signing is no longer a
bolted-on side API. It is now a first-class, per-table endpoint in the Iceberg
REST catalog spec itself** —
`POST /v1/{prefix}/namespaces/{namespace}/tables/{table}/sign` — and the old
standalone `POST /v1/aws/s3/sign` signer API is formally deprecated. The table
identity is in the URL path, which means a compliant signer is *told which table
a request is for* and can authorize against it before signing. That is exactly
the shape icegate wants, and it is the shape the spec now blesses.

The rest of the landscape:

- **Credential vending is the majority answer, and it is remarkably uniform.**
  Polaris, Unity Catalog, Nessie, Gravitino and AWS Lake Formation all do the
  same thing: call STS `AssumeRole` with an inline session policy pinned to the
  *table's own location prefix*, and hand back credentials that expire in about
  an hour. Nobody vends bucket-wide. Nobody vends without an expiry.
- **Three systems have actually built a signer**: Nessie (on by default for S3),
  Lakekeeper (for S3-compatible stores that have no STS), and the Iceberg
  reference client. Two systems have deliberately *refused* to: Polaris resolves
  the mode and then throws, and Gravitino throws
  `UnsupportedOperationException`. Unity Catalog's Iceberg facade implements
  neither.
- **A signer is what you build when STS is not available to you.** That is
  precisely icegate's position — R2 has no `AssumeRole` — and it is the reason
  Lakekeeper's R2 support and Nessie's signer are the two closest analogues to
  what icegate wants to be.
- **Cloudflare R2 Data Catalog sits alone in the bad quadrant**: it vends
  credentials that inherit the caller's token permissions wholesale, and it does
  not sign.

The binding constraint is not server capability but **client support**: DuckDB
cannot do remote signing at all, and PyIceberg can only do it on a non-default
FileIO. See §9 — this is the finding most likely to change icegate's plan.

---

## 1. The Apache Iceberg REST spec itself

### 1.1 `X-Iceberg-Access-Delegation`

Defined as a reusable parameter named `data-access`
([`rest-catalog-open-api.yaml#L2141-L2163`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L2141-L2163)):

```yaml
    data-access:
      name: X-Iceberg-Access-Delegation
      in: header
      description: >
        Optional signal to the server that the client supports delegated access
        via a comma-separated list of access mechanisms.  The server may choose
        to supply access via any or none of the requested mechanisms.
      required: false
      schema:
        type: array
        items:
          type: string
          enum:
            - vended-credentials
            - remote-signing
      example: "vended-credentials,remote-signing"
```

Three things follow directly from that text, and they matter:

1. **The client advertises capability; the server decides.** The header is an
   "optional signal ... that the client *supports*" a mechanism, and "the server
   may choose to supply access via any or none of the requested mechanisms". A
   server is free to ignore it entirely and return whatever it wants — which is
   what R2 does (§8), and what makes a gateway like icegate legitimate rather
   than a protocol violation.
2. It is a *comma-separated list*, `style: simple, explode: false` — so a client
   can ask for both and take whatever comes back.
3. It is attached to the load-table operation, alongside `If-None-Match`
   ([`#L1054-L1055`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L1054-L1055)).

**Notable**: the header string `X-Iceberg-Access-Delegation` appears *only in the
spec file* across the whole `apache/iceberg` repo — a GitHub code search for the
literal in that repo returns `open-api/rest-catalog-open-api.yaml` and nothing
else ([code search](https://github.com/search?q=repo%3Aapache%2Ficeberg+%22X-Iceberg-Access-Delegation%22&type=code)),
and it is absent from `RESTSessionCatalog.java`. The Java client does not send
it; it simply consumes whatever credentials or signing config the server
returns. PyIceberg *does* send it (§9.1). Practical consequence for icegate:
**you cannot rely on the header being present to decide what to return.** Decide
from your own policy and let the client cope.

### 1.2 What `LoadTableResult` carries

`LoadTableResult` has four relevant members
([`#L3900-L3919`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L3900-L3919)):
`metadata`, `config` (string→string), `storage-credentials` (array of
`StorageCredential`), and `remote-signing-config`.

`StorageCredential` is prefix-scoped
([`#L3827-L3841`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L3827-L3841)):

```yaml
    StorageCredential:
      required: [prefix, config]
      properties:
        prefix:
          description: Indicates a storage location prefix where the credential is relevant.
            Clients should choose the most specific prefix (by selecting the longest prefix)
            if several credentials of the same type are available.
        config: {type: object, additionalProperties: {type: string}}
```

That `prefix` field is the spec's only native expression of credential scope —
longest-prefix-wins, chosen by the *client*. Note what is **not** in the schema:
there is no expiry field. Expiry is conveyed only implicitly, inside `config`,
via whatever the storage provider uses (`s3.session-token` and the credential's
own lifetime). The spec never states a lifetime norm.

The documented AWS `config` keys
([`#L3873-L3881`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L3873-L3881)):
`client.region`, `s3.access-key-id`, `s3.secret-access-key`, `s3.session-token`,
`s3.remote-signing-enabled` ("if `true` remote signing should be performed as
described in the `RemoteSignRequest` schema section"), and
`s3.cross-region-access-enabled`.

Precedence is stated explicitly: "Clients must first check whether the
respective credentials exist in the `storage-credentials` field before checking
the `config` for credentials"
([`#L3883-L3886`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L3883-L3886)).

There is also a standalone credential-refresh endpoint,
`GET /v1/{prefix}/namespaces/{namespace}/tables/{table}/credentials` →
`LoadCredentialsResponse`
([`#L1352-L1396`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L1352-L1396)),
which exists precisely because vended credentials expire and a long-running scan
needs to re-fetch them without reloading the table.

### 1.3 Remote signing: the new per-table endpoint

```
POST /v1/{prefix}/namespaces/{namespace}/tables/{table}/sign
operationId: signRequest
```

([`#L1398-L1430`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L1398-L1430);
also in the released 1.11.0 spec at
[`apache-iceberg-1.11.0` `#L1266`](https://github.com/apache/iceberg/blob/apache-iceberg-1.11.0/open-api/rest-catalog-open-api.yaml#L1266)).

The path parameters are `prefix`, `namespace`, `table`. **The signer therefore
knows the table identity from the URL alone, before it parses the body.** It can
authorize the caller for that table, look up that table's storage location, and
refuse to sign anything outside it.

`RemoteSignRequest`
([`#L5424-L5455`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L5424-L5455)):

| field | required | notes |
|---|---|---|
| `region` | yes | SigV4 signing region |
| `uri` | yes | the full S3 request URI — bucket and key live here |
| `method` | yes | enum `PUT, GET, HEAD, POST, DELETE, PATCH, OPTIONS` |
| `headers` | yes | multi-valued map of the request's headers |
| `properties` | no | string→string passthrough |
| `body` | no | "should only be populated for requests where the body ... must be validated before a request is signed, such as the S3 DeleteObjects call" |
| `provider` | no | scheme of the storage native URI, e.g. `s3`; defaults to `s3` |

`RemoteSignResult` is just `{uri, headers}`
([`#L5457-L5467`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L5457-L5467)).

**What a signer can validate, therefore**: the method, the full URI (bucket +
key + query), every header, and — only for bulk deletes — the body. The `body`
field's description is the spec explicitly acknowledging the attack it closes:
a `DeleteObjects` POST names its victims in the body, not the URI, so a signer
that only inspected URIs could be tricked into signing a mass delete. This is
the one case where the spec says the signer must look deeper.

`RemoteSigningConfig`
([`#L5469-L5485`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L5469-L5485))
lets the server push static `properties` and `headers` that the client "MUST
pass through unchanged" / "MUST include unchanged" on every sign call. This is
the server's channel for handing the client an opaque token it must echo back —
useful if a signer wants a capability token rather than re-authenticating.

**Version caveat**: `RemoteSigningConfig` and the `remote-signing-config` field
are on main but **not** in the 1.11.0 released spec (absent from
[the 1.11.0 yaml](https://github.com/apache/iceberg/blob/apache-iceberg-1.11.0/open-api/rest-catalog-open-api.yaml)).
The per-table `/sign` path *is* in 1.11.0.

The backward-compatibility rules
([`#L3888-L3898`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L3888-L3898)):
`signer.endpoint` and `signer.uri` are **DEPRECATED** but still honoured; if
neither is present "clients SHOULD contact the default remote signing endpoint
using the catalog's base URI".

### 1.4 The old signer API is deprecated

`aws/src/main/resources/s3-signer-open-api.yaml` now opens with
([`#L20-L32`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/aws/src/main/resources/s3-signer-open-api.yaml#L20-L32)):

```yaml
# ⚠️ WARNING: this API is deprecated. Use the new remote signing endpoint instead,
# see open-api/rest-catalog-open-api.yaml.
info:
  title: "[DEPRECATED] Apache Iceberg S3 Signer API"
```

Its `POST /v1/aws/s3/sign` operation is marked `deprecated: true`
([`#L64-L71`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/aws/src/main/resources/s3-signer-open-api.yaml#L64-L71)),
and its `S3SignRequest`/`S3SignResponse` schemas are the same shape as
`RemoteSignRequest`/`RemoteSignResult` minus `provider`
([`#L109-L157`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/aws/src/main/resources/s3-signer-open-api.yaml#L109-L157)).
Note the old endpoint was **account-wide, not table-scoped** — no table in the
path. The move to a per-table path is the whole point of the redesign.

The deprecated spec also documents the caching contract, which survives into the
new one: "The server will also send a `Cache-Control` header, indicating whether
the response can be cached (`Cache-Control = ["private"]`) or not
(`Cache-Control = ["no-cache"]`)"
([`#L141-L145`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/aws/src/main/resources/s3-signer-open-api.yaml#L141-L145)).

### 1.5 The reference signer client (`S3V4RestSignerClient`)

[`aws/.../s3/signer/S3V4RestSignerClient.java`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/aws/src/main/java/org/apache/iceberg/aws/s3/signer/S3V4RestSignerClient.java).
Behaviour worth knowing before building a server for it:

- It refuses to pre-sign: `presign()` throws
  `UnsupportedOperationException("Pre-signing not allowed.")` (L296-299). A
  scoped signer cannot hand out presigned URLs to this client; it must sign
  headers per request.
- `calculateContentHashPresign` returns the constant `UNSIGNED_PAYLOAD` (L91,
  L290-293) — payloads are not hashed into the signature.
- `sign()` builds the `RemoteSignRequest` with `method`, `region`, `uri`,
  `headers`, `properties`, `body`, `provider = "s3"` (L302-316).
- `body` is populated **only** for `DeleteObjects` — a POST with a `delete`
  query parameter (L366-381), matching §1.3.
- Responses are cached in a static Caffeine cache keyed on the request, but
  **only if** the response carried `Cache-Control: private` (L318-347,
  L396-398). A signer that returns `Cache-Control: no-cache` forces a round trip
  per object — the knob for trading latency against per-request authorization.
- On the way back it deletes the `Cache-Control` header, then lets the original
  request's headers *overwrite* the server's (L383-394).
- Config resolution: `s3.signer.uri` (L78) and `s3.signer.endpoint` (L84) are
  both `@Deprecated` **since 1.11.0, scheduled for removal in 1.12.0**,
  superseded by `signer.uri` / `signer.endpoint`
  ([`RESTCatalogProperties.java#L104,L110`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/core/src/main/java/org/apache/iceberg/rest/RESTCatalogProperties.java#L104-L110)).
  The fallback endpoint constant `v1/aws/s3/sign` (L89) is deprecated with *no
  replacement* — from 1.12.0 the endpoint becomes required. The base URI falls
  back to the catalog `uri` (L119-124). Likewise `S3SignRequest` /
  `S3SignResponse` are now empty deprecated subinterfaces of
  `RemoteSignRequest` / `RemoteSignResponse`. The JSON on the wire did not
  change across the rename, so **a signer serving the old path and the old
  property spellings still works with new clients**.
- It also refuses `checkSignerParams` cases: `UnsupportedOperationException` for
  "Payload signing not supported" and "Chunked encoding not supported"
  (L400-408). That happens to match R2's own SigV4 limitations exactly (§8.6).
- The signer client authenticates *separately*, loading an auth manager under
  the name `"s3-signer"` (L182) with its own credential/token
  (`OAuth2Properties.CREDENTIAL`, `OAuth2Properties.TOKEN`, L146, L167) and an
  OAuth2 scope of `"sign"` (L99).

`s3.remote-signing-enabled` is defined in
[`S3FileIOProperties.java#L294-L296`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/aws/src/main/java/org/apache/iceberg/aws/s3/S3FileIOProperties.java#L294-L296),
default `false`.

### 1.6 Catalog authentication

The spec's security schemes are `OAuth2` (clientCredentials flow) and
`BearerAuth` (`type: http, scheme: bearer`), applied globally
([`#L6005-L6026`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L6005-L6026),
[`#L61-L62`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L61-L62)).
It also insists: "Implementations must not return altered success (200)
responses when a request is unauthenticated or unauthorized."

The inline token endpoint is `POST /v1/oauth/tokens` — note `oauth`, not
`oauth2` — and is **DEPRECATED for REMOVAL**
([`#L181-L199`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L181-L199)):

> The `oauth/tokens` endpoint is **DEPRECATED for REMOVAL**. It is _not_
> recommended to implement this endpoint, unless you are fully aware of the
> potential security implications. All clients are encouraged to explicitly set
> the configuration property `oauth2-server-uri` to the correct OAuth endpoint.
> Deprecated since Iceberg (Java) 1.6.0. The endpoint and related types will be
> removed from this spec in Iceberg (Java) 2.0.

The rationale is tracked in
[apache/iceberg#10537, "Security improvements in the Iceberg REST specification"](https://github.com/apache/iceberg/issues/10537).
The replacement guidance is unambiguous: **point clients at an external identity
provider via `oauth2-server-uri`; do not mint tokens in your catalog.**

The `token` vs `credential` client distinction: `token` is a bearer token used
directly; `credential` is a client-id:secret that the client exchanges at the
token endpoint for a token. `LoadTableResult.config` may itself carry a `token`
key — "Authorization bearer token to use for table requests if OAuth2 security
is enabled"
([`#L3868`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L3868)) —
or an RFC 8693 token-type key such as
`urn:ietf:params:oauth:token-type:jwt=<JWT-token>`
([`#L1050-L1053`](https://github.com/apache/iceberg/blob/0d60b2bb8780d781f4ceb69032418d222b649ae5/open-api/rest-catalog-open-api.yaml#L1050-L1053)).
So the catalog can hand a client a *different, narrower* token for table-level
operations. That is a per-table capability-token channel that already exists in
the protocol.

---

## 2. Apache Polaris (incubating)

Version 1.7.0, released 2026-08-02
([releases](https://github.com/apache/polaris/releases)).

### 2.1 RBAC

`principal` → `principal role` → `catalog role` → grants. The management API
exposes `/principals`, `/principal-roles`, `/catalogs/{c}/catalog-roles`, and
the mapping `/principal-roles/{pr}/catalog-roles/{c}`
([polaris-management-service.yml](https://github.com/apache/polaris/blob/main/spec/polaris-management-service.yml)).
Privileges are a flat enum of roughly 102 codes — `TABLE_READ_DATA`,
`TABLE_WRITE_DATA`, `CATALOG_MANAGE_ACCESS`, `PRINCIPAL_ROLE_USAGE`, and so on
([`PolarisPrivilege.java`](https://github.com/apache/polaris/blob/main/polaris-core/src/main/java/org/apache/polaris/core/entity/PolarisPrivilege.java)).

The token identifies the **principal, never a table**. The JWT carries `sub`
(principal name), `principalId`, `client_id`, and `scope` — a space-separated
list of activated principal roles in the form `PRINCIPAL_ROLE:<name>`, default
`PRINCIPAL_ROLE:ALL`
([`JWTBroker.java`](https://github.com/apache/polaris/blob/main/runtime/service/src/main/java/org/apache/polaris/service/auth/internal/broker/JWTBroker.java)).
Lifetime is `polaris.authentication.token-broker.max-token-generation`, default
`PT1H`. Polaris's own token endpoint is `POST /api/catalog/v1/oauth/tokens` and
is marked `deprecated: true`
([oauth-tokens-api.yaml](https://github.com/apache/polaris/blob/main/spec/polaris-catalog-apis/oauth-tokens-api.yaml)),
in step with the upstream spec.

### 2.2 Credential vending: STS with a per-table scope-down policy

`AwsCredentialsStorageIntegration.compute()` builds
`AssumeRoleRequest.builder().externalId(...).roleArn(...).roleSessionName(...).policy(policyString(...).toJson()).durationSeconds(...)`
and calls `stsClient.assumeRole(...)`
([`AwsCredentialsStorageIntegration.java` L209-295](https://github.com/apache/polaris/blob/main/polaris-core/src/main/java/org/apache/polaris/core/storage/aws/AwsCredentialsStorageIntegration.java)).

The scope-down policy (`policyString()`, L314-429) is **per-table-location
prefix, not bucket-wide**:

- read → `s3:GetObject`, `s3:GetObjectVersion` on `arn:aws:s3:::{bucket}/{prefix}/*`
- list → `s3:ListBucket` on `arn:aws:s3:::{bucket}` with a
  `Condition.StringLike."s3:prefix" = {prefix}/*`
- write → `s3:PutObject`, `s3:DeleteObject` on the same object ARN pattern,
  plus `s3:GetBucketLocation`, plus an optional KMS statement

The locations fed in are the table's own
([`StorageAccessConfigProvider.java` L78, L137](https://github.com/apache/polaris/blob/main/runtime/service/src/main/java/org/apache/polaris/service/catalog/io/StorageAccessConfigProvider.java)).

Two hardening details worth stealing:

1. `escapeIamGlobLiteral` (L536) escapes glob metacharacters in the key:
   `*`→`${*}`, `?`→`${?}`, `$`→`${$}`.
2. If the policy would contain zero statements, an explicit `Deny * on *` is
   inserted (L421-427) so that an empty session policy never silently degrades
   to the role's full rights.

Expiry: `STORAGE_CREDENTIAL_DURATION_SECONDS` default **3600s**, with a
credential cache at `STORAGE_CREDENTIAL_CACHE_DURATION_SECONDS` default
**1800s**
([`FeatureConfiguration.java` L451-468](https://github.com/apache/polaris/blob/main/polaris-core/src/main/java/org/apache/polaris/core/config/FeatureConfiguration.java)).
**UNVERIFIED**: whether any upper bound on the duration is validated.

### 2.3 Remote signing: resolved, then hard-rejected

Polaris has the enum — `VENDED_CREDENTIALS("vended-credentials")`,
`REMOTE_SIGNING("remote-signing")`, with legacy `true` mapping to vended
([`AccessDelegationMode.java` L36-62](https://github.com/apache/polaris/blob/main/runtime/service/src/main/java/org/apache/polaris/service/catalog/AccessDelegationMode.java))
— and even has a resolver that *prefers* remote signing when vending is
unavailable, including the case "If STS is unavailable for the catalog's AWS
storage, returns REMOTE_SIGNING"
([`DefaultAccessDelegationModeResolver.java` L66-67, L137-142](https://github.com/apache/polaris/blob/main/runtime/service/src/main/java/org/apache/polaris/service/catalog/DefaultAccessDelegationModeResolver.java)).

And then it throws
([`IcebergCatalogHandler.java` L1740-1755](https://github.com/apache/polaris/blob/main/runtime/service/src/main/java/org/apache/polaris/service/catalog/iceberg/IcebergCatalogHandler.java)):

```java
// TODO remove when remote signing is implemented
// Reject if the resolved mode is REMOTE_SIGNING since it's not yet supported
Preconditions.checkArgument(
    resolvedMode.orElse(null) != AccessDelegationMode.REMOTE_SIGNING,
    "Unsupported access delegation mode: %s", AccessDelegationMode.REMOTE_SIGNING);
```

**Polaris ships no signer endpoint.**

### 2.4 External identity

`polaris.authentication.type` is one of `internal` (default, confirmed in
[`application.properties` L225](https://github.com/apache/polaris/blob/main/runtime/defaults/src/main/resources/application.properties)),
`external` (the internal token endpoint then returns HTTP 501), or `mixed`, with
a per-realm override `polaris.authentication.<realm>.type`. OIDC itself is plain
Quarkus (`quarkus.oidc.auth-server-url`, `quarkus.oidc.client-id`, named tenants
`quarkus.oidc.<tenant>.*`). Polaris-side claim mapping is
`polaris.oidc.principal-mapper.type` / `.id-claim-path` / `.name-claim-path`,
JWT-only — opaque tokens need a custom `PrincipalMapper` SPI
([`OidcConfiguration.java`](https://github.com/apache/polaris/blob/main/runtime/service/src/main/java/org/apache/polaris/service/auth/external/OidcConfiguration.java)).
Role mapping is two-phase: `quarkus.oidc.roles.role-claim-path` produces
security roles, then `polaris.oidc.principal-roles-mapper.*` (with
`.mappings[n].regex` / `.replacement`) rewrites them into `PRINCIPAL_ROLE:<name>`
strings
([external IdP docs](https://polaris.apache.org/in-dev/unreleased/managing-security/external-idp/)).

### 2.5 S3-compatible storage without STS: no answer

`AwsStorageConfigurationInfo` carries `endpoint`, `endpointInternal`,
`pathStyleAccess`, `stsEndpoint`, `stsUnavailable`, `roleArn`, `externalId`,
`region`. The docs cover "AWS S3, and S3-compatible object stores (MinIO, Apache
Ozone S3 gateway, Ceph RGW, and similar)"
([configuring-aws-s3-cloud-storage-specific.md](https://github.com/apache/polaris/blob/main/site/content/in-dev/unreleased/configuration/configuring-polaris-for-production/configuring-aws-s3-cloud-storage-specific.md)).
**Cloudflare R2 is not mentioned anywhere in the repo — UNVERIFIED.**

The entire STS path is gated on
`shouldUseSts(cfg) { return !Boolean.TRUE.equals(cfg.getStsUnavailable()); }`
(`AwsCredentialsStorageIntegration.java` L297-299). With `stsUnavailable: true`,
**Polaris vends nothing** — the response carries only `client.region`,
`s3.endpoint`, `s3.path-style-access`, and the docs tell the client to omit
`X-Iceberg-Access-Delegation: vended-credentials` and authenticate to the object
store directly.

There is a dev-only escape hatch, `SKIP_CREDENTIAL_SUBSCOPING_INDIRECTION`
(default `false`), explicitly disclaimed for production: "those credentials are
handed to every client, breaking defense-in-depth. For S3-compatible storage
without STS, set stsUnavailable: true on the storage config instead."
([`FeatureConfiguration.java` L67-77](https://github.com/apache/polaris/blob/main/polaris-core/src/main/java/org/apache/polaris/core/config/FeatureConfiguration.java)).

**That escape hatch is exactly what R2 Data Catalog does by default** (§8), and
Polaris's own comment is the clearest available third-party statement of why it
is a bad idea.

---

## 3. Lakekeeper

Version 0.13.1, released 2026-06-30. This is the most directly relevant system
in the survey: it is the only one that both vends *and* signs, and the only one
with a first-class Cloudflare R2 path.

### 3.1 OpenFGA authorization

The model lives at
[`authz/openfga/v4.8/components`](https://github.com/lakekeeper/lakekeeper/tree/main/authz/openfga/v4.8/components)
(one `.fga` file per type, plus a compiled `schema.json`); the client is
`crates/authz-openfga/`. Object types: `server`, `project`, `warehouse`,
`namespace`, `role`, `user`, `lakekeeper_table`, `lakekeeper_view`,
`lakekeeper_generic_table`, `lakekeeper_catalog_tag`.

The hierarchy `server → project → warehouse → namespace → table` is expressed as
parent relations (`lakekeeper_table: define parent: [namespace]`,
`namespace: define parent: [namespace, warehouse]`, and so on), with privileges
recursing upward — e.g.
`define select: [user, role#assignee] or ownership or modify or select from parent`.
Data access is `define can_read_data: select` and
`define can_write_data: modify`.

A request costs **one** OpenFGA `Check` — OpenFGA walks the parent chain
server-side rather than Lakekeeper walking it
([`crates/authz-openfga/src/authorizer.rs`](https://github.com/lakekeeper/lakekeeper/blob/main/crates/authz-openfga/src/authorizer.rs)).

### 3.2 OIDC

Config keys (`LAKEKEEPER__<FIELD>`, from
[`crates/lakekeeper/src/config.rs`](https://github.com/lakekeeper/lakekeeper/blob/main/crates/lakekeeper/src/config.rs)):
`OPENID_PROVIDER_URI`, `OPENID_AUDIENCE`, `OPENID_ADDITIONAL_ISSUERS`,
`OPENID_SCOPE`, `OPENID_SUBJECT_CLAIM` (comma-separated, first-present wins),
`OPENID_ROLES_CLAIM`, plus a multi-IdP form
`OPENID_PROVIDERS__<IDP_ID>__{URI,AUDIENCE,SUBJECT_CLAIMS,...}`, plus
`ENABLE_KUBERNETES_AUTHENTICATION` and `INSTANCE_ADMINS`. Principal id
precedence is configured-claim → `oid` → `sub`
([`service/authn.rs`](https://github.com/lakekeeper/lakekeeper/blob/main/crates/lakekeeper/src/service/authn.rs)).
The FGA user id is `<idp_id>~<subject>` (separator `~`, ids `oidc` and
`kubernetes`), so tuples look like `user:oidc~alice`.

### 3.3 STS vending

`get_sts_policy_string()` scopes to the **table location prefix**, not the
warehouse
([`crates/lakekeeper/src/service/storage/s3.rs` L1229-1290](https://github.com/lakekeeper/lakekeeper/blob/main/crates/lakekeeper/src/service/storage/s3.rs)):

```rust
let bucket_arn = format!("arn:aws:s3:::{}", table_location.bucket_name().trim_end_matches('/'));
let key = escape_iam_glob_literal(&format!("{}/", table_location.key().join("/")));
let key_wildcard = format!("{key}*");
// "TableAccess"          Resource: {bucket_arn}/{key_wildcard}
// "ListBucketForFolder"  s3:ListBucket on bucket_arn, Condition StringLike s3:prefix = key_wildcard
// "GetBucketLocation"    s3:GetBucketLocation on bucket_arn
```

Actions by level (L1208-1227): Read → `s3:GetObject`, `s3:GetObjectVersion`;
ReadWrite adds `s3:PutObject`, `s3:AbortMultipartUpload`,
`s3:ListMultipartUploadParts`; ReadWriteDelete adds `s3:DeleteObject`. Note the
same glob-escaping defence Polaris implements — two projects arriving
independently at the same escape is a strong signal it bit someone. A code
comment explains the single-wildcard-ARN choice: "AWS STS enforces a (small,
undocumented) limit on the *packed* size of the session policy."

`sts-token-validity-seconds` defaults to **3600**.

### 3.4 Cloudflare R2: a dedicated non-STS vending path

Lakekeeper has a `cloudflare-r2` credential type (`account-id`,
`access-key-id`, `secret-access-key`, `token`) that POSTs to
`https://api.cloudflare.com/client/v4/accounts/{account}/r2/temp-access-credentials`
with body
`{"bucket", "prefixes": ["<table-key>/"], "permission": "object-read-only"|"object-read-write", "ttlSeconds", "parentAccessKeyId"}`
(`s3.rs` L686-730). The docs state this requires an **Admin Read & Write** API
token, and that R2 auto-sets `assume-role-arn: None`, `sts-enabled: true`,
`flavor: s3-compat`
([Lakekeeper storage docs](https://docs.lakekeeper.io/docs/nightly/storage/#cloudflare-r2)).

**This is directly actionable for icegate**: Cloudflare *does* have a
prefix-scoped, TTL-bounded temporary-credential API, and Lakekeeper uses it
rather than passing the parent token through. The cost is that the catalog must
hold an Admin Read & Write token — worse than a signer for a public read-only
bucket, but far better than R2 Data Catalog's current behaviour.

### 3.5 The signer, and what it validates

Routes (`crates/lakekeeper/src/api/iceberg/v1/s3_signer.rs` L34-76):
`/aws/s3/sign`, `/{prefix}/v1/aws/s3/sign`, and the one actually advertised to
clients, `/signer/{prefix}/tabular-id/{tabular_id}/v1/aws/s3/sign`. Note
Lakekeeper puts the **table id in the path**, arriving at the same conclusion
the Iceberg spec later reached with its per-table `/sign` endpoint — though
Lakekeeper has **not** adopted the spec path itself.

The validation order in `sign()`
([`crates/lakekeeper/src/server/s3_signer/sign.rs` L109-303](https://github.com/lakekeeper/lakekeeper/blob/main/crates/lakekeeper/src/server/s3_signer/sign.rs))
is the reference design, and the ordering is deliberate:

1. `require_warehouse_id(prefix)` — a bare `/aws/s3/sign` with no warehouse is
   rejected.
2. `require_warehouse_action(..., CatalogWarehouseAction::Use)` — authorization
   **before** anything else.
3. Storage profile must be S3 and have `remote_signing_enabled`, else `403
   RemoteSigningDisabled` (L141-155).
4. `parse_s3_url()` — scheme must be http/https; the method maps to
   `Operation::{Read,Write,Delete}` (anything outside GET/HEAD/POST/PUT/DELETE →
   405); bucket and key are extracted per `remote_signing_url_style`
   (`auto` / `path` / `virtual-host`). **`.r2.cloudflarestorage.com` is
   hardcoded as known virtual-host style** (L1048).
5. Resolve the tabular — by path `tabular_id`, else by S3 location. Views are
   explicitly not signable.
6. Authorize *that table for that operation* — `CatalogTableAction::ReadData`
   for GET/HEAD, `WriteData` for PUT/POST/DELETE (L200-250). The comment
   `// Can't fail here before AuthZ!` at L198 marks the deliberate ordering so
   that lookup failures leak nothing about which tables exist.
7. `validate_region()` — must equal the profile's region.
8. `validate_uri()` — **every** location the request touches must lie inside the
   resolved table's location (L597-640).

Only after all of that is the storage secret fetched (L272-290).

The subtle checks are the valuable part, because they are the ones a
from-scratch signer gets wrong:

- **`DeleteObjects`**: the XML body is parsed and *every* `<Key>` must pass
  `validate_uri` (L843-865). A missing body is an error, not a pass.
- **`ListObjectsV2`**: `require_bucket_addressed` insists the path resolves to
  exactly `s3://{bucket}` *and* that url-decoding did not change the path —
  "`%2F` hides separators from the url parser, which then collapses the `.`/`..`
  behind them" (L890-911). `require_known_list_parameters` allowlists exactly
  `list-type, prefix, continuation-token, delimiter, encoding-type, fetch-owner,
  max-keys, start-after, x-id` and rejects duplicates, because "a bucket
  sub-resource (`?policy`, `?versioning`, `?uploads`, …) alongside the list
  parameters would make the signed request return something else entirely"
  (L915-960). A list with no `prefix` is refused outright.
- **List uses stricter containment than reads**: `is_prefix_within` rather than
  `is_sublocation_of`, because "S3 matches list prefixes as raw strings, so a
  prefix that stops at the table location would also return the keys of
  same-prefixed siblings."
- **Decode/sign mismatch**: `SignRequestUri` carries both `received` and
  `decoded` URLs — the signature covers `received`, locations derive from
  `decoded`, and shape checks compare the two (L673-706).

**A gap worth recording**: `ParsedSignRequest.endpoint` and `.port` are both
`#[allow(dead_code)]` (L716-719) — the request **host is never compared against
the configured storage endpoint**. Only region and bucket/key containment are
checked.

The response is `{uri, headers}` with `Cache-Control: private` for GET/HEAD and
`no-cache` otherwise (L396-421), which is precisely the header that activates
the Java client's 30-second signature cache (§1.5).

### 3.6 Both modes at once

`remote-signing-enabled` (default `true`) and `sts-enabled` are independent
booleans. The docs: "If both methods are requested or neither is specified,
Lakekeeper attempts to provide vended credentials first (if STS is enabled),
then falls back to remote signing (if enabled)."
([storage docs](https://docs.lakekeeper.io/docs/nightly/storage/)). The header
constant is `DATA_ACCESS_HEADER = "x-iceberg-access-delegation"`, parsed
multi-valued into `DataAccess { vended_credentials, remote_signing }`.

Lakekeeper emits **both old and new key spellings** in the table config:
`s3.remote-signing-enabled=true`, `s3.signer.uri` + `s3.signer.endpoint`, and
`signer.uri` + `signer.endpoint`.

---

## 4. Unity Catalog (OSS)

### 4.1 Temporary credential API

Four POST endpoints under `/api/2.1/unity-catalog`:
`/temporary-table-credentials`, `/temporary-volume-credentials`,
`/temporary-model-version-credentials`, `/temporary-path-credentials`
([`api/all.yaml` L655-708](https://github.com/unitycatalog/unitycatalog/blob/main/api/all.yaml)).

Request bodies: `GenerateTemporaryTableCredential {table_id, operation}` with
`TableOperation` enum `UNKNOWN_TABLE_OPERATION | READ | READ_WRITE`
(all.yaml L2368-2389); volumes take `{volume_id, READ_VOLUME|WRITE_VOLUME}`;
paths take `{url, PATH_READ|PATH_READ_WRITE|PATH_CREATE_TABLE}`.

The response `TemporaryCredentials` carries exactly one of
`aws_temp_credentials {access_key_id, secret_access_key, session_token}`,
`azure_user_delegation_sas {sas_token}`, or `gcp_oauth_token {oauth_token}`,
**plus `expiration_time` (epoch ms) and `url`** — the normalized storage path
the credential covers (all.yaml L2390-2414). Note that unlike the Iceberg spec's
`StorageCredential`, UC's response has an explicit expiry field.

### 4.2 Scope and expiry

STS `AssumeRole` with a per-request inline policy: SELECT → `s3:GetO*`; UPDATE →
`s3:GetO*, s3:PutO*, s3:DeleteO*, s3:*Multipart*`; `s3:ListBucket` constrained by
an `s3:prefix` condition; bucket+prefix scoped, with IAM special characters
escaped
([`AwsPolicyGenerator.java` L21-99, L131-133](https://github.com/unitycatalog/unitycatalog/blob/main/server/src/main/java/io/unitycatalog/server/service/credential/aws/AwsPolicyGenerator.java)).

Expiry is **hard-coded at one hour with no knob**:
`durationSeconds((int) Duration.ofHours(1).toSeconds())`
([`AwsCredentialGenerator.java` L108-110](https://github.com/unitycatalog/unitycatalog/blob/main/server/src/main/java/io/unitycatalog/server/service/credential/aws/AwsCredentialGenerator.java)).

The UC master role assumes the customer's `role_arn` using a UC-generated
`external_id` as a confused-deputy guard
([`AwsCredentialVendor.java` L19-57](https://github.com/unitycatalog/unitycatalog/blob/main/server/src/main/java/io/unitycatalog/server/service/credential/aws/AwsCredentialVendor.java)).

### 4.3 Catalog auth

External OIDC plus RFC 8693 token exchange:
`POST /api/1.0/unity-control/auth/tokens` with
`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`. UC validates the
IdP token and mints its own JWT
([`AuthService.java` L79-106, L181-189](https://github.com/unitycatalog/unitycatalog/blob/main/server/src/main/java/io/unitycatalog/server/service/AuthService.java));
`AuthDecorator` then enforces `Authorization: Bearer` with an internal-issuer JWT
([`AuthDecorator.java` L30-84](https://github.com/unitycatalog/unitycatalog/blob/main/server/src/main/java/io/unitycatalog/server/service/AuthDecorator.java)).
Turned on with `server.authorization=enable` plus
`server.authorization-url` / `server.token-url` / `server.allowed-issuers`; an
admin PAT is written to `etc/conf/token.txt` at startup
([auth.md](https://github.com/unitycatalog/unitycatalog/blob/main/docs/server/auth.md)).
Authorization decisions go through `JCasbinAuthorizer` (jCasbin `SyncedEnforcer`,
policies in the UC database), or `AllowingAuthorizer` when disabled.

### 4.4 Iceberg REST: read-only, no delegation

UC OSS exposes a **read-only** Iceberg REST facade at
`/api/2.1/unity-catalog/iceberg/` for Delta UniForm tables
([uniform.md](https://github.com/unitycatalog/unitycatalog/blob/main/docs/usage/tables/uniform.md)).
`GET /v1/config` returns `prefix=catalogs/<catalog>`; only config, namespaces,
loadTable, tableExists, listTables, loadView and metrics are implemented
([`IcebergRestCatalogService.java` L50-100](https://github.com/unitycatalog/unitycatalog/blob/main/server/src/main/java/io/unitycatalog/server/service/IcebergRestCatalogService.java)).

There is **no `X-Iceberg-Access-Delegation` handling, no `/sign` endpoint, and no
credential injection** in the Iceberg service — data access goes through the
separate temp-credential endpoints. So UC solved the same problem with its own
API rather than the Iceberg delegation mechanisms.

---

## 5. AWS: Glue Iceberg REST, S3 Tables, Lake Formation

### 5.1 Glue: SigV4 on the catalog API itself

Endpoint `https://glue.<region>.amazonaws.com/iceberg`, with the prefix always
`/catalogs/{catalog}` — e.g.
`GET /iceberg/v1/catalogs/{catalog}/namespaces`
([connect-glu-iceberg-rest.html](https://docs.aws.amazon.com/glue/latest/dg/connect-glu-iceberg-rest.html)).
"API requests to the AWS Glue Data Catalog endpoints are authenticated using AWS
Signature Version 4 (SigV4)"
([iceberg-rest-apis.html](https://docs.aws.amazon.com/glue/latest/dg/iceberg-rest-apis.html)).

**AWS is the only precedent in this survey for SigV4-authenticating the catalog
API itself**, rather than a bearer token.

**Config key names, pinned** (the task brief flagged uncertainty here — the
dotted spellings are the real ones):

| key | value | source |
|---|---|---|
| `rest.sigv4-enabled` | `true` | AWS docs; **legacy** switch in Iceberg (`SIGV4_ENABLED_LEGACY`, emits a deprecation warning) |
| `rest.signing-name` | `glue` (Iceberg default `execute-api`) | [`AwsProperties.java` L185-218](https://github.com/apache/iceberg/blob/main/aws/src/main/java/org/apache/iceberg/aws/AwsProperties.java) |
| `rest.signing-region` | region | same |
| `rest.access-key-id` / `rest.secret-access-key` / `rest.session-token` | — | same |
| `rest.auth.type=sigv4` | modern replacement | [`AuthManagers.java` L35-55](https://github.com/apache/iceberg/blob/main/core/src/main/java/org/apache/iceberg/rest/auth/AuthManagers.java), [`AuthProperties.java` L25-48](https://github.com/apache/iceberg/blob/main/core/src/main/java/org/apache/iceberg/rest/auth/AuthProperties.java) |
| `rest.auth.sigv4.delegate-auth-type` | default `oauth2` | same |

The hyphenated spellings `rest-signing-name` / `rest-signer-region` appear in **no
AWS doc and no Iceberg source** that was checked — treat them as incorrect.

### 5.2 S3 Tables

Endpoint `https://s3tables.<region>.amazonaws.com/iceberg`; `warehouse` is the
table bucket ARN and the REST `{prefix}` is the URL-encoded bucket ARN; SigV4
with signing name `s3tables`; **"OAuth-based authentication is not supported"**
([s3-tables-integrating-open-source.html](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html)).
Authorization is per-operation IAM: `loadTable` requires
`s3tables:GetTableMetadataLocation` plus `s3tables:GetTableData`.

For governed access AWS steers you to the Glue endpoint plus Lake Formation: the
client role needs `lakeformation:GetDataAccess`, and an admin must enable "Allow
external engines to access data in Amazon S3 locations with full table access",
after which "third-party applications ... get temporary credentials from Lake
Formation"
([s3-tables-integrating-glue-endpoint.html](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-glue-endpoint.html)).

**UNVERIFIED**: the exact `X-Iceberg-Access-Delegation` value Glue and S3 Tables
expect, and the exact `LoadTableResult` keys they return. The AWS documentation
pages never mention the header. Only third-party writeups claim
`vended-credentials`; that claim is not carried here.

### 5.3 Lake Formation vending

`GetTemporaryGlueTableCredentials`: "Lake Formation assumes the role associated
with a registered location ... with a scope down policy which restricts the
access to a single prefix." Request takes `TableArn` (required),
`DurationSeconds`, `Permissions` (SELECT/INSERT/...), `S3Path`,
`SupportedPermissionTypes`; the response is `AccessKeyId`, `SecretAccessKey`,
`SessionToken`, `Expiration`, `VendedS3Path[]`
([API_GetTemporaryGlueTableCredentials](https://docs.aws.amazon.com/lake-formation/latest/APIReference/API_GetTemporaryGlueTableCredentials.html)).

Note a documentation inconsistency worth knowing if you copy the numbers: the
`DurationSeconds` "Valid Range" is given as **900–43200**, while prose on the
same page says "between 900 and 21,600 seconds".

---

## 6. Project Nessie

Version 0.108.4, released 2026-07-31. **Nessie is the closest existing analogue
to icegate's plan**: it implements remote signing, has it on by default for S3,
and validates per request against table locations.

### 6.1 Auth

Authorization is CEL-based and still current:
`nessie.server.authorization.enabled=true` and
`nessie.server.authorization.rules.<ruleId>=<CEL expression>`, with CEL variables
`op`, `role`, `roles`, `ref`, `path`, `contentType`
([authorization docs](https://projectnessie.org/nessie-latest/authorization/)).
Authentication is Quarkus OIDC bearer tokens; the **exact enabling property is
UNVERIFIED** in this pass (the commonly cited switches are
`nessie.server.authentication.enabled` plus `quarkus.oidc.*` — confirm at the
[authentication page](https://projectnessie.org/nessie-latest/authentication/)).

The Iceberg REST endpoint is served under `/iceberg`
([`IcebergApiV1S3SignResource.java` L60-97](https://github.com/projectnessie/nessie/blob/main/catalog/service/rest/src/main/java/org/projectnessie/catalog/service/rest/IcebergApiV1S3SignResource.java)).

### 6.2 Vending (opt-in, off by default)

`nessie.catalog.service.s3.default-options.server-iam.*` (server-side) and
`client-iam.*` (vended to clients), with keys `enabled` (**default false**),
`assume-role`, `policy`, `external-id`, `session-duration`, `sts-endpoint`
([configuration docs](https://projectnessie.org/nessie-latest/configuration/)).
The client-iam session policy is auto-generated per request and
S3-location-dependent — configured statements are "inserted *after* the
automatically generated S3 location dependent `Allow` policy statement"
([`S3ClientIam.java` L38-56](https://github.com/projectnessie/nessie/blob/main/catalog/files/api/src/main/java/org/projectnessie/catalog/files/config/S3ClientIam.java)).
STS credentials are cached with an `expiryReduction` safety margin
([`StsCredentialsManager.java`](https://github.com/projectnessie/nessie/blob/main/catalog/files/impl/src/main/java/org/projectnessie/catalog/files/s3/StsCredentialsManager.java)).
**UNVERIFIED**: the default numeric `session-duration`.

### 6.3 Signing (on by default)

`nessie.catalog.service.s3.default-options.request-signing-enabled` is **enabled
by default**, and `url-signing-expire` defaults to **3 hours**
([configuration docs](https://projectnessie.org/nessie-latest/configuration/)).

Endpoints: `POST /iceberg/v1/{prefix}/s3sign/{signedParams}` (current, with a
key-signed path parameter) and a legacy
`POST /iceberg/v1/{prefix}/s3-sign/{identifier}`
(`IcebergApiV1S3SignResource.java` L96-136). Nessie advertises the signer by
setting **both** `s3.signer.uri` and `s3.signer.endpoint` in the
`LoadTableResult` config, deliberately, for pre-1.5.0 Iceberg clients
([`IcebergConfigurer.java` L76-80, L325-361](https://github.com/projectnessie/nessie/blob/main/catalog/service/rest/src/main/java/org/projectnessie/catalog/service/rest/IcebergConfigurer.java)).

What it validates
([`IcebergS3SignParams.java` L100-260](https://github.com/projectnessie/nessie/blob/main/catalog/service/rest/src/main/java/org/projectnessie/catalog/service/rest/IcebergS3SignParams.java)):

- the request URI must be an S3 URI;
- PUT/POST/DELETE/PATCH are classified as writes;
- **writes must target the table's *current* write location; reads may also
  target historical snapshot locations** — a distinction no other implementation
  in this survey makes, and the right one for a versioned table format;
- the URI is prefix-checked against the warehouse and table base locations;
- **signing a write to the current `metadata.json` location is rejected**, with
  a TODO to block all metadata objects.

That last rule is the interesting one: even an authorized writer is not allowed
to use a signed request to clobber the metadata pointer, because that would let
a client bypass the catalog's own commit path.

---

## 7. Apache Gravitino

Version 1.3.0, released 2026-06-29.

The Iceberg REST service is a servlet at `/iceberg/*`, so
`http://host:9001/iceberg/v1/{catalog-prefix}/...`
([`RESTService.java` L79, L206](https://github.com/apache/gravitino/blob/v1.3.0/iceberg/iceberg-rest-server/src/main/java/org/apache/gravitino/iceberg/RESTService.java);
[docs](https://gravitino.apache.org/docs/1.3.0/iceberg-rest-service/)). Caller
auth goes through a shared `AuthenticationFilter` with types `NONE, SIMPLE,
BASIC, OAUTH, KERBEROS` — but "The Iceberg REST service does not support
Kerberos authentication"
([security docs](https://gravitino.apache.org/docs/1.3.0/security/how-to-authenticate/)).
OAuth means a bearer JWT (static key or JWKS) via `gravitino.authenticators=oauth`
plus `authenticator.oauth.*`.

**Credential vending: yes.** Provider ids verified in
`api/src/main/java/org/apache/gravitino/credential/`: `s3-token`,
`s3-secret-key`, `aws-irsa`, `gcs-token`, `adls-token`, `azure-account-key`,
`oss-token`, `oss-secret-key`, `jdbc-user-password`. Config keys in
`CredentialConstants.java` L26-47: `credential-providers`,
`credential-cache-expire-ratio` (default 0.15), `credential-cache-max-size`
(default 10000), `s3-token-expire-in-secs`, `oss-token-expire-in-secs`,
`adls-token-expire-in-secs`, `s3-credential-list-location-prefix`; S3 keys
`s3-access-key-id`, `s3-secret-access-key`, `s3-region`, `s3-role-arn`,
`s3-token-service-endpoint`, `s3-external-id`
([`S3Properties.java` L28-38](https://github.com/apache/gravitino/blob/v1.3.0/catalogs/catalog-common/src/main/java/org/apache/gravitino/storage/S3Properties.java)).
(Doc bug worth noting: the credential-vending page says
`gravitino.iceberg-rest.cache-max-size`; the real key is
`credential-cache-max-size`.)

Scope is a per-table inline STS session policy: `s3:GetObject` /
`GetObjectVersion` on read+write location prefixes, `s3:PutObject` /
`DeleteObject` on write locations only, `s3:ListBucket` with an `s3:prefix`
StringLike condition, plus `s3:GetBucketLocation`. The locations are the table's
`location()`, `write.data.path` and `write.metadata.path`
([`S3TokenGenerator.java` L109-207](https://github.com/apache/gravitino/blob/v1.3.0/bundles/aws/src/main/java/org/apache/gravitino/s3/credential/S3TokenGenerator.java)).
Trailing-slash prefix handling prevents sibling-table enumeration by default
(`s3-credential-list-location-prefix` defaults to false).

Expiry: `s3-token-expire-in-secs` defaults to **3600** (OSS and ADLS likewise);
the server caches a credential for `remaining_lifetime * 0.15`. **UNVERIFIED**:
GCS token lifetime (no expire config exists for it).

**Remote signing: definitively not implemented.** `vended-credentials` is
honoured case-insensitively on loadTable/createTable/updateTable/registerTable,
while `remote-signing` throws
`UnsupportedOperationException("Gravitino IcebergRESTServer doesn't support remote signing")`
([`IcebergTableOperations.java` L96, L175, L309, L628-646](https://github.com/apache/gravitino/blob/v1.3.0/iceberg/iceberg-rest-server/src/main/java/org/apache/gravitino/iceberg/service/rest/IcebergTableOperations.java));
the docs say "remote-signing: Gravitino doesn't support this mode yet". A live
inconsistency: `remote-signing` is still advertised as an allowed value of the
`gravitino.iceberg-rest.data-access` property returned by `/v1/config` even
though the header is rejected.

---

## 8. Cloudflare R2 Data Catalog

### 8.1 Catalog auth

Iceberg clients authenticate with a Cloudflare API token as a bearer token:
"Iceberg clients (including PyIceberg) must authenticate to the catalog with an
R2 API token that has both R2 and catalog permissions"
([get-started](https://developers.cloudflare.com/r2/data-catalog/get-started/)).
It is passed as the OAuth2 `token` property — `RestCatalog(..., token=TOKEN)` in
PyIceberg, `iceberg.rest-catalog.oauth2.token` in Trino,
`CREATE SECRET (TYPE ICEBERG, TOKEN '<token>')` in DuckDB
([config examples](https://developers.cloudflare.com/r2/data-catalog/config-examples/pyiceberg/)).

Dashboard tokens must be **Admin Read & Write** or **Admin Read only**:
"Read-only catalog operations (such as listing namespaces, loading tables, and
querying data) work with Admin Read only, while write operations … require Admin
Read & Write"
([tokens](https://developers.cloudflare.com/r2/api/tokens/#permissions)).
API-created tokens need two permission groups: `Workers R2 Data Catalog Read`
(or Write) **plus** `Workers R2 Storage Bucket Item Read` (or Write)
([manage-catalogs](https://developers.cloudflare.com/r2/data-catalog/manage-catalogs/#authenticate-your-iceberg-engine)).
Read-only tokens are recent — changelog 2026-07-09, "R2 Data Catalog now
supports read-only API tokens"
([changelog](https://developers.cloudflare.com/changelog/post/2026-07-09-r2-data-catalog-read-only-tokens/)).
Before that date, *every* catalog operation required Admin Read & Write.

### 8.2 What it vends

"The catalog also provides engines with SigV4 credentials, which are required to
access the underlying data files stored in R2." And, crucially:

> When an engine loads credentials from the catalog, R2 Data Catalog returns
> SigV4 credentials that **inherit the R2 storage permissions of the API token
> used to authenticate**. A token with read-only R2 Data Catalog access but
> read-write R2 storage access can still be used to write objects (including
> catalog metadata files) to the underlying bucket.

([manage-catalogs](https://developers.cloudflare.com/r2/data-catalog/manage-catalogs/#authenticate-your-iceberg-engine))

That sentence is the whole problem in one paragraph. The credential is not
narrowed to the table, not narrowed to a prefix, and the catalog-side permission
does not constrain the storage-side permission.

**UNVERIFIED from Cloudflare's documentation**: the exact `LoadTableResult` JSON
shape R2 returns — whether it populates the top-level `storage-credentials`
array or only the flat `config` map, whether a session token is present, and
whether there is any expiry. Cloudflare publishes no LoadTable response example
and no credential-lifetime statement anywhere in its R2 docs corpus (a full-text
check of
[`r2/llms-full.txt`](https://developers.cloudflare.com/r2/llms-full.txt) returns
zero hits for `storage-credentials`, `expires`, or vended-credential lifetime).

**Observed directly, on our own live gateway**, and recorded in
[ADR-0009](../adr/0009-biocOnIce-moves-accounts-not-the-logs.md): "a `read`-only
principal that asks for delegated access receives `s3.access-key-id` /
`s3.secret-access-key` with **no expiry and no session token**." That is an
internal observation rather than a vendor commitment — it is the best evidence
available, and it should be re-checked rather than assumed stable.

### 8.3 Remote signing: not supported

Cloudflare's own PySpark example turns it off explicitly and asks for vended
credentials instead
([spark-python](https://developers.cloudflare.com/r2/data-catalog/config-examples/spark-python/)):

```
.config("spark.sql.catalog.my_catalog.header.X-Iceberg-Access-Delegation", "vended-credentials")
.config("spark.sql.catalog.my_catalog.s3.remote-signing-enabled", "false")
```

So `X-Iceberg-Access-Delegation: vended-credentials` **is** honoured — Cloudflare
instructs clients to send it. No `/v1/aws/s3/sign` endpoint is documented
anywhere. **UNVERIFIED but strongly implied**: there is no explicit sentence
stating remote signing is unsupported; the evidence is the config example plus
the absence of any signer endpoint.

### 8.4 Blast radius

R2 API tokens have two resource types, `Account` and `Bucket`, where a specific
bucket is
`"com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_<JURISDICTION>_<BUCKET_NAME>": "*"`
([tokens#resources](https://developers.cloudflare.com/r2/api/tokens/#resources)).

The critical asymmetry: **`Workers R2 Data Catalog Read`/`Write` are
Account-scoped permission groups, while `Workers R2 Storage Bucket Item
Read`/`Write` are Bucket-scoped**
([tokens#permission-groups](https://developers.cloudflare.com/r2/api/tokens/#permission-groups)).

So the tightest available token is: catalog access across the whole account,
object access limited to named buckets. Because the vended SigV4 credential
inherits the *storage* half, **a bucket-scoped `Storage Bucket Item Read` token
yields a vended credential that cannot touch other buckets.** That is the real
containment lever available today, and it is narrower than the dashboard's
"Admin Read only" preset, which is account-wide on both halves.

Note also that the dashboard presets `Object Read & Write` / `Object Read only`
"are only supported by the S3-compatible API, not the Cloudflare REST API" and do
not satisfy Data Catalog
([tokens#permissions](https://developers.cloudflare.com/r2/api/tokens/#permissions)).

### 8.5 Endpoint shape

Wrangler builds the catalog URI as `https://catalog.cloudflarestorage.com/${path}`
where `path = response.name.replace("_", "/")` and `warehouse = response.name` —
so **warehouse = `<ACCOUNT_ID>_<BUCKET_NAME>`** and **catalog URI =
`https://catalog.cloudflarestorage.com/<ACCOUNT_ID>/<BUCKET_NAME>`**
([workers-sdk `r2/catalog.ts` L87-100](https://github.com/cloudflare/workers-sdk/blob/main/packages/wrangler/src/r2/catalog.ts)).
R2 Data Catalog remains in **public beta**
([overview](https://developers.cloudflare.com/r2/data-catalog/)). There is no
consolidated limitations page; caveats live per-engine.

### 8.6 R2 SigV4 quirks that constrain a Worker-side signer

- Presigned URLs are supported and are plain SigV4, "generated client-side with
  no communication with R2"; examples use
  `X-Amz-Content-Sha256=UNSIGNED-PAYLOAD`
  ([presigned-urls](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)).
- **Region must be `auto`** — R2 "enforce[s] the requirement for `auto` in SigV4
  signing" ([R2 changelog](https://developers.cloudflare.com/r2/llms-full.txt)).
- **Streaming/chunked SigV4 is not supported**: "`DisablePayloadSigning = true`
  and `DisableDefaultChecksumValidation = true` must be passed as Cloudflare R2
  does not currently support the Streaming SigV4 implementation used by
  AWSSDK.S3"
  ([aws-sdk-net](https://developers.cloudflare.com/r2/examples/aws/aws-sdk-net/)).
  The Java SDK likewise needs `.chunkedEncodingEnabled(false)`, since chunked
  transfer encoding on `putObject` "causes a signature mismatch error (HTTP 403)
  with R2"
  ([aws-sdk-java](https://developers.cloudflare.com/r2/examples/aws/aws-sdk-java/)).
- Expired signature → `403` / `ExpiredRequest`, error code `10018`
  ([errors](https://developers.cloudflare.com/r2/api/s3/errors/)).

**These line up exactly with the Iceberg Java signer's own restrictions** (§1.5:
payload signing unsupported, chunked encoding unsupported, `UNSIGNED-PAYLOAD`).
The two constraint sets are compatible, which is a genuine piece of good news
for icegate.

- Separately, R2 *does* have a short-lived credential API outside the catalog
  path: "short-lived, scoped S3 credentials derived from an existing R2 API
  token … include a session token and expire automatically"
  ([temporary-credentials](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/)).
  This is the API Lakekeeper drives for R2 (§3.4).

---

## 9. Client reality check

This section is the one most likely to change the plan. A signer nobody can
speak to is not a security control.

### 9.1 PyIceberg

Latest release `pyiceberg-0.11.1` (2026-03-03); `main` is 0.12.0.

- **Vended credentials: yes, and it asks for them by default.**
  `ACCESS_DELEGATION_DEFAULT = "vended-credentials"`, applied with
  `session.headers.setdefault("X-Iceberg-Access-Delegation", ACCESS_DELEGATION_DEFAULT)`
  ([`pyiceberg/catalog/rest/__init__.py`](https://github.com/apache/iceberg-python/blob/main/pyiceberg/catalog/rest/__init__.py)).
  Overridable per-catalog via `header.X-Iceberg-Access-Delegation`.
- **Version caveat**: through 0.11.1 PyIceberg only merged the flat `config`
  map. The top-level `storage-credentials` array is **new in 0.12.0**, via
  `_resolve_storage_credentials()`, which does longest-prefix matching over
  `StorageCredential.prefix` — "Per Iceberg spec: storage-credentials take
  precedence over config". 0.12.0 also adds `load_credentials()` for refresh.
  (Verified by diffing tags 0.7.0 / 0.8.0 / 0.9.0 / 0.10.0 / 0.11.1 — zero
  occurrences of `StorageCredential` in any of them.)
- **Remote signing: yes, but only on `FsspecFileIO`, which is not the default.**
  `S3V4RestSigner` lives in
  [`pyiceberg/io/fsspec.py`](https://github.com/apache/iceberg-python/blob/main/pyiceberg/io/fsspec.py)
  (L117-163), registered as a botocore `before-sign.s3` hook with
  `config_kwargs["signature_version"] = UNSIGNED` (L189-192). It is enabled by
  the property `s3.signer` whose value must literally be `S3V4RestSigner`
  (`SIGNERS = {"S3V4RestSigner": S3V4RestSigner}`). `pyiceberg/io/pyarrow.py`
  contains **no signer code at all**, and `ARROW_FILE_IO` is first in
  `SCHEME_TO_FILE_IO["s3"]`
  ([`pyiceberg/io/__init__.py` L311-320](https://github.com/apache/iceberg-python/blob/main/pyiceberg/io/__init__.py)),
  so a user must set `py-io-impl=pyiceberg.io.fsspec.FsspecFileIO` and install
  `s3fs` for signing to work at all.
- What it POSTs to `{s3.signer.uri or uri}/{s3.signer.endpoint or "v1/aws/s3/sign"}`:

  ```python
  signer_body = {"method": request.method,
                 "region": request.context["client_region"],
                 "uri": request.url,
                 "headers": {key: [val] for key, val in request.headers.items()}}
  ```

  and it reads back `response_json["headers"]` (dict of lists, joined with
  `", "`) and `response_json["uri"]`. **Note it sends no `body` and no
  `provider`** — a signer must treat both as optional.

### 9.2 DuckDB iceberg extension

- **Vended credentials: yes, and it is the default.**
  `IRCAccessDelegationMode access_mode = IRCAccessDelegationMode::VENDED_CREDENTIALS;`
  ([`src/include/iceberg_attach.hpp` L35](https://github.com/duckdb/duckdb-iceberg/blob/main/src/include/iceberg_attach.hpp)),
  with `headers.Insert("X-Iceberg-Access-Delegation", "vended-credentials")` at
  five call sites in
  [`src/catalog/rest/api/catalog_api.cpp`](https://github.com/duckdb/duckdb-iceberg/blob/main/src/catalog/rest/api/catalog_api.cpp).
  It consumes the `storage_credentials` array with longest-prefix matching and
  re-vends on expiry (`ReVendVendedCredentials`,
  [`iceberg_table_secret_provider.cpp` L160-237](https://github.com/duckdb/duckdb-iceberg/blob/main/src/catalog/rest/storage/iceberg_table_secret_provider.cpp)).
- **Remote signing: no.** `ACCESS_DELEGATION_MODE` accepts exactly two values —
  "Unrecognized access mode '%s'. Supported options are 'vended_credentials' and
  'none'" (`src/iceberg_attach.cpp` L272-279), matching the
  [docs](https://duckdb.org/docs/stable/core_extensions/iceberg/iceberg_rest_catalogs).
  A full-repo grep for `s3.signer`, `remote-signing`, `signer.uri` returns
  **zero** hits.
- Auth types are `OAUTH2, SIGV4, NONE` with `OAUTH2` the default
  (`src/include/iceberg_attach.hpp` L14).

**Consequence: DuckDB cannot use a scoped signer.** Its only non-vended mode is
`none`, which falls back to ambient httpfs/S3 secrets — meaning the client must
already hold an R2 credential.

### 9.3 Iceberg Java / iceberg-aws

The reference implementation, covered in §1.5. The point most relevant to
icegate: when `s3.remote-signing-enabled=true`, `applySignerConfiguration`
installs the signer via `SdkAdvancedClientOption.SIGNER` **and**
`getCredentialsProvider` returns `AnonymousCredentialsProvider.create()`
([`S3FileIOProperties.java` L993-997, L1030-1043](https://github.com/apache/iceberg/blob/main/aws/src/main/java/org/apache/iceberg/aws/s3/S3FileIOProperties.java))
— **the client holds no credential at all.** That is precisely the icegate
model, natively supported.

The Java client does not set `X-Iceberg-Access-Delegation` itself; users pass it
as the catalog property `header.X-Iceberg-Access-Delegation`, which is exactly
what Cloudflare's own Spark example does (§8.3).

### 9.4 Summary of client support

| client | vended credentials | remote signing | caveat |
|---|---|---|---|
| Iceberg Java / Spark | yes | **yes, native** | client runs with anonymous credentials |
| PyIceberg | yes (0.12.0 for `storage-credentials` array) | **yes, Fsspec only** | requires `py-io-impl=…FsspecFileIO` + `s3fs`; PyArrow path has no signer |
| DuckDB | yes, default | **no** | only other mode is `none` (ambient creds) |

---

## 10. Synthesis for icegate

### 10.1 Comparison

| system | catalog auth | vends? | vending scope | expiry | signs? | per-table scoping at sign time |
|---|---|---|---|---|---|---|
| Iceberg spec | OAuth2 / bearer; inline token endpoint deprecated for removal | `storage-credentials` | `prefix`, longest-match | **no expiry field in schema** | yes, `…/tables/{table}/sign` | yes — table is in the URL path |
| Polaris 1.7.0 | principal JWT, roles in `scope` | STS AssumeRole | table location prefix | 3600s (cache 1800s) | **no** — resolves then throws | n/a |
| Lakekeeper 0.13.1 | OIDC → OpenFGA check | STS, or R2 temp-credentials API | table location prefix | 3600s default | **yes** | yes — table id in path, then authz on that table |
| Unity Catalog OSS | OIDC + RFC 8693 → internal JWT; jCasbin | own `/temporary-*-credentials` API | table/volume path, `s3:prefix` condition | **1h hard-coded** | no (Iceberg facade is read-only) | n/a |
| AWS Glue / S3 Tables | **SigV4 on the catalog API** | Lake Formation | single prefix, scope-down policy | 900–43200s | no | n/a |
| Nessie 0.108.4 | OIDC bearer + CEL rules | STS, **opt-in, default off** | auto-generated per-location policy | UNVERIFIED | **yes, default on** | yes — write vs read locations, metadata.json blocked |
| Gravitino 1.3.0 | bearer JWT (OAuth/JWKS) | STS `s3-token` etc. | table location + write paths | 3600s | **no** — throws | n/a |
| **Cloudflare R2** | Cloudflare API token as bearer | yes, SigV4 | **inherits the token's storage permissions** | **none observed** | **no** | n/a |

### 10.2 Takeaways

**1. Build the signer on the spec's per-table path, not the legacy one.**
`POST /v1/{prefix}/namespaces/{namespace}/tables/{table}/sign` is in released
Iceberg 1.11.0 and puts the table identity in the URL. Lakekeeper had to invent
its own `/signer/{prefix}/tabular-id/{id}/…` path to get the same property, and
Nessie signs a `{signedParams}` blob for it. icegate is greenfield and can just
use the standard. Keep serving the legacy `v1/aws/s3/sign` too, since that is
still the client-side default constant and PyIceberg's default — the JSON body
is identical, so it is one extra route, not a second implementation.

**2. Validate in this order, and refuse rather than narrow.** Lakekeeper's
ordering is the reference: authorize first, parse second, resolve the table
third, check containment last, and fetch the signing secret only after
everything passes. For icegate's actual case — one public, read-only bucket —
the honest simplification is to **reject every method except GET and HEAD
outright**. That single rule deletes the two hardest validation problems in this
entire survey: `DeleteObjects` body parsing and write-location containment. Say
so explicitly in the ADR rather than leaving it as an accident of the current
implementation.

**3. `ListObjectsV2` is the sharp edge that remains even for read-only.**
Lakekeeper's list handling is the most instructive code in the survey and it is
all about GET requests: require the path to resolve to exactly `s3://{bucket}`,
verify url-decoding did not change the path (`%2F` hides separators, and the URL
parser then collapses `.`/`..` behind them), allowlist the query parameters
exactly (a `?policy` or `?versioning` sub-resource alongside list parameters
makes the signed request return something else entirely), reject duplicates,
require a `prefix`, and compare that prefix as a **raw string** rather than a
path — because S3 does, and a prefix stopping at a table boundary otherwise
enumerates same-prefixed siblings.

**4. Validate the URI as received, sign the URI as received.** Keep the raw and
decoded forms separate, derive locations from the decoded form, and check that
the two agree. A signer that validates a decoded path but signs the raw one is
exploitable; Lakekeeper carries both in one struct precisely to prevent that.

**5. Expiry norm is one hour, and R2's is currently "never".** Every vending
implementation surveyed lands at 3600s (Polaris, Lakekeeper, Gravitino, and Unity
Catalog which hard-codes it). Signing sidesteps the question entirely — a SigV4
signature is minutes-valid and the client must come back — which is the strongest
argument for the signer over any vending scheme. If a vended path is ever needed
for DuckDB, R2's `temp-access-credentials` API (prefix + permission + TTL, the
one Lakekeeper drives) is the way to get an expiry at all.

**6. Emit `Cache-Control: private` on GET/HEAD.** It is what activates the Java
client's 30-second, 100-entry signature cache; `no-cache` forces a round trip per
object. This is the single knob trading Worker request volume against
per-request authorization freshness, and both Lakekeeper and the deprecated spec
document it as the intended contract.

**7. The client matrix, not the threat model, decides whether this ships.**
Iceberg Java/Spark supports the signer natively and runs anonymous; PyIceberg
supports it only if users switch to `FsspecFileIO`; **DuckDB cannot do it at
all.** If DuckDB is a required consumer of the public catalog, the signer cannot
be the only path, and the fallback containment lever is the one in §8.4: issue
the backend token with a *bucket-scoped* `Workers R2 Storage Bucket Item Read`
permission so the inherited vended credential is read-only and cannot leave the
public bucket. That is a smaller change than a signer and should probably happen
first regardless.

**8. Do not depend on the delegation header.** The spec says the server may
supply any or none of the requested mechanisms; the Java client does not send the
header at all; PyIceberg and DuckDB both send `vended-credentials`
unconditionally. icegate should decide from its own policy and let clients cope.
