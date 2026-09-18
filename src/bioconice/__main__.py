import argparse

from . import bedbase, bugsigdb, catalog, cellxgene, ensembl, hgnc, icite, mane, ncbi, ncbi_go, obo
from . import ncbi_accession, ncbi_orthologs, ncbi_pubmed


def _print(counts):
    for name, c in counts.items():
        print(f"{name:40} {c['written']:>10,} written  {c['unchanged']:>10,} unchanged"
              if isinstance(c, dict) else f"{name:40} {c:>10,} rows")


def main():
    p = argparse.ArgumentParser(prog="bioconice")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest-ensembl", help="land one species of one Ensembl release, then derive")
    ing.add_argument("species", help="e.g. homo_sapiens")
    ing.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    ing.add_argument("--ensembl-release", default="116")
    ing.add_argument("--transform-only", action="store_true",
                     help="re-derive from already-landed raw rows, without re-downloading")

    bs = sub.add_parser("ingest-bugsigdb", help="land a BugSigDB export release (no transform yet)")
    bs.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    bs.add_argument("--version", default=bugsigdb.DEFAULT_VERSION,
                    help="BugSigDBExports release tag, e.g. v1.3.1. Tags are immutable; "
                         "the devel branch re-exports hourly and is not")

    # The NCBI ingests share a CLI shape: land whole, then derive — every taxon
    # in the dump by default, or only the ones named.
    ncbi_cmds = {
        "ingest-ncbi": (ncbi, "land the NCBI Gene dumps whole, then derive"),
        "ingest-ncbi-accession": (ncbi_accession, "land NCBI gene2accession whole, then derive"),
        "ingest-ncbi-pubmed": (ncbi_pubmed, "land NCBI gene2pubmed whole, then derive"),
        "ingest-gene2go": (ncbi_go, "land NCBI gene2go whole, then derive GO annotations"),
        "ingest-ncbi-orthologs": (ncbi_orthologs, "land NCBI gene_orthologs and gene_group whole, "
                                  "then derive ortholog pairs"),
    }
    for cmd, (_, help_text) in ncbi_cmds.items():
        c = sub.add_parser(cmd, help=help_text)
        c.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
        c.add_argument("--taxa", help="comma-separated taxa to DERIVE annotation for "
                       "(default: every taxon in the dump); raw is always landed whole")

    ic = sub.add_parser("ingest-icite", help="land the monthly iCite snapshot whole, then derive")
    ic.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    ic.add_argument("--snapshot", help="iCite snapshot label, e.g. 2026-08 (default: latest on Figshare)")
    ic.add_argument("--csv", help="an already-extracted icite_metadata.csv; skips download")

    cx = sub.add_parser("ingest-cellxgene", help="land the CELLxGENE Discover dataset listing whole, then derive")
    cx.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    cx.add_argument("--json", help="an already-fetched datasets listing JSON; skips the API call")
    cx.add_argument("--census-release", help="Census build to reference, e.g. 2025-11-08 "
                     "(default: the release manifest's 'stable' LTS alias)")
    ob = sub.add_parser("ingest-obo", help="land one OBO ontology's release, then derive term + relationship")
    ob.add_argument("ontology", choices=sorted(obo.REGISTRY),
                    help="cl, uberon, mondo, efo, hsapdv, mmusdv, go, doid")
    ob.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    ob.add_argument("--url", help="an already-downloaded OBO Graphs JSON file or alternate URL; "
                    "skips the registry URL (how offline tests stay offline)")

    bb = sub.add_parser("ingest-bedbase",
                        help="land BEDbase's bed/bedset listings, then derive resource entries")
    bb.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    bb.add_argument("--limit", type=int,
                    help="bound the crawl to about this many records per endpoint (default: "
                         "the full 663k bed / 22k bedset listings); a bounded run never "
                         "retires records outside what it fetched")

    hg = sub.add_parser("ingest-hgnc", help="land the HGNC complete set whole, then derive")
    hg.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    hg.add_argument("--url", help="a dated archive (hgnc_complete_set_YYYY-MM-DD.txt, citable: its "
                    "date becomes the version) or a local copy; default is the rolling file, "
                    "versioned by retrieval date")

    mn = sub.add_parser("ingest-mane", help="land the MANE summary whole, then derive")
    mn.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    mn.add_argument("--url", help="a specific release's MANE.GRCh38.vX.Y.summary.txt.gz, or a local "
                    "copy; the version is read from the file name (default: the newest release)")

    sub.add_parser("tables", help="list catalog tables")
    args = p.parse_args()

    cat = catalog()
    if args.cmd == "ingest-ensembl":
        if args.transform_only:
            info = ensembl.species_info(args.ensembl_release, args.species)
            counts = ensembl.transform(cat, args.release, info, args.ensembl_release)
        else:
            counts = ensembl.ingest(cat, args.release, args.species, args.ensembl_release)
        _print(counts)
    elif args.cmd == "ingest-bugsigdb":
        n = bugsigdb.land_raw(cat, args.release, args.version)
        print(f"{'raw.bugsigdb__full_dump':40} {n:>10,} rows  ({args.version})")
    elif args.cmd == "ingest-icite":
        _print(icite.ingest(cat, args.release, args.snapshot, args.csv))
    elif args.cmd == "ingest-cellxgene":
        _print(cellxgene.ingest(cat, args.release, json_path=args.json,
                                census_release=args.census_release))
    elif args.cmd == "ingest-obo":
        _print(obo.ingest(cat, args.release, args.ontology, args.url))
    elif args.cmd == "ingest-bedbase":
        _print(bedbase.ingest(cat, args.release, args.limit))
    elif args.cmd == "ingest-hgnc":
        _print(hgnc.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-mane":
        _print(mane.ingest(cat, args.release, args.url))
    elif args.cmd in ncbi_cmds:
        module = ncbi_cmds[args.cmd][0]
        taxa = [int(t) for t in args.taxa.split(",")] if args.taxa else None
        _print(module.ingest(cat, args.release, taxa))
    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
