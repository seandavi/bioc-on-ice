// Fixed recipe list — the same worked queries issue #100's MCP `recipes()` tool
// serves. Every SQL string here is self-contained (INSTALL/LOAD/ATTACH included)
// so pasting one verbatim into desktop DuckDB works with no setup.
//
// Verified live against the catalog on 2026-09-18 (`duckdb` CLI, no browser):
// the `verified` field is the row/count DuckDB actually returned that day, not
// a guess — see explorer/README.md for how to re-check.

const ATTACH = `INSTALL iceberg; LOAD iceberg;
ATTACH 'bioconice' AS bioc (
    TYPE ICEBERG,
    ENDPOINT 'https://icegate-bioconice.seandavi.workers.dev',
    AUTHORIZATION_TYPE 'none'
);`;

export const RECIPES = [
  {
    id: "tp53-citers",
    title: "TP53 → papers → who cites them",
    description:
      "Every PubMed record linked to human TP53 (NCBI gene2pubmed), then everyone who cites any of those papers (iCite's citation graph).",
    sql: `${ATTACH}

WITH tp53_papers AS (
  SELECT DISTINCT pubmed_id AS pmid
  FROM bioc.annotation.ncbi__gene_pubmed
  WHERE gene_id = '7157' AND taxon_id = 9606 AND valid_to IS NULL
)
SELECT count(DISTINCT c.citing_pmid) AS citers
FROM bioc.annotation.icite__citation c
JOIN tp53_papers p ON c.cited_pmid = p.pmid
WHERE c.valid_to IS NULL;`,
    verified: "558,501 distinct citing PMIDs",
  },
  {
    id: "blood-tcell-datasets",
    title: "Human blood datasets with any T-cell subtype",
    description:
      "CELLxGENE datasets tagged with UBERON blood (UBERON:0000178) and a cell type that is CL T cell (CL:0000084) or any of its is_a descendants — the rollup is a recursive CTE over ontology.relationship, not a precomputed closure.",
    sql: `${ATTACH}

WITH RECURSIVE t_cell_subtypes AS (
  SELECT 'CL:0000084' AS term_id                 -- T cell, the root
  UNION
  SELECT r.subject_id
  FROM bioc.ontology.relationship r
  JOIN t_cell_subtypes s ON r.object_id = s.term_id
  WHERE r.ontology = 'cl' AND r.predicate = 'is_a' AND r.valid_to IS NULL
),
blood_datasets AS (
  SELECT DISTINCT resource_id                    -- resource_id = dataset_version_id
  FROM bioc.resource.resource_relationship
  WHERE relationship = 'has_tissue' AND target_id = 'UBERON:0000178' AND valid_to IS NULL
),
tcell_datasets AS (
  SELECT DISTINCT resource_id
  FROM bioc.resource.resource_relationship rr
  JOIN t_cell_subtypes t ON rr.target_id = t.term_id
  WHERE rr.relationship = 'has_cell_type' AND rr.valid_to IS NULL
)
SELECT count(DISTINCT d.dataset_id) AS n_datasets
FROM bioc.resource.cellxgene__dataset d
JOIN blood_datasets b ON d.dataset_version_id = b.resource_id
JOIN tcell_datasets t ON d.dataset_version_id = t.resource_id
WHERE d.taxon_id = 9606 AND d.valid_to IS NULL;`,
    verified: "114 datasets",
  },
  {
    id: "gene-models",
    title: "Gene models for a species at the current release",
    description:
      "Every current Ensembl transcript for human, joined back to its gene. Swap taxon_id for another organism, or add valid_from <= 'R' AND (valid_to IS NULL OR valid_to > 'R') for a past release instead of 'current'.",
    sql: `${ATTACH}

SELECT g.gene_id, g.symbol, g.gene_type, t.transcript_id, t.biotype, t.canonical
FROM bioc.annotation.gene g
JOIN bioc.annotation.transcript t USING (gene_id, taxon_id, source)
WHERE g.taxon_id = 9606 AND g.source = 'ENSEMBL'
  AND g.valid_to IS NULL AND t.valid_to IS NULL;`,
    verified: "646,577 transcript rows (human, Ensembl 116)",
  },
  {
    id: "taxon-coverage",
    title: "A taxon's coverage across tables",
    description:
      "How many current rows human (taxon_id 9606) has in each of the main annotation tables — a quick sanity check when onboarding a new organism.",
    sql: `${ATTACH}

SELECT 'annotation.gene' AS tbl, count(*) AS n FROM bioc.annotation.gene WHERE taxon_id = 9606 AND valid_to IS NULL
UNION ALL
SELECT 'annotation.transcript', count(*) FROM bioc.annotation.transcript WHERE taxon_id = 9606 AND valid_to IS NULL
UNION ALL
SELECT 'annotation.exon', count(*) FROM bioc.annotation.exon WHERE taxon_id = 9606 AND valid_to IS NULL
UNION ALL
SELECT 'annotation.ncbi__gene', count(*) FROM bioc.annotation.ncbi__gene WHERE taxon_id = 9606 AND valid_to IS NULL;`,
    verified:
      "gene 78,941 · transcript 646,577 · exon 5,087,789 · ncbi__gene 193,813",
  },
];

export const PROVENANCE_MATRIX_SQL = `${ATTACH}

PIVOT bioc.provenance.release
ON source
USING first(source_version)
GROUP BY release
ORDER BY release;`;
