import argparse

from . import catalog, ensembl


def main():
    p = argparse.ArgumentParser(prog="bioconice")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest-ensembl", help="land one species of one Ensembl release, then derive")
    ing.add_argument("species", help="e.g. homo_sapiens")
    ing.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    ing.add_argument("--ensembl-release", default="116")
    ing.add_argument("--transform-only", action="store_true",
                     help="re-derive from already-landed raw rows, without re-downloading")

    sub.add_parser("tables", help="list catalog tables")
    args = p.parse_args()

    cat = catalog()
    if args.cmd == "ingest-ensembl":
        if args.transform_only:
            info = ensembl.species_info(args.ensembl_release, args.species)
            counts = ensembl.transform(cat, args.release, info, args.ensembl_release)
        else:
            counts = ensembl.ingest(cat, args.release, args.species, args.ensembl_release)
        for name, rows in counts.items():
            print(f"{name:35} {rows:>10,} rows")
    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
