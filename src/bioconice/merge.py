"""Release-scoped merge maintaining full Type 2 history.

A row is one **version** of a record, valid over `[valid_from, valid_to)` in
biocOnIce release coordinates. Merging the complete upstream state for a scope
sorts every record into one of five outcomes:

  new         business key absent      -> insert, valid_from = this release
  unchanged   present, attrs identical -> carried forward untouched
  changed     present, attrs differ    -> close the old row at this release
                                          and insert a new version
  retired     absent upstream          -> close the old row at this release
  history     already closed           -> untouched

Within the release being built, all of that is a draft: a row opened at this
release is replaced rather than superseded, one retired at this release that
comes back identical is reopened, and one opened and retired at this release
is dropped. Only earlier releases are history.

Nothing is ever updated in place. That is the point: overwriting a changed
attribute is Kimball Type 1, which destroys history, and it is what made a
point-in-time query return transcripts pointing at genes that did not yet
exist. Closing and reopening also collapses "changed" and "reappeared" into one
rule rather than two — see ADR-0006.

Which columns form the business key comes from the table's own declaration, so
this works for any table in `schemas.TABLES` without being told about it.

ponytail: recomputes the scope's complete state and overwrites the scope rather
than writing only changed rows. PyIceberg's `upsert` derives a filter predicate
from every join key and does not complete on five million rows. Storage is
unaffected once snapshots expire, because history lives in the rows. Upgrade
path if write time matters — first choice: DuckDB MERGE INTO pushed down
against the Iceberg REST catalog (duckdb-iceberg supports MERGE per the DuckDB
release notes), gated on two verifications: it must work through icegate
against R2 Data Catalog (beta; delete-file support unverified), and the
offline test substrate — the local sqlite PyIceberg catalog — cannot take
DuckDB writes, so the local path stays PyIceberg regardless. Second choice:
go insert-only and compute `valid_to` as a `LEAD()` window in a view, which
is where Data Vault has moved.
"""

import re
import time
from datetime import datetime, timezone

import duckdb
import pyarrow as pa
from pyiceberg.exceptions import CommitFailedException, RESTError
from pyiceberg.expressions import AlwaysTrue, And, EqualTo, IsNull

from . import schemas


def overwrite(cat, identifier, table, arrow, overwrite_filter):
    """One filtered overwrite, riding out the two transient commit failures.

    Two production loads commit to the same R2 Data Catalog at once — every
    ingest writes its manifest row to provenance.release, and the catalog
    rate-limits writes catalog-wide — so a commit can fail for reasons that
    have nothing to do with the data: a 429, or another writer's snapshot
    landing first (CommitFailedException). Both are safe to retry because the
    overwrite is a filtered replace of the scope: on a conflict the table is
    reloaded so the retry commits against the new current snapshot.
    """
    for attempt in range(8):
        try:
            table.overwrite(arrow, overwrite_filter=overwrite_filter)
            return
        except CommitFailedException:
            time.sleep(2 ** attempt)
            table = cat.load_table(identifier)
        except RESTError as err:
            if not schemas.is_rate_limit(err):
                raise
            time.sleep(65)
            table = cat.load_table(identifier)
    raise RuntimeError(f"{identifier}: commit still failing after {attempt + 1} retries")

VALIDITY = ("valid_from", "valid_to")


def _columns(identifier, schema):
    """Business key and attributes.

    The join key is the *business* key, not the Iceberg identifier fields —
    those additionally carry `valid_from`, since each change opens a new
    version. Joining on the row key would make every record look new.
    """
    keys = list(schemas.TABLES[identifier].business_key)
    attrs = [f.name for f in schema.fields if f.name not in keys and f.name not in VALIDITY]
    return keys, attrs


def _cols(schema, side, release, opened=None):
    """Column list in declared order, sourced per branch of the merge.

    `live` reads the incoming row; `closing` and `history` read the stored one.
    """
    out = []
    for f in schema.fields:
        n = f.name
        if n == "valid_from":
            out.append(opened if side == "live" else "c.valid_from")
        elif n == "valid_to":
            out.append(f"'{release}'" if side == "closing"
                       else "c.valid_to" if side == "history" else "NULL::VARCHAR")
        else:
            out.append(f'{"i" if side == "live" else "c"}."{n}"')
        out[-1] += f' AS "{n}"'
    return ", ".join(out)


def merge(cat, identifier, incoming, release, scope):
    """Merge `incoming` — the complete upstream state within `scope` — into a table.

    `scope` bounds what this ingest is responsible for, typically one species.
    Records outside it are never read and so are never wrongly retired.
    """
    table = schemas.create(cat, identifier)
    schema = table.schema()
    keys, attrs = _columns(identifier, schema)

    con = duckdb.connect()
    con.register("inc", incoming)
    con.register("stored", table.scan(row_filter=scope).to_arrow())

    on = " AND ".join(f'i."{k}" = c."{k}"' for k in keys)
    # IS DISTINCT FROM, so a NULL becoming a value (or the reverse) counts as a
    # change; plain <> would silently treat it as unchanged.
    differs = " OR ".join(f'i."{a}" IS DISTINCT FROM c."{a}"' for a in attrs) or "false"
    live = "c.valid_to IS NULL"
    k0 = f'"{keys[0]}"'
    kq = ", ".join(f'"{k}"' for k in keys)

    # The release being merged is still under construction, so anything recorded
    # in it is a draft that a later merge of the same release may correct:
    #   - a live row opened at this release is replaced, not superseded;
    #   - a row retired at this release that reappears identical is reopened,
    #     not re-created as a new version (its interval never really closed);
    #   - a row opened and retired at this release is dropped, since it existed
    #     in no release; the same goes for such zero-width rows already stored.
    # Only earlier releases are history. This is what makes the merge safe to
    # rerun within a release, and what lets a mistaken ingest be undone by the
    # correct one (2026-09-18: alternate Ensembl assemblies retiring the
    # canonical one's genes under a shared taxon id).
    con.execute("CREATE OR REPLACE TABLE cur AS "
                "SELECT * FROM stored WHERE valid_from IS DISTINCT FROM valid_to")
    # One stored row per incoming key to match against, by priority: a row
    # retired at this release that is identical (reopen it, and thereby drop any
    # replacement opened at this release), else the live row, else a row retired
    # at this release that differs (history; the incoming opens a new version).
    keq = " AND ".join(f'l."{k}" = c."{k}"' for k in keys)
    con.execute(f"""
        CREATE OR REPLACE TABLE cand AS
        SELECT c.*, CASE WHEN c.valid_to = '{release}' AND NOT ({differs}) THEN 0
                         WHEN c.valid_to IS NULL THEN 1 ELSE 2 END AS _prio
        FROM cur c JOIN inc i ON {on}
        WHERE c.valid_to IS NULL OR c.valid_to = '{release}'
        QUALIFY row_number() OVER (PARTITION BY {", ".join(f'c."{k}"' for k in keys)} ORDER BY _prio) = 1
    """)
    is_new = f"c.{k0} IS NULL"
    reopen = f"c.valid_to = '{release}' AND NOT ({differs})"
    # A new key, a changed one, or a retired one coming back changed starts a
    # version at this release; unchanged and reopened rows keep their start.
    opened = (f"CASE WHEN {is_new} OR ({differs}) THEN '{release}' ELSE c.valid_from END")
    supersede = f"c.valid_from < '{release}'"

    con.execute(f"""
        CREATE OR REPLACE TABLE merged AS
        SELECT {_cols(schema, 'live', release, opened)},
               CASE WHEN {is_new} THEN 'new'
                    WHEN {reopen} THEN 'reopened'
                    WHEN {differs} THEN 'changed' ELSE 'unchanged' END AS _state
        FROM inc i LEFT JOIN cand c ON {on}
      UNION ALL
        SELECT {_cols(schema, 'closing', release)}, 'superseded' AS _state
        FROM inc i JOIN cur c ON {on} AND {live}
        WHERE ({differs}) AND {supersede}
      UNION ALL
        SELECT {_cols(schema, 'closing', release)}, 'retired' AS _state
        FROM cur c LEFT JOIN inc i ON {on}
        WHERE {live} AND i.{k0} IS NULL AND {supersede}
      UNION ALL
        SELECT {_cols(schema, 'history', release)}, 'history' AS _state
        FROM cur c
        WHERE c.valid_to IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM cand l WHERE l._prio = 0 AND {keq}
                          AND l.valid_from = c.valid_from)
    """)

    # Iceberg declares identifier fields and enforces nothing, so both
    # invariants are ours. Violating either corrupts silently: the current view
    # still reads correctly while joins fan out.
    for what, sql in (
        ("more than one live row",
         f"SELECT {kq} FROM merged WHERE valid_to IS NULL GROUP BY ALL HAVING count(*) > 1"),
        ("duplicate row keys",
         f"SELECT {kq}, valid_from FROM merged GROUP BY ALL HAVING count(*) > 1"),
    ):
        n = con.sql(f"SELECT count(*) FROM ({sql})").fetchone()[0]
        if n:
            raise ValueError(f"{identifier}: {n} business keys would have {what}. Either "
                             f"`incoming` contains duplicate keys, or a version boundary "
                             f"is wrong.")

    stats = dict(con.sql("SELECT _state, count(*) FROM merged GROUP BY 1").fetchall())
    cols = ", ".join(f'"{f.name}"' for f in schema.fields)
    final = con.sql(f"SELECT {cols} FROM merged").to_arrow_table()
    overwrite(cat, identifier, table, final.cast(table.schema().as_arrow()), scope)

    return {"written": sum(stats.get(s, 0) for s in ("new", "changed", "reopened", "superseded", "retired")),
            "unchanged": stats.get("unchanged", 0),
            **{s: n for s, n in stats.items() if s != "history"}}


def write(cat, identifier, arrow, overwrite_filter):
    """Create-if-missing, cast to the declared schema, overwrite the filter's rows."""
    table = schemas.create(cat, identifier)
    # Casting to the declared schema is the check: a column we failed to produce,
    # or a null in an identifier field, fails here rather than landing quietly.
    overwrite(cat, identifier, table, arrow.cast(table.schema().as_arrow()), overwrite_filter)
    return arrow.num_rows


def manifest(cat, release, source, artifact, url, rows, version=None, method="retrieval_date",
             checksum=None):
    """Record what this release was built from — ADR-0007.

    `source` is the provider and `artifact` the one thing of its that was read:
    the species for ensembl, the ontology for obo, otherwise the file, named as
    its raw table is after the `__`. Each ingest replaces only its own
    (release, source, artifact) row, so 276 species or three dumps no longer
    overwrite one another (#96). A source with no version of its own (NCBI) is
    versioned by the retrieval date; one with a real, citable release label
    passes `version` and method="release_number", so the version the source
    itself uses is kept.
    """
    now = datetime.now(timezone.utc)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, '{source}' AS source,
               '{version or now.date()}' AS source_version,
               '{method}' AS version_method,
               '{now.isoformat(timespec="seconds")}' AS retrieved_at,
               '{url}' AS url, {repr(checksum) if checksum else 'NULL'}::VARCHAR AS checksum, {rows}::BIGINT AS row_count,
               {repr(artifact) if artifact else 'NULL'}::VARCHAR AS artifact
    """).to_arrow_table()
    # A manifest row states what a completed ingest used; it is not versioned,
    # so it is replaced wholesale for its key rather than merged. The match is
    # null-safe because rows from before #96 have no artifact and `=` never
    # matches a NULL: such a row is replaced only by another artifact-less
    # write, never by one file's row — splitting it is migrate_manifest's job.
    write(cat, "provenance.release", arrow,
          And(EqualTo("release", release), EqualTo("source", source),
              EqualTo("artifact", artifact) if artifact else IsNull("artifact")))


# Source keys as written before #96, each naming one file -> (source, artifact).
# Absent on purpose: ncbi_gene, gwas_catalog and eqtlcatalogue rows summed
# several files, so no single artifact is true of them and they stay NULL.
_LEGACY = {
    "ncbi_gene2go": ("ncbi_gene", "gene2go"),
    "ncbi_gene2accession": ("ncbi_gene", "gene2accession"),
    "ncbi_gene2pubmed": ("ncbi_gene", "gene2pubmed"),
    "ncbi_gene_orthologs": ("ncbi_gene", "gene_orthologs"),
    "ncbi_gene_group": ("ncbi_gene", "gene_group"),
    "bedbase_bed": ("bedbase", "metadata"),
    "bedbase_bedset": ("bedbase", "bedsets"),
    "bedbase_bedset_membership": ("bedbase", "bedset_membership"),
    "cellxgene": ("cellxgene", "dataset"),
    "cellxgene_census": ("cellxgene", "census"),
    "rnacentral": ("rnacentral", "id_mapping"),
    "bugsigdb": ("bugsigdb", "full_dump"),
    "biogrid": ("biogrid", "interactions"),
    "cellosaurus": ("cellosaurus", "release"),
    "intact": ("intact", "mitab"),
    "complexportal": ("complexportal", "complex"),
    "icite": ("icite", "metadata"),
    "hgnc": ("hgnc", "complete_set"),
    "mane": ("mane", "mane_summary"),
    "wikipathways": ("wikipathways", "gmt"),
}
# Sources that minted a key per file: obo_cl -> (obo, cl).
_LEGACY_PREFIXES = ("obo", "pubtator3", "encode")


def _legacy_key(source, url):
    """(source, artifact) for a pre-#96 row; artifact None where none is derivable."""
    if source == "ensembl":
        # The one row per release is whichever species landed last; its URL says which.
        species = re.search(r"/gtf/([^/]+)/", url or "")
        return source, species and species.group(1)
    if source in _LEGACY:
        return _LEGACY[source]
    prefix, _, rest = source.partition("_")
    return (prefix, rest) if prefix in _LEGACY_PREFIXES and rest else (source, None)


def migrate_manifest(cat):
    """One-off for #96: give the rows written under (release, source) their artifact.

    Only `source` and `artifact` of a row without an artifact change; every
    other field, and every row that already has one, is left as it is — so a
    second run finds nothing to do and writes nothing. Where an ingest since
    #96 has already written the key a legacy row maps to, the legacy row is the
    stale one and is dropped. The table is a few hundred rows, so it is
    rewritten whole, by PyIceberg like every other write — which is why it must
    run while no ingest does: a manifest row committed between the read and
    the rewrite here would be lost.
    """
    table = schemas.create(cat, "provenance.release")  # _evolve adds `artifact`
    if table.schema().identifier_field_ids:
        # (release, source) is no longer unique, and Iceberg takes no optional
        # column as an identifier — see TableDef.iceberg_schema.
        with table.update_schema() as update:
            update.set_identifier_fields()
    rows = table.scan().to_arrow().to_pylist()
    taken = {(r["release"], r["source"], r["artifact"]) for r in rows if r["artifact"]}
    out, stats = [], {"rows_before": len(rows), "backfilled": 0, "dropped_superseded": 0}
    for r in rows:
        if not r["artifact"]:
            source, artifact = _legacy_key(r["source"], r["url"])
            if (r["release"], source, artifact) in taken:
                stats["dropped_superseded"] += 1
                continue
            if artifact:
                r = {**r, "source": source, "artifact": artifact}
                stats["backfilled"] += 1
        out.append(r)
    stats["rows_after"] = len(out)
    stats["left_without_artifact"] = sum(not r["artifact"] for r in out)
    if stats["backfilled"] or stats["dropped_superseded"]:
        overwrite(cat, "provenance.release", table,
                  pa.Table.from_pylist(out, schema=table.schema().as_arrow()), AlwaysTrue())
    return stats
