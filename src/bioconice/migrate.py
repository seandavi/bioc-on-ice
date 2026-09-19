"""`bioconice migrate-assembly-scope`: issue #94's one-off rewrite of the live tables.

`genome_id` joined the row key of annotation.gene / transcript / exon, and
Iceberg adds no required identifier column to rows that exist
(`schemas._evolve` refuses), so those three are rebuilt. Not re-ingested: until
#94 production held one assembly per taxon (276 taxa, 276 reference.genome
rows, checked 2026-09-19), so every row's assembly is decided by joining its
(taxon_id, source) to reference.genome. A pair that maps to no genome or to
more than one stops the run before anything is written.

Rows are copied as they are — valid_from and valid_to included, so a 2026.08
point-in-time query answers as before — one (taxon_id, source) per commit, so
the 143M-row exon table never sits in memory (its largest chunk is human's
5.1M rows). Every step looks at what is there and does only what is missing,
so a run that died is simply run again, and a second run does nothing:

  reference.genome    in place: `is_canonical` arrives by schema evolution and
                      is true on every row (each was its taxon's only
                      assembly); the 13 rows Ensembl gives no accession for
                      get their assembly name as genome_id instead of ''.
  gene/transcript/exon  built as `<table>__v2` in the new shape, counted
                      against the original per (taxon, source, valid_from,
                      valid_to), then swapped in by two renames; the original
                      stays as `<table>__v1` until someone drops it.
                      `copy_swap` is for a catalog that cannot rename: drop the
                      original, recreate it, copy `__v2` back (which then
                      stays as the backup).
  raw.ensembl__gtf    in place: genome_id is an optional column there, filled
                      one taxon per commit.
  identifier_mapping  source 'Ensembl' -> 'ENSEMBL' (the Ensembl half of
                      #153), one commit.

PyIceberg does every write; DuckDB is not involved (AGENTS.md).
"""

from collections import Counter

import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import AlwaysTrue, And, EqualTo, IsNull

from . import merge, schemas
from .ensembl import SOURCE

FEATURES = ("annotation.gene", "annotation.transcript", "annotation.exon")


def _load(cat, name):
    try:
        return cat.load_table(name)
    except NoSuchTableError:
        return None


def _counts(table, fields, row_filter=AlwaysTrue()):
    """Rows per distinct `fields` tuple, streamed: only those columns are read."""
    n = Counter()
    for batch in table.scan(row_filter=row_filter, selected_fields=fields).to_arrow_batch_reader():
        for r in pa.Table.from_batches([batch]).group_by(list(fields)).aggregate(
                [([], "count_all")]).to_pylist():
            n[tuple(r[f] for f in fields)] += r["count_all"]
    return n


def _constant(rows, name, value):
    col = pa.repeat(value, rows.num_rows)
    i = rows.schema.get_field_index(name)
    return rows.append_column(name, col) if i < 0 else rows.set_column(i, name, col)


def _write(cat, name, table, rows, scope):
    names = [f.name for f in table.schema().fields]
    merge.overwrite(cat, name, table, rows.select(names).cast(table.schema().as_arrow()), scope)


def _describe(table, identifier):
    """The declared comment and column docs, onto a table changed in place.

    `schemas.create` only adds columns, and these three tables' descriptions
    changed with #94 (identifier_mapping's says which assembly Ensembl's rows
    are for); the rebuilt ones get theirs by being created.
    """
    d = schemas.TABLES[identifier]
    docs = {f.name: f.doc for f in d.schema.fields}
    stale = [f.name for f in table.schema().fields if docs.get(f.name, f.doc) != f.doc]
    if stale:
        with table.update_schema() as update:
            for name in stale:
                update.update_column(name, doc=docs[name])
    if table.properties.get("comment") != d.comment:
        with table.transaction() as tx:
            tx.set_properties(comment=d.comment)


def _genome(cat):
    """reference.genome, in place; returns {(taxon_id, source): {genome_id}}."""
    table = schemas.create(cat, "reference.genome")          # evolves is_canonical in
    _describe(table, "reference.genome")
    rows = table.scan().to_arrow().to_pylist()
    stale = [r for r in rows if r["is_canonical"] is None or not r["genome_id"]]
    for r in stale:
        r["genome_id"] = r["genome_id"] or r["assembly_name"]
        r["is_canonical"] = True
    genomes = {}
    for r in rows:
        genomes.setdefault((r["taxon_id"], r["source"]), set()).add(r["genome_id"])
    many = {k: sorted(v) for k, v in genomes.items() if len(v) > 1}
    if stale and many:
        raise SystemExit(f"reference.genome: more than one assembly for {many}; which one is "
                         "canonical, and which one the existing rows belong to, is not decidable")
    if stale:
        _write(cat, "reference.genome", table,
               pa.Table.from_pylist(rows, schema=table.schema().as_arrow()), AlwaysTrue())
    print(f"{'reference.genome':32} {len(rows):>12,} rows, {len(stale):,} rewritten")
    return genomes


def _genome_of(genomes, identifier, taxon, source):
    ids = genomes.get((taxon, source), ())
    if len(ids) != 1:
        raise SystemExit(f"{identifier}: rows of taxon {taxon}, source {source} map to "
                         f"{len(ids)} reference.genome rows {sorted(ids)}; need exactly one")
    return next(iter(ids))


KEY = ("taxon_id", "source", "valid_from", "valid_to")


def _only(counts, pair):
    return {k: n for k, n in counts.items() if k[:2] == pair}


def _copy(cat, src, identifier, name, genomes):
    """Copy `src` into `name` (declared as `identifier`), filling genome_id where absent."""
    dst = schemas.create(cat, identifier, name)
    want, have = _counts(src, KEY), _counts(dst, KEY)
    pairs = sorted({k[:2] for k in want})
    fill = "genome_id" not in src.schema().column_names
    if fill:
        for pair in pairs:                                   # all of them, before any write
            _genome_of(genomes, identifier, *pair)
    # ponytail: one commit per (taxon, source) — 276 a table. If the catalog's
    # write rate limit makes that slow, put several small taxa in one commit.
    for taxon, source in pairs:
        if _only(want, (taxon, source)) == _only(have, (taxon, source)):
            continue                                         # copied by an earlier run
        scope = And(EqualTo("taxon_id", taxon), EqualTo("source", source))
        rows = src.scan(row_filter=scope).to_arrow()
        if fill:
            rows = _constant(rows, "genome_id", _genome_of(genomes, identifier, taxon, source))
        _write(cat, name, dst, rows, scope)
    have = _counts(cat.load_table(name), KEY)
    if have != want:
        diff = {k: (want.get(k), have.get(k)) for k in want.keys() | have.keys()
                if want.get(k) != have.get(k)}
        raise SystemExit(f"{name}: row counts differ from the source (want, have): {diff}")
    closed = sum(n for k, n in want.items() if k[3] is not None)
    print(f"{name:32} {sum(want.values()):>12,} rows before, {sum(have.values()):>12,} after "
          f"({closed:,} closed), {len(pairs)} (taxon, source) scopes")


def _rebuild(cat, identifier, genomes, copy_swap):
    v1, v2 = identifier + "__v1", identifier + "__v2"
    old = _load(cat, identifier)
    if old is None and _load(cat, v2) is None:
        return                                               # nothing was ever loaded
    if old is not None and "genome_id" not in old.schema().column_names:
        _copy(cat, old, identifier, v2, genomes)
        # What a rollback needs if the backup table is gone too:
        # cat.register_table(identifier, <this location>).
        print(f"{identifier}: old shape was at {old.metadata_location}")
        if copy_swap:
            cat.drop_table(identifier)
        else:
            schemas.rate_limited(lambda: cat.rename_table(identifier, v1))
    new = _load(cat, v2)
    if new is None:
        print(f"{identifier:32} already has genome_id")
    elif copy_swap:
        _copy(cat, new, identifier, identifier, genomes)
    else:
        schemas.rate_limited(lambda: cat.rename_table(v2, identifier))


def _per_taxon(counts):
    out = Counter()
    for (taxon, _), n in counts.items():
        out[taxon] += n
    return out


def _raw(cat, genomes):
    name = "raw.ensembl__gtf"
    if _load(cat, name) is None:
        return
    table = schemas.create(cat, name)                        # evolves genome_id in
    _describe(table, name)
    before = _counts(table, ("taxon_id", "genome_id"))
    todo = sorted(t for t, g in before if g is None)
    for taxon in todo:
        _genome_of(genomes, name, taxon, SOURCE)
    for taxon in todo:
        scope = And(EqualTo("taxon_id", taxon), IsNull("genome_id"))
        rows = table.scan(row_filter=scope).to_arrow()
        _write(cat, name, table,
               _constant(rows, "genome_id", _genome_of(genomes, name, taxon, SOURCE)), scope)
        table = cat.load_table(name)
    after = _counts(table, ("taxon_id", "genome_id")) if todo else before
    if _per_taxon(after) != _per_taxon(before) or any(g is None for _, g in after):
        raise SystemExit(f"{name}: per-taxon row counts changed, or genome_id is still NULL")
    print(f"{name:32} {sum(before.values()):>12,} rows before, {sum(after.values()):>12,} after, "
          f"{len(todo)} taxa filled")


def _respell(cat):
    name = "annotation.identifier_mapping"
    table = _load(cat, name)
    if table is None:
        return
    _describe(table, name)
    legacy = EqualTo("source", "Ensembl")
    rows = table.scan(row_filter=legacy).to_arrow()
    if rows.num_rows:
        # An Ensembl ingest run with the new code before this migration asserts
        # the same mappings again under 'ENSEMBL'; respelling would double them.
        if sum(_counts(table, ("source",), EqualTo("source", SOURCE)).values()):
            raise SystemExit(f"{name}: rows under both 'Ensembl' and '{SOURCE}'; an ingest ran "
                             "before this migration — sort those taxa out by hand first")
        _write(cat, name, table, _constant(rows, "source", SOURCE), legacy)
        n = sum(_counts(cat.load_table(name), ("source",), EqualTo("source", SOURCE)).values())
        if n != rows.num_rows:
            raise SystemExit(f"{name}: {rows.num_rows:,} 'Ensembl' rows became {n:,} '{SOURCE}'")
    print(f"{name:32} {rows.num_rows:>12,} rows respelled 'Ensembl' -> '{SOURCE}'")


def assembly_scope(cat, copy_swap=False):
    genomes = _genome(cat)
    for identifier in FEATURES:
        _rebuild(cat, identifier, genomes, copy_swap)
    _raw(cat, genomes)
    _respell(cat)
