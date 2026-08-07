# 0008 — NCBI gene attributes live in their own Entrez-keyed table

**Status**: Accepted

## Context

`annotation.gene` is derived from the Ensembl GTF and keyed by Ensembl stable
id. It has no `description`, no cytogenetic band and no NCBI gene type, because
a GTF carries none of them and SPEC.md forbids shipping a column before a source
fills it. NCBI `gene_info` fills all three, and `description` is what OrgDb
serves as `GENENAME`, so acceptance criterion D1 depends on it.

Both SPEC.md and issue #19 were written expecting those columns to appear on
`annotation.gene` once NCBI landed. They do not.

## Decision

NCBI's view of a gene lands in **`annotation.ncbi_gene`**, keyed by
`(gene_id, taxon_id)` where `gene_id` is an Entrez GeneID. `annotation.gene`
stays Ensembl-keyed and gains no columns. A client that wants a description for
an Ensembl gene joins through `annotation.identifier_mapping` on
`ENSEMBL <-> ENTREZ`.

## Why not columns on annotation.gene

The obstacle is the key, not the provenance. `gene_info` is keyed by Entrez id,
and the Entrez↔Ensembl mapping is many-to-many in **both** directions: measured
on the landed `raw.ncbi_gene2ensembl`, 261 of human's 38,284 mapped Ensembl
genes correspond to more than one Entrez gene, and 136 of mouse's 34,832.

Writing `description` onto an Ensembl-keyed row therefore requires picking one
of several NCBI records for those genes, at write time, with no way for a client
to see that a choice was made. That is exactly the collapse SPEC.md D3 forbids
for identifier mapping ("a key with several matches returns **every** match"),
and there is no reason it should be acceptable for attributes when it is a
failure for identifiers. Joining through `identifier_mapping` fans out to two
rows and lets the client decide.

Two secondary consequences point the same way. NCBI and Ensembl disagree about
symbols and about gene type vocabulary (`protein-coding` versus
`protein_coding`); separate tables let both stand unreconciled, which is honest,
where a shared row would force one to win. And OrgDb's own key is ENTREZID, so
serving criterion D from an Entrez-keyed table is a direct read rather than a
reverse join.

## Why not a provider column in the business key

`reference.genome` already handles two providers describing one assembly by
putting `provider` in the business key, and the same shape was considered here:
one `annotation.gene` holding both Ensembl-keyed and Entrez-keyed rows,
distinguished by `provider`, scoped per writer through `ensembl.WRITER`.

Rejected because the two row sets are not alternative descriptions of the same
key space — their `gene_id` values are drawn from different namespaces, so the
table would have a column whose meaning depends on a sibling column's value, and
every existing join (`gene` to `transcript` on `gene_id`) would silently pick up
rows that cannot match. `reference.genome` works because both providers really
do use the same assembly accession. Here they do not.

## Consequences

`GENENAME`, `GENETYPE` and `MAP` cost one join from an Ensembl id, and that join
is many-to-many, which callers must handle. Two tables now answer "what is this
gene", and the comment on each says which sense of the word it means.

`annotation.ncbi_gene` is mostly not genes in the narrow sense: 128,261 of
human's 193,809 records are `gene_type` `biological-region`, against 20,595
protein-coding. They are kept, not filtered — `gene_type` distinguishes them and
OrgDb's ENTREZID key space includes them — but a query that means "genes" has to
say so.

`raw.ncbi_gene_history` is landed and deliberately not interpreted. Whether
supersession is a table, a typed retirement reason, or nothing at all is still
open as issue #15 item 4; the tombstones are in the catalog so that question can
be settled against real data.
