"""`bioconice parity`: a release bundle against the warehouse it was built from, offline."""

from bioconice import parity, publish
from test_publish import ONLY, con  # noqa: F401 (con is the fixture)

EXONS = (1, 'SELECT transcript_id, rank, start, "end" FROM bioc.annotation.exon WHERE valid_to IS NULL')
# unqualified, as the README's provenance query is
PROVENANCE = (2, "SELECT source, artifact, row_count FROM provenance.release")


def test_bundle_matches_the_catalog_it_came_from(con, tmp_path, capsys):  # noqa: F811
    out = tmp_path / "release"
    publish.publish(con, out, out.as_uri(), ONLY)
    capsys.readouterr()
    assert parity.parity(con, out, [EXONS, PROVENANCE])
    report = capsys.readouterr().out
    assert "| `annotation.exon` |" in report and "MISMATCH" not in report and report.endswith("PASS\n")


def test_a_write_after_the_build_fails_the_gate(con, tmp_path, capsys):  # noqa: F811
    out = tmp_path / "release"
    publish.publish(con, out, out.as_uri(), ONLY)
    con.execute("INSERT INTO bioc.provenance.release SELECT * FROM bioc.provenance.release LIMIT 1")
    capsys.readouterr()
    assert not parity.parity(con, out, [EXONS, PROVENANCE])
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split(" | ")[0] for ln in lines if ln.endswith("MISMATCH |")] == [
        "| `provenance.release`", "| README query, line 2"]
    assert lines[-1] == "FAIL"


def test_readme_queries_include_the_txdb_query():
    queries = [sql for _, sql in parity.readme_queries()]
    assert any("g.symbol = 'TP53'" in q for q in queries)
    assert not any(q.lstrip().upper().startswith("INSTALL") for q in queries)
