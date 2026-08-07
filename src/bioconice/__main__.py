import argparse

from . import bugsigdb, catalog, ensembl, ncbi
from . import ncbi_pubmed


def main():
    p = argparse.ArgumentParser(prog="bioconice")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest-ensembl", help="land one species of one Ensembl release, then derive")
    ing.add_argument("species", help="e.g. homo_sapiens")
    ing.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    ing.add_argument("--ensembl-release", default="116")
    ing.add_argument("--transform-only", action="store_true",
                     help="re-derive from already-landed raw rows, without re-downloading")

    nc = sub.add_parser("ingest-ncbi", help="land the NCBI Gene dumps whole, then derive")
    nc.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    nc.add_argument("--taxa", default="9606,10090",
                    help="taxa to DERIVE annotation for; raw is always landed whole")

    bs = sub.add_parser("ingest-bugsigdb", help="land a BugSigDB export release (no transform yet)")
    bs.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    bs.add_argument("--version", default=bugsigdb.DEFAULT_VERSION,
                    help="BugSigDBExports release tag, e.g. v1.3.1. Tags are immutable; "
                         "the devel branch re-exports hourly and is not")

    npm = sub.add_parser("ingest-ncbi-pubmed", help="land NCBI gene2pubmed whole, then derive")
    npm.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    npm.add_argument("--taxa", default="9606,10090",
                     help="taxa to DERIVE annotation for; raw is always landed whole")

    sub.add_parser("tables", help="list catalog tables")
    args = p.parse_args()

    cat = catalog()
    if args.cmd == "ingest-ensembl":
        if args.transform_only:
            info = ensembl.species_info(args.ensembl_release, args.species)
            counts = ensembl.transform(cat, args.release, info, args.ensembl_release)
        else:
            counts = ensembl.ingest(cat, args.release, args.species, args.ensembl_release)
        for name, c in counts.items():
            if isinstance(c, dict):
                print(f"{name:35} {c['written']:>10,} written  {c['unchanged']:>10,} unchanged")
            else:
                print(f"{name:35} {c:>10,} rows")
    elif args.cmd == "ingest-bugsigdb":
        n = bugsigdb.land_raw(cat, args.release, args.version)
        print(f"{'raw.bugsigdb_full_dump':40} {n:>10,} rows  ({args.version})")
    elif args.cmd == "ingest-ncbi":
        counts = ncbi.ingest(cat, args.release, [int(t) for t in args.taxa.split(",")])
        for name, c in counts.items():
            print(f"{name:40} {c['written']:>10,} written  {c['unchanged']:>10,} unchanged"
                  if isinstance(c, dict) else f"{name:40} {c:>10,} rows")
    elif args.cmd == "ingest-ncbi-pubmed":
        counts = ncbi_pubmed.ingest(cat, args.release, [int(t) for t in args.taxa.split(",")])
        for name, c in counts.items():
            print(f"{name:40} {c['written']:>10,} written  {c['unchanged']:>10,} unchanged"
                  if isinstance(c, dict) else f"{name:40} {c:>10,} rows")
    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
