"""Release-scoped merge, driven by the declared schema.

A derived table holds current state plus history: `first_seen` is the release a
record appeared in, `retired_in` the release it vanished upstream, NULL while
current. Maintaining that against a full upstream dump sorts every record into
one of four states:

  new        absent locally            -> first_seen = this release
  changed    present, attributes differ -> updated, first_seen preserved
  unchanged  present, identical         -> carried forward untouched
  retired    absent upstream            -> retired_in = this release

Which columns are keys and which are attributes comes from the table's own
Iceberg identifier fields, so this works for any table in `schemas.TABLES`
without being told anything about it.

ponytail: this recomputes the scope's complete state and overwrites the scope,
rather than writing only the rows that changed. PyIceberg's `upsert` would do
the latter, but it derives a filter predicate from every join key, which is
pathological past a few thousand rows — five million exons did not finish in
ten minutes, where overwrite takes seconds. Storage is unaffected once
snapshots are expired, since history lives in the rows rather than in retained
files. Upgrade path if write time ever matters: partition by taxon and replace
only the touched partitions.
"""

import duckdb

from . import schemas

VALIDITY = ("first_seen", "retired_in")


def _columns(identifier, schema):
    """Business key and attributes.

    The join key is the *business* key, not the Iceberg identifier fields —
    those additionally carry `first_seen`, because a retired record that
    reappears becomes a second row. Joining on the row key would make every
    record look new.
    """
    keys = list(schemas.TABLES[identifier].business_key)
    attrs = [f.name for f in schema.fields if f.name not in keys and f.name not in VALIDITY]
    return keys, attrs


def _select(schema, side, release):
    """Column list in declared order, sourced per branch of the merge."""
    out = []
    for f in schema.fields:
        n = f.name
        if n == "first_seen":
            # upstream rows keep the release they first appeared in; only a
            # genuinely new key gets stamped with this one
            out.append(f"coalesce(c.first_seen, '{release}')" if side == "upstream"
                       else "c.first_seen")
        elif n == "retired_in":
            out.append("NULL::VARCHAR" if side == "upstream"
                       else f"'{release}'" if side == "retiring" else "c.retired_in")
        else:
            out.append(f"{'i' if side == 'upstream' else 'c'}.\"{n}\"")
        out[-1] += f' AS "{n}"' 
    return ", ".join(out)


def merge(cat, identifier, incoming, release, scope):
    """Merge `incoming` (the complete upstream state within `scope`) into a table.

    `scope` is an Iceberg expression bounding what this ingest is responsible
    for — typically one species. Records outside it are never read and so are
    never wrongly retired.
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
    live = "c.retired_in IS NULL"
    k0 = f'"{keys[0]}"' 

    con.execute(f"""
        CREATE OR REPLACE TABLE merged AS
        SELECT {_select(schema, 'upstream', release)},
               CASE WHEN c.{k0} IS NULL THEN 'new'
                    WHEN {differs} THEN 'changed' ELSE 'unchanged' END AS _state
        FROM inc i LEFT JOIN cur c ON {on} AND {live}
      UNION ALL
        SELECT {_select(schema, 'retiring', release)}, 'retired' AS _state
        FROM cur c LEFT JOIN inc i ON {on}
        WHERE {live} AND i.{k0} IS NULL
      UNION ALL
        SELECT {_select(schema, 'history', release)}, 'history' AS _state
        FROM cur c WHERE c.retired_in IS NOT NULL
    """)

    # Iceberg declares identifier fields but enforces nothing, so the invariant
    # is ours: at most one live row per business key. Violating it corrupts
    # silently — the current view still reads correctly while every join on the
    # key fans out — so fail loudly here instead.
    kq = ", ".join(f'"{k}"' for k in keys)
    dupes = con.sql(f"""
        SELECT count(*) FROM (
            SELECT {kq} FROM merged WHERE retired_in IS NULL GROUP BY ALL HAVING count(*) > 1)
    """).fetchone()[0]
    if dupes:
        raise ValueError(
            f"{identifier}: {dupes} business keys would have more than one live row. "
            f"Either `incoming` contains duplicate keys, or a retired record was "
            f"resurrected without its predecessor staying retired.")

    stats = dict(con.sql("SELECT _state, count(*) FROM merged GROUP BY 1").fetchall())
    cols = ", ".join(f'"{f.name}"' for f in schema.fields)
    final = con.sql(f"SELECT {cols} FROM merged").to_arrow_table()
    table.overwrite(final.cast(table.schema().as_arrow()), overwrite_filter=scope)

    return {"written": sum(stats.get(s, 0) for s in ("new", "changed", "retired")),
            "unchanged": stats.get("unchanged", 0),
            **{s: n for s, n in stats.items() if s != "history"}}
