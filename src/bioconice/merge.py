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

import time

import duckdb
from pyiceberg.exceptions import CommitFailedException, RESTError

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
            if "429" not in str(err) and "TooManyRequests" not in type(err).__name__:
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
    con.register("cur", table.scan(row_filter=scope).to_arrow())

    on = " AND ".join(f'i."{k}" = c."{k}"' for k in keys)
    # IS DISTINCT FROM, so a NULL becoming a value (or the reverse) counts as a
    # change; plain <> would silently treat it as unchanged.
    differs = " OR ".join(f'i."{a}" IS DISTINCT FROM c."{a}"' for a in attrs) or "false"
    live = "c.valid_to IS NULL"
    k0 = f'"{keys[0]}"'
    is_new = f"c.{k0} IS NULL"
    # A new key, or a changed one, starts a version at this release; an
    # unchanged one carries its own start forward.
    opened = f"CASE WHEN {is_new} OR ({differs}) THEN '{release}' ELSE c.valid_from END"
    # Re-ingesting the release that opened the live row is a correction *within*
    # that release, not a new version: replace it, rather than emitting a
    # zero-width interval and a duplicate row key.
    supersede = f"c.valid_from < '{release}'"

    con.execute(f"""
        CREATE OR REPLACE TABLE merged AS
        SELECT {_cols(schema, 'live', release, opened)},
               CASE WHEN {is_new} THEN 'new'
                    WHEN {differs} THEN 'changed' ELSE 'unchanged' END AS _state
        FROM inc i LEFT JOIN cur c ON {on} AND {live}
      UNION ALL
        SELECT {_cols(schema, 'closing', release)}, 'superseded' AS _state
        FROM inc i JOIN cur c ON {on} AND {live}
        WHERE ({differs}) AND {supersede}
      UNION ALL
        SELECT {_cols(schema, 'closing', release)}, 'retired' AS _state
        FROM cur c LEFT JOIN inc i ON {on}
        WHERE {live} AND i.{k0} IS NULL
      UNION ALL
        SELECT {_cols(schema, 'history', release)}, 'history' AS _state
        FROM cur c WHERE c.valid_to IS NOT NULL
    """)

    # Iceberg declares identifier fields and enforces nothing, so both
    # invariants are ours. Violating either corrupts silently: the current view
    # still reads correctly while joins fan out.
    kq = ", ".join(f'"{k}"' for k in keys)
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

    return {"written": sum(stats.get(s, 0) for s in ("new", "changed", "superseded", "retired")),
            "unchanged": stats.get("unchanged", 0),
            **{s: n for s, n in stats.items() if s != "history"}}
