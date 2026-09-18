"""explorer/public/recipes.js's SQL parses in DuckDB.

Offline and syntax-only, per AGENTS.md (tests stay offline; no network call to
icegate here). `extract_statements` parses without binding, so this catches a
typo or unbalanced clause without needing the tables — or even a network
connection — to exist. The live numbers in recipes.js's `verified` fields were
checked by hand against the real catalog (see explorer/README.md) and are not
re-verified here.

Reads the SQL out of the JS file with regexes rather than duplicating it in
Python, so there is exactly one place these queries are written down
(SPEC.md's "one declarative source" principle, applied to the client instead
of the schema).
"""

import re
from pathlib import Path

import duckdb

RECIPES_JS = Path(__file__).parent.parent / "explorer" / "public" / "recipes.js"


def _extract_template(text, name):
    """Pull out `const <name> = \\`...\\`;` (or `export const`), backtick-delimited."""
    m = re.search(rf"(?:export )?const {name} = `(.*?)`;", text, re.DOTALL)
    assert m, f"could not find template literal {name!r} in {RECIPES_JS}"
    return m.group(1)


def _sql_statements():
    text = RECIPES_JS.read_text()
    attach = _extract_template(text, "ATTACH")

    sqls = {}
    for m in re.finditer(r'id:\s*"([\w-]+)".*?sql:\s*`(.*?)`,\s*\n\s*verified:', text, re.DOTALL):
        recipe_id, sql = m.groups()
        sqls[recipe_id] = sql.replace("${ATTACH}", attach)

    sqls["provenance-matrix"] = _extract_template(text, "PROVENANCE_MATRIX_SQL").replace(
        "${ATTACH}", attach
    )
    return sqls


RECIPE_SQL = _sql_statements()


def test_found_all_recipes():
    # 4 recipes (issue #101 / #100) + the provenance matrix.
    assert set(RECIPE_SQL) == {
        "tp53-citers",
        "blood-tcell-datasets",
        "gene-models",
        "taxon-coverage",
        "provenance-matrix",
    }


def test_recipe_sql_parses():
    con = duckdb.connect()
    for recipe_id, sql in RECIPE_SQL.items():
        try:
            statements = con.extract_statements(sql)
        except duckdb.ParserException as e:
            raise AssertionError(f"recipe {recipe_id!r} does not parse: {e}") from e
        assert statements, f"recipe {recipe_id!r} produced no statements"
